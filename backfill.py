#!/usr/bin/env python3
"""AI Command Center — transcript backfill miner.

Mines every Claude Code transcript under ~/.claude/projects/ into the tracker DB:
  <proj>/<sid>.jsonl          → a session
  <proj>/<sid>/**/*.jsonl     → subagent transcripts, attributed to the parent <sid>
  <proj>/memory/*             → skipped (memory writes are not builds)

Streaming line parser (never loads a whole file), tolerant of bad lines, idempotent by
session_id, safe next to live (hook-owned) sessions. Stdlib only.

  python3 backfill.py --workers 6
  python3 backfill.py --since 2026-09-01 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
from db import (  # noqa: E402
    add_event, bash_written_paths, connect, derive_project, is_ignored,
    log_line, now_iso, reindex_item, touch_file, upsert_session,
)

WRITE_TOOLS = {"Write"}
EDIT_TOOLS = {"Edit", "MultiEdit", "NotebookEdit"}
FILE_TOOLS = WRITE_TOOLS | EDIT_TOOLS
KEEP_TYPES = {"ai-title", "cost-state", "summary"}
URL_RE = re.compile(r"https?://[^\s<>\"'\)\]\}]+")
HILITE_RE = re.compile(
    r"\b(built|created|added|wrote|saved|installed|deployed|now runs|wired)\b", re.I)
DOC_EXT = {".md", ".markdown", ".txt", ".rst"}
# the file you would actually run: these win the item's "entry" slot
CODE_EXT = {".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html",
            ".htm", ".css", ".scss", ".rb", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".swift",
            ".kt", ".php", ".pl", ".lua", ".r", ".sql", ".applescript", ".scpt", ".ipynb"}
# backups and run artefacts: real files, but never the entry point
JUNKY = re.compile(r"(\.bak|\.orig|\.tmp|\.old|~|\.out|\.err)(\W|_|\d|$)", re.I)
MAX_FILES_PER_ITEM = 40
MAX_COMMANDS_PER_TRANSCRIPT = 600   # a long session can run thousands; keep the tail readable
MAX_LINKS = 10
RECAP_MIN = 120
HILITE_MIN = 200
HILITE_CAP = 1200


# --------------------------------------------------------------------------- parsing
def _text_of(block) -> str:
    if isinstance(block, dict) and block.get("type") == "text":
        t = block.get("text")
        return t if isinstance(t, str) else ""
    return ""


def parse_transcript(path: str) -> dict:
    """Stream one .jsonl transcript. Returns a plain (picklable) dict of extracted facts."""
    r: dict = {
        "path": path, "sid": None, "cwd": None, "git_branch": None, "version": None,
        "started_at": None, "ended_at": None, "title": None, "first_prompt": None,
        "models": Counter(), "user_turns": 0, "assistant_turns": 0, "tool_calls": 0,
        "writes": 0, "edits": 0, "bash_calls": 0, "cost": None,
        "files": {},            # abs path -> [op, tool, first_ts, last_ts, count]
        "commands": [],         # (event_key, ts, cwd, command, description, wrote)
        "urls": [], "recap": None, "highlights": [], "bad_lines": 0, "lines": 0,
        "error": None,
    }
    files = r["files"]
    recent_urls = deque(maxlen=5)      # urls seen in the last 5 assistant messages
    all_urls: dict[str, None] = {}
    hilites: list[str] = []
    cur_cwd = None

    def note_file(p: str, op: str, tool: str, ts: str | None):
        if is_ignored(p):
            return
        e = files.get(p)
        if e is None:
            files[p] = [op, tool, ts, ts, 1]
        else:
            if op == "create":
                e[0] = "create"
            e[3] = ts or e[3]
            e[4] += 1

    try:
        fh = open(path, "r", errors="replace")
    except OSError as exc:
        r["error"] = str(exc)
        return r
    with fh:
        for line in fh:
            r["lines"] += 1
            if len(line) < 3:
                continue
            # fast pre-filter: skip records we never need without paying for json.loads
            if line.startswith('{"type":"'):
                try:
                    t0 = line[9:line.index('"', 9)]
                except ValueError:
                    t0 = ""
                if t0 not in KEEP_TYPES:
                    continue
            elif '"attachment":{' in line[:220]:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                r["bad_lines"] += 1
                continue
            if not isinstance(rec, dict):
                continue
            t = rec.get("type")

            if t == "ai-title":
                ttl = (rec.get("aiTitle") or "").strip()
                if ttl:
                    r["title"] = ttl          # keep the newest one
                continue
            if t == "cost-state":
                r["cost"] = rec
                continue
            if t == "summary":
                if not r["title"]:
                    s = (rec.get("summary") or "").strip()
                    if s:
                        r["title"] = s
                continue
            if t not in ("user", "assistant"):
                continue

            ts = rec.get("timestamp")
            if ts:
                if r["started_at"] is None or ts < r["started_at"]:
                    r["started_at"] = ts
                if r["ended_at"] is None or ts > r["ended_at"]:
                    r["ended_at"] = ts
            if r["sid"] is None and rec.get("sessionId"):
                r["sid"] = rec["sessionId"]
            rc = rec.get("cwd")
            if rc:
                cur_cwd = rc
                if r["cwd"] is None:
                    r["cwd"] = rc
            if r["git_branch"] is None and rec.get("gitBranch"):
                r["git_branch"] = rec["gitBranch"]
            if r["version"] is None and rec.get("version"):
                r["version"] = rec["version"]
            side = bool(rec.get("isSidechain"))
            msg = rec.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None

            if t == "user":
                if isinstance(content, str):
                    c = content.strip()
                    if c and not rec.get("isMeta") and not side and not c.startswith("<"):
                        r["user_turns"] += 1
                        if not r["first_prompt"]:
                            r["first_prompt"] = c[:500]
                continue

            # assistant
            r["assistant_turns"] += 1
            mdl = msg.get("model") if isinstance(msg, dict) else None
            if mdl:
                r["models"][mdl] += 1
            if not isinstance(content, list):
                continue
            msg_urls: list[str] = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "tool_use":
                    r["tool_calls"] += 1
                    name = b.get("name") or ""
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    if name in FILE_TOOLS:
                        if name in WRITE_TOOLS:
                            r["writes"] += 1
                        else:
                            r["edits"] += 1
                        fp = inp.get("file_path") or inp.get("notebook_path")
                        if isinstance(fp, str) and fp.strip():
                            note_file(db.norm_path(fp, cur_cwd),
                                      "create" if name in WRITE_TOOLS else "modify", name, ts)
                    elif name == "Bash":
                        r["bash_calls"] += 1
                        cmd = inp.get("command")
                        if isinstance(cmd, str) and cmd:
                            wrote = 0
                            try:
                                for p, op in bash_written_paths(cmd, cur_cwd):
                                    if db.plausible_path(p):   # drop regex debris
                                        note_file(p, op, "Bash", ts)
                                        wrote += 1
                            except Exception:
                                pass
                            if len(r["commands"]) < MAX_COMMANDS_PER_TRANSCRIPT:
                                r["commands"].append(
                                    (b.get("id"), ts, cur_cwd, cmd[:db.CMD_MAX],
                                     inp.get("description"), wrote))
                elif bt == "text":
                    txt = _text_of(b)
                    if not txt:
                        continue
                    for m in URL_RE.finditer(txt):
                        u = m.group(0).rstrip(".,;:!?`*")
                        if len(u) < 300:
                            msg_urls.append(u)
                            all_urls.setdefault(u, None)
                    if not side:
                        st = txt.strip()
                        if len(st) >= RECAP_MIN:
                            r["recap"] = st[:2000]
                        if len(st) >= HILITE_MIN and HILITE_RE.search(st):
                            hilites.append(st[:HILITE_CAP])
                            if len(hilites) > 24:      # keep memory bounded: keep longest
                                hilites.sort(key=len, reverse=True)
                                del hilites[8:]
            if msg_urls:
                recent_urls.append(msg_urls)

    # urls: prefer the ones from the last 5 assistant messages
    ordered: dict[str, None] = {}
    for grp in reversed(recent_urls):
        for u in grp:
            ordered.setdefault(u, None)
    for u in all_urls:
        ordered.setdefault(u, None)
    r["urls"] = list(ordered)[:MAX_LINKS]
    hilites.sort(key=len, reverse=True)
    seen = set()
    picked = []
    for h in hilites:
        k = h[:200]
        if k in seen or (r["recap"] and h[:120] == r["recap"][:120]):
            continue
        seen.add(k)
        picked.append(h)
        if len(picked) == 2:
            break
    r["highlights"] = picked
    r["models"] = dict(r["models"])
    return r


# --------------------------------------------------------------------------- discovery
def discover(projects_dir: Path) -> dict[str, dict]:
    """Return {session_id: {'main': path|None, 'subs': [paths], 'proj_dir': name}}."""
    groups: dict[str, dict] = {}
    for proj in sorted(p for p in projects_dir.iterdir() if p.is_dir()):
        for entry in sorted(proj.iterdir()):
            if entry.name == "memory":
                continue
            if entry.is_file() and entry.suffix == ".jsonl":
                g = groups.setdefault(entry.stem, {"main": None, "subs": [], "proj_dir": proj.name})
                g["main"] = str(entry)
            elif entry.is_dir():
                subs = [str(p) for p in sorted(entry.rglob("*.jsonl")) if "/memory/" not in str(p)]
                if subs:
                    g = groups.setdefault(entry.name, {"main": None, "subs": [], "proj_dir": proj.name})
                    g["subs"].extend(subs)
    return groups


# --------------------------------------------------------------------------- merging
def merge(sid: str, main: dict | None, subs: list[dict]) -> dict:
    s = main or {
        "path": subs[0]["path"] if subs else "", "cwd": None, "git_branch": None, "version": None,
        "started_at": None, "ended_at": None, "title": None, "first_prompt": None, "models": {},
        "user_turns": 0, "assistant_turns": 0, "tool_calls": 0, "writes": 0, "edits": 0,
        "bash_calls": 0, "cost": None, "files": {}, "commands": [], "urls": [], "recap": None,
        "highlights": [], "bad_lines": 0, "lines": 0,
    }
    out = dict(s)
    out["session_id"] = sid
    out["files"] = dict(s["files"])
    out["commands"] = list(s.get("commands") or [])
    out["sub_paths"] = []
    models = Counter(s.get("models") or {})
    for sub in subs:
        out["sub_paths"].append(sub["path"])
        out["commands"].extend(sub.get("commands") or [])
        for k in ("assistant_turns", "tool_calls", "writes", "edits", "bash_calls",
                  "bad_lines", "lines"):
            out[k] = out.get(k, 0) + sub.get(k, 0)
        for p, e in sub["files"].items():
            cur = out["files"].get(p)
            if cur is None:
                out["files"][p] = list(e)
            else:
                if e[0] == "create":
                    cur[0] = "create"
                cur[4] += e[4]
                if e[3] and (not cur[3] or e[3] > cur[3]):
                    cur[3] = e[3]
        if sub.get("started_at") and (not out["started_at"] or sub["started_at"] < out["started_at"]):
            out["started_at"] = sub["started_at"]
        if sub.get("ended_at") and (not out["ended_at"] or sub["ended_at"] > out["ended_at"]):
            out["ended_at"] = sub["ended_at"]
        if main is None:
            models.update(sub.get("models") or {})
            if not out["cwd"]:
                out["cwd"] = sub.get("cwd")
            out["user_turns"] += sub.get("user_turns", 0)
            if not out["recap"]:
                out["recap"] = sub.get("recap")
            for u in sub.get("urls") or []:
                if u not in out["urls"]:
                    out["urls"] = list(out["urls"]) + [u]
    out["models"] = dict(models)
    out["model"] = models.most_common(1)[0][0] if models else None
    out["urls"] = list(out["urls"])[:MAX_LINKS]
    cost = out.get("cost") or {}
    out["cost_usd"] = cost.get("totalCostUSD")
    out["lines_added"] = cost.get("totalLinesAdded")
    out["lines_removed"] = cost.get("totalLinesRemoved")
    out["model_usage"] = json.dumps(cost.get("modelUsage")) if cost.get("modelUsage") else None
    live_files = {p: e for p, e in out["files"].items() if not is_ignored(p)}
    out["files"] = live_files
    out["project"] = derive_project(out.get("cwd"))
    out["substantive"] = bool(
        (out.get("writes", 0) + out.get("edits", 0) + len(live_files)) >= 1
        or out.get("user_turns", 0) >= 3)
    return out


# --------------------------------------------------------------------------- item shaping
def short_model(model: str | None) -> str | None:
    if not model:
        return None
    m = model.lower()
    if "fable" in m:
        n = re.search(r"fable-(\d+)-(\d+)", m)
        return f"fable-{n.group(1)}.{n.group(2)}" if n else "fable"
    for fam in ("opus", "sonnet", "haiku"):
        if fam in m:
            n = re.search(fam + r"-(\d+)", m)
            return f"{fam}-{n.group(1)}.x" if n else fam
    return model.replace("claude-", "")[:24]


def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


_MD_NOISE = re.compile(r"[*_`#>]+")


def make_title(sess: dict) -> str:
    """ai-title → first typed prompt → first sentence of the closing recap → project+sid.
    (Helper/agent sessions driven by cross-session messages have no typed prompt and no
    ai-title, so the recap is the only human-readable name they have.)"""
    t = clean(sess.get("title"))
    if not t:
        t = clean(sess.get("first_prompt"))[:90]
    if not t:
        rc = _MD_NOISE.sub("", clean(sess.get("recap")))
        if rc:
            t = re.split(r"(?<=[.!?])\s", rc)[0][:90]
    t = t.strip(" -–—:·")
    if len(t) > 140:
        t = t[:137].rsplit(" ", 1)[0] + "…"
    proj = sess.get("project") or "unknown"
    if len(t) < 8:
        t = f"{t + ' — ' if t else ''}{proj} session {sess['session_id'][:8]}"
    return t[:140]


def guess_kind(files: list[tuple[str, list]], user_turns: int) -> str:
    if not files:
        return "research" if user_turns >= 3 else "other"
    exts = {os.path.splitext(p)[1].lower() for p, _ in files}
    if exts and exts <= DOC_EXT:
        return "doc"
    if any(e[0] == "create" for _, e in files):
        return "build"
    return "enhancement"


def rank_files(files: dict[str, list]) -> list[tuple[str, list]]:
    """Code first, then data outputs, then docs; within a tier the most-touched file leads,
    so item_files[0] (role=entry) is the thing you would actually run."""
    def key(kv):
        p, e = kv
        ext = os.path.splitext(p)[1].lower()
        if JUNKY.search(os.path.basename(p)):
            tier = 3                       # backups / .out logs never lead
        elif ext in CODE_EXT:
            tier = 0
        elif ext in DOC_EXT:
            tier = 2
        else:
            tier = 1                       # data, config, artefacts, extensionless
        return (tier, -e[4], p)
    return sorted(files.items(), key=key)


def build_item_payload(sess: dict) -> dict:
    ranked = rank_files(sess["files"])[:MAX_FILES_PER_ITEM]
    recap = clean(sess.get("recap"))
    first = clean(sess.get("first_prompt"))
    parts = []
    if recap:
        parts.append(recap[:700])
    if first:
        parts.append("First ask: " + first[:200])
    summary = "\n\n".join(parts)
    if len(summary) < 60:
        flist = ", ".join(os.path.basename(p) for p, _ in ranked[:6])
        extra = (f"Session {sess['session_id'][:8]} in {sess.get('project')} "
                 f"touched {len(sess['files'])} file(s): {flist or 'none'}. "
                 f"No closing summary was written in the transcript.")
        summary = (summary + "\n\n" + extra).strip() if summary else extra
    date = (sess.get("ended_at") or sess.get("started_at") or "")[:10]
    usage = (f"Files: {', '.join(p for p, _ in ranked[:6]) or 'none'}. "
             f"Session {sess['session_id'][:8]} on {date}, cwd {sess.get('cwd') or '?'}. "
             f"Transcript: {sess.get('path')}")
    tags = [sess.get("project") or "unknown"]
    sm = short_model(sess.get("model"))
    if sm:
        tags.append(sm)
    tags.append("backfill")
    others = {derive_project(os.path.dirname(p)) for p, _ in ranked}
    for o in sorted(others - {sess.get("project"), None}):
        if o and o not in tags:
            tags.append(o)
    extra = {"recap": (sess.get("recap") or "")[:2000],
             "first_prompt": sess.get("first_prompt") or "",
             "highlights": sess.get("highlights") or [],
             "transcript": sess.get("path"),
             "subagent_transcripts": sess.get("sub_paths") or [],
             "counts": {k: sess.get(k, 0) for k in
                        ("user_turns", "assistant_turns", "tool_calls", "writes", "edits", "bash_calls")},
             "files_total": len(sess["files"])}
    return {"title": make_title(sess), "summary": summary, "usage": usage,
            "kind": guess_kind(ranked, sess.get("user_turns", 0)), "tags": tags,
            "files": ranked, "links": sess.get("urls") or [], "extra": extra}


# --------------------------------------------------------------------------- summarize (opt-in)
def ai_summarize(payload: dict, sess: dict, timeout: int = 90) -> str | None:
    """OFF by default (--summarize). Ask `claude -p --model haiku` for a 2-sentence summary."""
    files = "\n".join(p for p, _ in payload["files"][:10])
    prompt = (
        "In exactly two sentences, plainly describe what was built or changed in this Claude Code "
        "session and why it exists. No preamble, no markdown.\n\n"
        f"Title: {payload['title']}\nProject: {sess.get('project')}\n"
        f"First ask: {(sess.get('first_prompt') or '')[:400]}\n"
        f"Closing recap: {(sess.get('recap') or '')[:1500]}\nFiles:\n{files}\n")
    try:
        pr = subprocess.run(["claude", "-p", "--model", "haiku", prompt],
                            capture_output=True, text=True, timeout=timeout)
        out = clean(pr.stdout)
        return out[:1500] if len(out) >= 60 else None
    except Exception as exc:
        log_line("backfill", f"summarize failed: {exc}")
        return None


# --------------------------------------------------------------------------- DB writes
def write_session(con, sess: dict, existing: dict | None, args) -> dict:
    """Insert/refresh the session row + its session_files. Returns a small stat dict."""
    sid = sess["session_id"]
    stat = {"files": 0, "mode": "new"}
    is_live = bool(existing and existing["source"] == "live")
    if is_live:
        stat["mode"] = "live-refresh"
        sets, vals = [], []
        for col, val in (("title", make_title(sess)), ("model", sess.get("model")),
                         ("first_prompt", sess.get("first_prompt")), ("cwd", sess.get("cwd")),
                         ("git_branch", sess.get("git_branch")), ("cc_version", sess.get("version")),
                         ("transcript_path", sess.get("path")), ("ended_at", sess.get("ended_at"))):
            if val:
                sets.append(f"{col}=COALESCE(NULLIF({col},''),?)")
                vals.append(val)
        for col, val in (("cost_usd", sess.get("cost_usd")), ("lines_added", sess.get("lines_added")),
                         ("lines_removed", sess.get("lines_removed")),
                         ("model_usage", sess.get("model_usage"))):
            if val is not None:
                sets.append(f"{col}=COALESCE({col},?)")
                vals.append(val)
        if sets:
            con.execute(f"UPDATE sessions SET {', '.join(sets)}, "
                        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE session_id=?",
                        (*vals, sid))
    else:
        upsert_session(
            con, sid, source="backfill", cwd=sess.get("cwd"), project=sess.get("project"),
            git_branch=sess.get("git_branch"), cc_version=sess.get("version"),
            title=make_title(sess), first_prompt=sess.get("first_prompt"),
            model=sess.get("model"), started_at=sess.get("started_at"),
            ended_at=sess.get("ended_at"), last_activity=sess.get("ended_at"),
            user_turns=sess.get("user_turns", 0), assistant_turns=sess.get("assistant_turns", 0),
            tool_calls=sess.get("tool_calls", 0), writes=sess.get("writes", 0),
            edits=sess.get("edits", 0), bash_calls=sess.get("bash_calls", 0),
            cost_usd=sess.get("cost_usd"), lines_added=sess.get("lines_added"),
            lines_removed=sess.get("lines_removed"), model_usage=sess.get("model_usage"),
            transcript_path=sess.get("path"), substantive=1 if sess["substantive"] else 0)
    for p, e in sess["files"].items():
        op, tool, fts, lts, _cnt = e
        touch_file(con, sid, p, op=op, tool=tool, ts=lts or fts or sess.get("ended_at") or now_iso())
        stat["files"] += 1
    for key, cts, ccwd, cmd, desc, wrote in (sess.get("commands") or []):
        if db.record_command(con, sid, cmd, cwd=ccwd, description=desc, wrote=wrote,
                             ts=cts or sess.get("ended_at") or now_iso(),
                             event_key=key, source="backfill"):
            stat["commands"] = stat.get("commands", 0) + 1
    stat["live"] = is_live
    return stat


def write_item(con, sess: dict, payload: dict) -> int:
    sid = sess["session_id"]
    built_at = sess.get("ended_at") or sess.get("started_at")
    cur = con.execute(
        "INSERT INTO items(session_id,built_at,kind,title,summary,usage,project,status,"
        "confidence,source,tags,model,cost_usd,extra) "
        "VALUES(?,?,?,?,?,?,?,'active','inferred','backfill',?,?,?,?)",
        (sid, built_at, payload["kind"], payload["title"], payload["summary"], payload["usage"],
         sess.get("project"), json.dumps(payload["tags"]), sess.get("model"),
         sess.get("cost_usd"), json.dumps(payload["extra"])))
    item_id = cur.lastrowid
    for idx, (p, e) in enumerate(payload["files"]):
        role = "entry" if idx == 0 else ("created" if e[0] == "create" else "modified")
        try:
            st = os.stat(p)
            ex, size = 1, st.st_size
        except OSError:
            ex, size = 0, None
        con.execute("INSERT OR IGNORE INTO item_files(item_id,path,role,exists_at_log,size_bytes) "
                    "VALUES(?,?,?,?,?)", (item_id, p, role, ex, size))
        con.execute("UPDATE session_files SET logged_item_id=? WHERE session_id=? AND path=? "
                    "AND logged_item_id IS NULL", (item_id, sid, p))
    for u in payload["links"]:
        label = "artifact" if "claude.ai" in u else None
        con.execute("INSERT OR IGNORE INTO item_links(item_id,url,label) VALUES(?,?,?)",
                    (item_id, u, label))
    con.execute("UPDATE sessions SET substantive=1 WHERE session_id=?", (sid,))
    reindex_item(con, item_id)
    add_event(con, sid, "backfill", {"item_id": item_id, "files": len(payload["files"])})
    return item_id


def purge_session(con, sid: str) -> None:
    """--force: drop previous backfill artefacts for this session so we can re-mine cleanly."""
    ids = [r["id"] for r in con.execute(
        "SELECT id FROM items WHERE session_id=? AND source='backfill'", (sid,)).fetchall()]
    for i in ids:
        con.execute("UPDATE session_files SET logged_item_id=NULL WHERE logged_item_id=?", (i,))
        con.execute("DELETE FROM item_files WHERE item_id=?", (i,))
        con.execute("DELETE FROM item_links WHERE item_id=?", (i,))
        con.execute("DELETE FROM items_fts WHERE rowid=?", (i,))
        con.execute("DELETE FROM items WHERE id=?", (i,))



# --------------------------------------------------------------------------- extra passes
def _all_transcripts(groups: dict, since_epoch: float | None, limit: int | None) -> list[str]:
    """Every transcript file (session + subagent), newest first, for the usage/search passes."""
    def mtime(g):
        ps = ([g["main"]] if g["main"] else []) + g["subs"]
        return max((os.path.getmtime(p) for p in ps if os.path.exists(p)), default=0)
    out, n = [], 0
    for sid, g in sorted(groups.items(), key=lambda kv: -mtime(kv[1])):
        if since_epoch and mtime(g) < since_epoch:
            continue
        if limit and n >= limit:
            break
        if g["main"]:
            out.append(g["main"])
        out.extend(g["subs"])
        n += 1
    return out


def _map_paths(fn, paths: list[str], workers: int):
    """Run a pure per-file extractor over paths, in worker processes when asked."""
    if workers and workers > 1 and len(paths) > 1:
        import multiprocessing as mp
        try:
            ctx = mp.get_context("fork")
        except ValueError:
            ctx = mp.get_context()
        with ctx.Pool(workers) as pool:
            for res in pool.imap_unordered(fn, paths, chunksize=4):
                yield res
    else:
        for p in paths:
            yield fn(p)


def usage_pass(con, paths: list[str], args, rep: dict) -> None:
    """per-assistant-message token usage → usage_events (idempotent per msg id)."""
    from modules import usage as usage_mod
    t0 = time.time()
    rows_seen = inserted = 0
    con.execute("BEGIN")
    try:
        for i, rows in enumerate(_map_paths(usage_mod.extract_usage, paths, args.workers), 1):
            if rows:
                rows_seen += len(rows)
                inserted += usage_mod.store_usage(con, rows)
            if i % 100 == 0:
                con.execute("COMMIT"); con.execute("BEGIN")
                if not args.quiet:
                    print(f"  usage … {i}/{len(paths)} files", file=sys.stderr)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    rep["usage"] = {"files": len(paths), "messages": rows_seen, "inserted": inserted,
                    "seconds": round(time.time() - t0, 1),
                    "total_rows": con.execute("SELECT count(*) FROM usage_events").fetchone()[0],
                    "cost_usd_est": round(con.execute(
                        "SELECT coalesce(sum(cost_usd_est),0) FROM usage_events").fetchone()[0], 2)}


def transcripts_pass(con, paths: list[str], args, rep: dict) -> None:
    """transcript_fts chunks + artifacts (idempotent per session)."""
    from modules import search as search_mod
    t0 = time.time()
    chunks = urls = truncated = replaced = 0
    con.execute("BEGIN")
    try:
        for i, res in enumerate(_map_paths(search_mod.extract_chunks, paths, args.workers), 1):
            if res["chunks"] or res["urls"]:
                st = search_mod.store_chunks(con, res, replace=bool(args.force))
                chunks += st["chunks"]; urls += st["urls"]; replaced += st["replaced"]
            truncated += 1 if res["truncated"] else 0
            if i % 100 == 0:
                con.execute("COMMIT"); con.execute("BEGIN")
                if not args.quiet:
                    print(f"  transcripts … {i}/{len(paths)} files", file=sys.stderr)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    rep["transcripts"] = {
        "files": len(paths), "chunks_added": chunks, "chunks_replaced": replaced,
        "urls": urls, "sessions_capped": truncated, "seconds": round(time.time() - t0, 1),
        "total_chunks": con.execute("SELECT count(*) FROM transcript_chunks").fetchone()[0],
        "text_mb": round((con.execute(
            "SELECT coalesce(sum(length(text)),0) FROM transcript_chunks").fetchone()[0]) / 1e6, 1),
        "artifacts": con.execute("SELECT count(*) FROM artifacts").fetchone()[0]}


# --------------------------------------------------------------------------- report
def write_report(report: dict, root: Path) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = root / "logs" / f"backfill_{ts}.md"
    out.parent.mkdir(exist_ok=True)
    L = [f"# Backfill run {ts}", "",
         f"- projects dir: `{report['projects_dir']}`", f"- db: `{report['db']}`",
         f"- elapsed: {report['elapsed_s']}s (workers {report['workers']})",
         f"- transcripts parsed: {report['files_parsed']} ({report['bytes_mb']} MB), "
         f"bad lines: {report['bad_lines']}",
         f"- sessions seen: {report['sessions_seen']} · new/updated: {report['sessions_written']} "
         f"· skipped (already backfilled): {report['skipped_existing']} "
         f"· live refreshed: {report['live_refreshed']}",
         f"- substantive: {report['substantive']} · items created: {report['items']} "
         f"· files attached: {report['files_attached']} · links: {report['links']}",
         f"- corpus cost: ${report['total_cost']:.2f}", "",
         "## Per-project", "", "| project | sessions | substantive | items | files | cost |",
         "|---|---:|---:|---:|---:|---:|"]
    for p, c in sorted(report["per_project"].items(), key=lambda kv: -kv[1]["items"]):
        L.append(f"| {p} | {c['sessions']} | {c['substantive']} | {c['items']} | {c['files']} "
                 f"| ${c['cost']:.2f} |")
    L += ["", "## 20 most expensive sessions", "",
          "| cost | date | project | title | files | item |", "|---:|---|---|---|---:|---|"]
    for s in report["top_cost"]:
        L.append(f"| ${s['cost']:.2f} | {s['date']} | {s['project']} | {s['title'][:80]} "
                 f"| {s['files']} | {s['item_id'] or '—'} |")
    if report.get("usage"):
        u = report["usage"]
        L += ["", "## Usage pass (usage_events)", "",
              f"- {u['files']} transcripts → {u['messages']} assistant messages, "
              f"{u['inserted']} new rows in {u['seconds']}s",
              f"- table now {u['total_rows']} rows, estimated corpus cost ${u['cost_usd_est']}"]
    if report.get("transcripts"):
        t = report["transcripts"]
        L += ["", "## Transcript pass (transcript_fts + artifacts)", "",
              f"- {t['files']} transcripts → {t['chunks_added']} new chunks "
              f"({t['chunks_replaced']} replaced), {t['urls']} urls in {t['seconds']}s",
              f"- index now {t['total_chunks']} chunks / {t['text_mb']} MB of text, "
              f"{t['artifacts']} artifact rows, {t['sessions_capped']} transcripts hit the "
              f"{400}-chunk cap"]
    L += ["", "## Per-kind", ""]
    for k, n in sorted(report["per_kind"].items(), key=lambda kv: -kv[1]):
        L.append(f"- {k}: {n}")
    out.write_text("\n".join(L) + "\n")
    return out


def print_report(report: dict, log_path: Path | None):
    w = sys.stdout.write
    w("\n" + "=" * 78 + "\n")
    w(f"BACKFILL{'  (dry run)' if report['dry_run'] else ''}  "
      f"{report['files_parsed']} transcripts / {report['bytes_mb']} MB in {report['elapsed_s']}s "
      f"({report['workers']} workers)\n")
    w("=" * 78 + "\n")
    w(f"sessions seen {report['sessions_seen']} · written {report['sessions_written']} · "
      f"skipped-existing {report['skipped_existing']} · live-refreshed {report['live_refreshed']}\n")
    w(f"substantive {report['substantive']} · items {report['items']} · "
      f"files attached {report['files_attached']} · links {report['links']} · "
      f"corpus cost ${report['total_cost']:.2f}\n\n")
    w(f"{'project':28} {'sess':>5} {'subst':>6} {'items':>6} {'files':>6} {'cost':>9}\n")
    w("-" * 78 + "\n")
    for p, c in sorted(report["per_project"].items(), key=lambda kv: -kv[1]["items"])[:25]:
        w(f"{p[:28]:28} {c['sessions']:>5} {c['substantive']:>6} {c['items']:>6} "
          f"{c['files']:>6} {c['cost']:>8.2f}\n")
    w("-" * 78 + "\n")
    w("kinds: " + ", ".join(f"{k}={n}" for k, n in
                            sorted(report["per_kind"].items(), key=lambda kv: -kv[1])) + "\n")
    w("\ntop sessions by cost:\n")
    for s in report["top_cost"][:20]:
        w(f"  ${s['cost']:>7.2f}  {s['date']}  {s['project'][:16]:16}  {s['title'][:52]}\n")
    if report.get("usage"):
        u = report["usage"]
        w(f"\nusage pass: {u['messages']} messages / {u['inserted']} new rows in {u['seconds']}s "
          f"→ {u['total_rows']} rows, ${u['cost_usd_est']} estimated\n")
    if report.get("transcripts"):
        t = report["transcripts"]
        w(f"transcript pass: {t['chunks_added']} new chunks ({t['chunks_replaced']} replaced), "
          f"{t['urls']} urls in {t['seconds']}s → {t['total_chunks']} chunks / {t['text_mb']} MB "
          f"text, {t['artifacts']} artifacts, {t['sessions_capped']} capped\n")
    if log_path:
        w(f"\nreport written to {log_path}\n")


# --------------------------------------------------------------------------- main
def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="backfill.py", description="Mine Claude Code transcripts into the tracker ledger.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--since", metavar="YYYY-MM-DD", help="only sessions that started on/after this date")
    ap.add_argument("--limit", type=int, help="stop after N sessions (newest first)")
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    ap.add_argument("--force", action="store_true", help="re-mine sessions already backfilled")
    ap.add_argument("--workers", type=int, default=1, help="parse transcripts in N processes")
    ap.add_argument("--summarize", action="store_true",
                    help="call `claude -p --model haiku` per substantive session (costs money; off by default)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--projects-dir", default=None, help="default: config.json projects_dir")
    ap.add_argument("--db", default=None, help="path to tracker.db (default: db.py DB_PATH)")
    ap.add_argument("--usage", action="store_true",
                    help="also mine per-message token usage into usage_events (modules/usage.py)")
    ap.add_argument("--transcripts", action="store_true",
                    help="also build the transcript search index + artifacts (modules/search.py)")
    ap.add_argument("--no-items", action="store_true",
                    help="skip the session/item pass (use with --usage / --transcripts)")
    ap.add_argument("--quiet", action="store_true", help="suppress progress output")
    return ap.parse_args(argv)


def run(args) -> dict:
    """Entry point used by tracker.py and the tests. Never leaves a transaction open."""
    con_holder: list = []
    try:
        return _run(args, con_holder)
    except Exception:
        for c in con_holder:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
            try:
                c.close()
            except Exception:
                pass
        raise


def _run(args, con_holder: list | None = None) -> dict:
    t0 = time.time()
    projects_dir = Path(args.projects_dir or db.config().get("projects_dir")
                        or (Path.home() / ".claude/projects")).expanduser()
    if not projects_dir.is_dir():
        print(f"backfill: no such projects dir: {projects_dir}", file=sys.stderr)
        sys.exit(1)
    con = connect(args.db) if args.db else connect()
    if con_holder is not None:
        con_holder.append(con)
    try:
        db.ensure_fts_deletable(con)
    except Exception as exc:          # pragma: no cover - non-fatal
        log_line("backfill", f"ensure_fts_deletable: {exc}")

    groups = discover(projects_dir)
    existing = {r["session_id"]: r for r in
                con.execute("SELECT session_id, source, title, model, cost_usd FROM sessions")}
    existing_items = {r["session_id"] for r in
                      con.execute("SELECT DISTINCT session_id FROM items WHERE session_id IS NOT NULL")}

    # order newest-first by mtime so --limit takes the most recent work
    def mtime(g):
        ps = ([g["main"]] if g["main"] else []) + g["subs"]
        return max((os.path.getmtime(p) for p in ps if os.path.exists(p)), default=0)

    ordered = sorted(groups.items(), key=lambda kv: -mtime(kv[1]))
    since_epoch = None
    if args.since:
        since_epoch = time.mktime(time.strptime(args.since, "%Y-%m-%d"))

    todo, skipped_existing = [], 0
    for sid, g in ordered:
        ex = existing.get(sid)
        if ex is not None and ex["source"] == "backfill" and not args.force:
            skipped_existing += 1
            continue
        if since_epoch and mtime(g) < since_epoch:
            continue
        todo.append((sid, g))
        if args.limit and len(todo) >= args.limit:
            break

    paths = []
    for sid, g in todo:
        if g["main"]:
            paths.append(g["main"])
        paths.extend(g["subs"])
    total_bytes = sum(os.path.getsize(p) for p in paths if os.path.exists(p))

    parsed: dict[str, dict] = {}
    if args.workers and args.workers > 1 and len(paths) > 1:
        import multiprocessing as mp
        try:
            ctx = mp.get_context("fork")
        except ValueError:
            ctx = mp.get_context()
        with ctx.Pool(args.workers) as pool:
            for res in pool.imap_unordered(parse_transcript, paths, chunksize=4):
                parsed[res["path"]] = res
    else:
        for p in paths:
            parsed[p] = parse_transcript(p)

    rep = {"dry_run": bool(args.dry_run), "projects_dir": str(projects_dir),
           "db": str(args.db or db.DB_PATH), "workers": args.workers,
           "files_parsed": len(paths), "bytes_mb": round(total_bytes / 1e6, 1),
           "bad_lines": sum(r["bad_lines"] for r in parsed.values()),
           "sessions_seen": len(groups), "sessions_written": 0,
           "skipped_existing": skipped_existing, "live_refreshed": 0, "substantive": 0,
           "items": 0, "files_attached": 0, "links": 0, "total_cost": 0.0,
           "per_project": {}, "per_kind": {}, "top_cost": [], "since": args.since,
           "summarized": 0}
    per_project = rep["per_project"]
    per_kind = rep["per_kind"]
    cost_rows = []

    if args.no_items:
        todo = []
    if not args.dry_run and todo:
        con.execute("BEGIN")
    n = 0
    in_tx = bool(todo) and not args.dry_run
    for sid, g in todo:
        main = parsed.get(g["main"]) if g["main"] else None
        subs = [parsed[p] for p in g["subs"] if p in parsed]
        if main is None and not subs:
            continue
        sess = merge(sid, main, subs)
        if since_epoch and sess.get("started_at") and \
                sess["started_at"][:10] < time.strftime("%Y-%m-%d", time.localtime(since_epoch)):
            continue
        proj = sess.get("project") or "unknown"
        pc = per_project.setdefault(proj, {"sessions": 0, "substantive": 0, "items": 0,
                                           "files": 0, "cost": 0.0})
        pc["sessions"] += 1
        rep["sessions_written"] += 1
        if sess.get("cost_usd"):
            rep["total_cost"] += sess["cost_usd"]
            pc["cost"] += sess["cost_usd"]
        if sess["substantive"]:
            rep["substantive"] += 1
            pc["substantive"] += 1

        ex = existing.get(sid)
        item_id = None
        payload = None
        if sess["substantive"]:
            payload = build_item_payload(sess)
        if not args.dry_run:
            if args.force and ex is not None and ex["source"] == "backfill":
                purge_session(con, sid)
            st = write_session(con, sess, ex, args)
            if st["live"]:
                rep["live_refreshed"] += 1
            make_item = bool(payload) and not (ex is not None and ex["source"] == "live")
            if make_item and sid in existing_items and not args.force:
                make_item = False
            if make_item:
                if args.summarize:
                    s2 = ai_summarize(payload, sess)
                    if s2:
                        payload["summary"] = (s2 + "\n\nFirst ask: "
                                              + clean(sess.get("first_prompt"))[:200]).strip()
                        payload["tags"].append("ai-summarized")
                        rep["summarized"] += 1
                item_id = write_item(con, sess, payload)
                rep["items"] += 1
                rep["files_attached"] += len(payload["files"])
                rep["links"] += len(payload["links"])
                pc["items"] += 1
                pc["files"] += len(payload["files"])
                per_kind[payload["kind"]] = per_kind.get(payload["kind"], 0) + 1
        elif payload:
            rep["items"] += 1
            rep["files_attached"] += len(payload["files"])
            rep["links"] += len(payload["links"])
            pc["items"] += 1
            pc["files"] += len(payload["files"])
            per_kind[payload["kind"]] = per_kind.get(payload["kind"], 0) + 1

        cost_rows.append({"cost": sess.get("cost_usd") or 0.0,
                          "date": (sess.get("started_at") or "")[:10], "project": proj,
                          "title": make_title(sess), "files": len(sess["files"]),
                          "item_id": item_id, "session_id": sid})
        n += 1
        if not args.dry_run and n % 50 == 0:
            con.execute("COMMIT")
            con.execute("BEGIN")
            if not args.quiet:
                print(f"  … {n}/{len(todo)} sessions", file=sys.stderr)
    if in_tx:
        con.execute("COMMIT")
        in_tx = False

    if (args.usage or args.transcripts) and not args.dry_run:
        extra_paths = _all_transcripts(groups, since_epoch, args.limit)
        rep["extra_files"] = len(extra_paths)
        if args.usage:
            usage_pass(con, extra_paths, args, rep)
        if args.transcripts:
            transcripts_pass(con, extra_paths, args, rep)
    elif args.usage or args.transcripts:
        rep["passes_skipped"] = "dry-run: --usage/--transcripts write nothing"

    rep["top_cost"] = sorted(cost_rows, key=lambda r: -r["cost"])[:20]
    rep["elapsed_s"] = round(time.time() - t0, 1)
    log_path = None
    if not args.dry_run:
        log_path = write_report(rep, db.ROOT)
        log_line("backfill", f"run: {rep['sessions_written']} sessions, {rep['items']} items, "
                             f"{rep['elapsed_s']}s")
    rep["log_path"] = str(log_path) if log_path else None
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
    elif not args.quiet:
        print_report(rep, log_path)
    con.close()
    return rep


def main(argv=None):
    args = parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
