"""OpenAI Codex CLI (GPT models).

capture: reads ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl into the ledger as sessions
         (agent "codex"): cwd, project, model, prompts, tool calls, file edits, tokens.
rules:   ~/.codex/AGENTS.md, Codex's global instruction file ($CODEX_HOME honoured).
usage:   the plan windows Codex records in its logs (event_msg/token_count rate_limits).
gate:    not yet. Codex has its own hooks (including a blocking Stop hook); wiring them is planned
         once their configuration format is confirmed against the Codex docs.
"""
from __future__ import annotations

import json
import re

import db
from agents.base import Adapter, env_home

PATCH_PATH = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.M)


class Codex(Adapter):
    name = "codex"
    display = "Codex CLI"
    vendor = "OpenAI"
    binaries = ("codex",)
    data_dirs = (".codex",)
    capabilities = {"capture": True, "rules": True, "gate": False, "usage": True}
    notes = "sessions and plan limits from its logs; rules through ~/.codex/AGENTS.md"

    @property
    def home(self):
        return env_home("CODEX_HOME", ".codex")

    def detected(self) -> bool:
        return super().detected() or self.home.exists()

    def rules_path(self, project=None):
        return self.home / "AGENTS.md"

    # ------------------------------------------------------------------ capture
    def scan(self, con, full: bool = False) -> dict:
        root = self.home / "sessions"
        out = {"files": 0, "sessions": 0, "skipped": 0}
        if not root.is_dir():
            return {**out, "note": f"no {root}"}
        for f in sorted(root.rglob("rollout-*.jsonl")):
            key = f"agent_scan:codex:{f.name}"
            stamp = str(int(f.stat().st_mtime))
            seen = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if seen and seen["value"] == stamp and not full:
                out["skipped"] += 1
                continue
            s = parse_rollout(f)
            if s:
                store_session(con, s)
                if s.get("rate_limits"):
                    store_limits(con, s["rate_limits"], s["last_ts"])
                out["sessions"] += 1
            con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, stamp))
            out["files"] += 1
        con.commit()
        return out

    def usage(self, con):
        rows = con.execute("SELECT * FROM agent_usage WHERE agent='codex' ORDER BY window").fetchall()
        return [dict(r) for r in rows] or None


def parse_rollout(path) -> dict | None:
    """One rollout file -> a session summary. Unknown record types are skipped (the format moves
    between Codex versions)."""
    s = {"id": None, "cwd": None, "model": None, "started_at": None, "last_ts": None, "prompts": 0,
         "first_prompt": None, "tool_calls": 0, "shell_calls": 0, "files": set(), "tokens": None,
         "rate_limits": None, "cli_version": None}
    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        return None
    with fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            t, p = r.get("type"), r.get("payload") or {}
            ts = r.get("timestamp")
            if ts:
                s["last_ts"] = ts
                s["started_at"] = s["started_at"] or ts
            if t == "session_meta":
                s["id"] = p.get("id") or p.get("session_id")
                s["cwd"] = p.get("cwd") or s["cwd"]
                s["started_at"] = p.get("timestamp") or s["started_at"]
                s["cli_version"] = p.get("cli_version")
            elif t == "turn_context":
                s["model"] = p.get("model") or s["model"]
                s["cwd"] = s["cwd"] or p.get("cwd")
            elif t == "event_msg":
                pt = p.get("type")
                if pt == "user_message":
                    s["prompts"] += 1
                    s["first_prompt"] = s["first_prompt"] or (p.get("message") or "")[:300]
                elif pt == "token_count":
                    info = p.get("info") or {}
                    s["tokens"] = info.get("total_token_usage") or s["tokens"]
                    rl = p.get("rate_limits") or {}
                    if isinstance(rl.get("primary"), dict) or isinstance(rl.get("secondary"), dict):
                        s["rate_limits"] = rl  # keep the last reading that carries a window
                elif pt in ("patch_apply_begin", "patch_apply_end"):
                    s["files"].update((p.get("changes") or {}).keys())
            elif t == "response_item" and p.get("type") in ("function_call", "custom_tool_call"):
                s["tool_calls"] += 1
                name = p.get("name") or ""
                body = p.get("arguments") or p.get("input") or ""
                if name in ("exec_command", "shell", "local_shell"):
                    s["shell_calls"] += 1
                if name == "apply_patch" or "*** Begin Patch" in body:
                    s["files"].update(m.strip() for m in PATCH_PATH.findall(body))
    if not s["id"]:
        return None
    s["files"] = sorted(s["files"])
    return s


def store_session(con, s: dict) -> None:
    sid = f"codex:{s['id']}"
    cwd = db.norm_path(s["cwd"]) if s["cwd"] else None
    model_usage = json.dumps({"tokens": s["tokens"], "files_edited": s["files"][:200],
                              "cli_version": s["cli_version"]})
    con.execute(
        "INSERT INTO sessions(session_id, agent, source, cwd, project, model, title, first_prompt, started_at, "
        "last_activity, ended_at, user_turns, tool_calls, bash_calls, edits, model_usage, transcript_path, updated_at) "
        "VALUES(?, 'codex', 'agent-log', ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(session_id) DO UPDATE SET cwd=excluded.cwd, project=excluded.project, model=excluded.model, "
        "title=excluded.title, first_prompt=excluded.first_prompt, last_activity=excluded.last_activity, "
        "ended_at=excluded.ended_at, user_turns=excluded.user_turns, tool_calls=excluded.tool_calls, "
        "bash_calls=excluded.bash_calls, edits=excluded.edits, model_usage=excluded.model_usage, "
        "updated_at=excluded.updated_at",
        (sid, cwd, db.derive_project(cwd) if cwd else None, s["model"],
         (s["first_prompt"] or "Codex session")[:80], s["first_prompt"], s["started_at"], s["last_ts"],
         s["last_ts"], s["prompts"], s["tool_calls"], s["shell_calls"], len(s["files"]), model_usage, None,
         db.now_iso()))


def store_limits(con, rl: dict, ts: str) -> None:
    for window in ("primary", "secondary"):
        w = rl.get(window)
        if not isinstance(w, dict):
            continue
        con.execute("INSERT OR REPLACE INTO agent_usage(agent, window, ts, used_pct, window_minutes, resets_at, plan) "
                    "VALUES('codex',?,?,?,?,?,?)",
                    (window, ts, w.get("used_percent"), w.get("window_minutes"), w.get("resets_at"),
                     rl.get("plan_type")))


ADAPTER = Codex()
