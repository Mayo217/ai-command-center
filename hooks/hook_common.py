#!/usr/bin/env python3
"""Shared helpers for the AI Command Center hooks.

Contract notes live in hooks/HOOK_CONTRACT.md (verified against the Claude Code 2.1.x docs).
Golden rule: a hook must never wedge Claude. Everything is wrapped; on any internal error we
log to logs/hooks.log and exit 0. The single deliberate exception is the Stop gate's block.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
ROOT = HOOKS_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import db  # noqa: E402
import oscompat  # noqa: E402

MAX_CONTEXT_CHARS = 2600  # phase 3: room for the continuity block
MAX_REASON_CHARS = 1200


# ------------------------------------------------------------------ plumbing
def read_event() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception as e:
        db.log_line("hooks", f"unparseable stdin: {e}: {raw[:200]!r}")
        return {}


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def run(name: str, fn) -> None:
    """Wrap a hook body so it can never crash Claude Code."""
    data = read_event()
    try:
        fn(data)
    except SystemExit:
        raise
    except Exception:
        db.log_line("hooks", f"{name} ERROR: {traceback.format_exc(limit=4)}")
    sys.exit(0)


# ------------------------------------------------------------------ session helpers
def ensure_session(con, data: dict, **extra):
    sid = data.get("session_id") or "unknown"
    fields = {
        "cwd": oscompat.posix(data["cwd"]) if data.get("cwd") else None,
        "transcript_path": data.get("transcript_path"),
        "last_activity": db.now_iso(),
    }
    if data.get("model"):
        fields["model"] = data["model"]
    fields.update({k: v for k, v in extra.items() if v is not None})
    row = con.execute("SELECT 1 FROM sessions WHERE session_id=?", (sid,)).fetchone()
    if not row:
        fields.setdefault("started_at", db.now_iso())
        fields.setdefault("source", "live")
    db.upsert_session(con, sid, **fields)
    return sid


def bump(con, sid: str, **counters):
    if not counters:
        return
    sets = ", ".join(f"{k}={k}+{int(v)}" for k, v in counters.items())
    con.execute(f"UPDATE sessions SET {sets}, last_activity=?, updated_at=? WHERE session_id=?",
                (db.now_iso(), db.now_iso(), sid))


# ------------------------------------------------------------------ transcript (tail only)
def transcript_meta(path: str | None, tail_bytes: int = 200_000) -> dict:
    """Cheap O(tail) scan of a transcript for ai-title / cost-state / model."""
    out: dict = {}
    if not path:
        return out
    try:
        p = Path(path)
        if not p.exists():
            return out
        size = p.stat().st_size
        with open(p, "rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()  # drop the partial line
            chunk = fh.read().decode("utf-8", "replace")
    except Exception:
        return out
    for line in chunk.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        t = r.get("type")
        if t == "ai-title" and r.get("aiTitle"):
            out["title"] = str(r["aiTitle"])[:140]
        elif t == "cost-state":
            out["cost_usd"] = r.get("totalCostUSD")
            out["lines_added"] = r.get("totalLinesAdded")
            out["lines_removed"] = r.get("totalLinesRemoved")
            mu = r.get("modelUsage") or {}
            if mu:
                out["model_usage"] = json.dumps(mu)
                try:
                    out["model"] = max(mu.items(), key=lambda kv: (kv[1] or {}).get("costUSD", 0))[0]
                except Exception:
                    pass
        elif t == "last-prompt" and r.get("lastPrompt") and not out.get("last_prompt"):
            out["last_prompt"] = str(r["lastPrompt"])[:500]
        elif t == "assistant":
            # the closing assistant text is the best free description of what the session did
            for blk in ((r.get("message") or {}).get("content") or []):
                if isinstance(blk, dict) and blk.get("type") == "text":
                    txt = " ".join(str(blk.get("text") or "").split())
                    if len(txt) >= 120:
                        out["recap"] = txt[:1200]
    return out


# ------------------------------------------------------------------ the "recently built here" context
def recent_context(con, sid: str, cwd: str | None) -> str:
    cwd = oscompat.posix(cwd or os.getcwd())
    proj = db.derive_project(cwd)
    limit = int(db.config().get("session_start_recent_items", 8))
    rows = con.execute(
        "SELECT * FROM items WHERE status NOT IN ('deleted','abandoned') AND "
        "(project=? OR id IN (SELECT item_id FROM item_files WHERE path=? OR path LIKE ?)) "
        "ORDER BY COALESCE(built_at, created_at) DESC, id DESC LIMIT ?",
        (proj, cwd, cwd.rstrip("/") + "/%", limit + 1)).fetchall()
    total = con.execute(
        "SELECT count(*) n FROM items WHERE status NOT IN ('deleted','abandoned') AND "
        "(project=? OR id IN (SELECT item_id FROM item_files WHERE path=? OR path LIKE ?))",
        (proj, cwd, cwd.rstrip("/") + "/%")).fetchone()["n"]

    head = (
        f"CLAUDE TRACKER (mandatory): session {sid[:8]} registered in the build ledger. "
        "Before you hand back after building or changing anything durable, run:\n"
        '  tracker log --title "<name of the thing>" --summary "<what it is + why, >=60 chars>" '
        '--usage "<how to run it / where it lives>" --file <path>\n'
        "(skill: log-build). The Stop gate blocks the handback while file changes are unlogged; "
        '`tracker waive --reason "..."` is only for sessions that made nothing durable. '
        'Files your subagents write count as yours.\n'
    )
    if not rows:
        body = f"Nothing has been logged for {proj} yet - you are first. Log what you build here.\n"
    else:
        lines = []
        for r in rows[:limit]:
            f = con.execute("SELECT path FROM item_files WHERE item_id=? ORDER BY CASE role WHEN 'entry' THEN 0 "
                            "WHEN 'created' THEN 1 ELSE 2 END, id LIMIT 1", (r["id"],)).fetchone()
            when = (r["built_at"] or r["created_at"] or "")[:10]
            path = f["path"] if f else ""
            if path.startswith(cwd + "/"):
                path = "./" + path[len(cwd) + 1:]
            lines.append(f"  #{r['id']} · {when} · {r['kind']} · {r['title']}" + (f" — {path}" if path else ""))
        body = f"Recently built in {proj} (newest first - do NOT rebuild these, extend them):\n" + "\n".join(lines) + "\n"
        if total > limit:
            body += f"  {total - limit} older item(s): `tracker recent --cwd .` · search: `tracker search \"...\"`\n"
    txt = head + body + continuity_block(con, proj, current_sid=sid)
    if len(txt) > MAX_CONTEXT_CHARS:
        txt = txt[:MAX_CONTEXT_CHARS - 3].rsplit("\n", 1)[0] + "\n..."
    return txt


def continuity_block(con, project: str, current_sid: str | None = None) -> str:
    """what the last two sessions here ended up saying, what is still open, and a
    usage warning when the account is near a limit. Every part degrades to nothing if its table
    or data is missing.

    Only *ended* sessions are quoted, and never this one: a resumed session already carries an
    `ended_at` from its previous handback, and quoting your own last recap back at yourself is
    noise, not continuity."""
    out = []
    rows = con.execute(
        "SELECT session_id, title, first_prompt, COALESCE(ended_at, started_at, created_at) ts, cost_usd "
        "FROM sessions WHERE project=? AND ended_at IS NOT NULL AND session_id IS NOT ?"
        "ORDER BY ended_at DESC LIMIT 2", (project, current_sid)).fetchall()
    if rows:
        out.append(f"Last sessions in {project}:")
        for r in rows:
            cost = f" · ${r['cost_usd']:.2f}" if r["cost_usd"] else ""
            title = (r["title"] or (r["first_prompt"] or "")[:60] or r["session_id"][:8]).replace("\n", " ")
            out.append(f"  {(r['ts'] or '')[:10]} · {title[:70]}{cost}")
            recap = db.session_recap(con, r["session_id"], 350)
            if recap:
                out.append(f"    ended: {recap}")
    wip = con.execute(
        "SELECT id, title FROM items WHERE project=? AND status='wip' ORDER BY COALESCE(built_at, created_at) DESC "
        "LIMIT 6", (project,)).fetchall()
    if wip:
        out.append(f"Open work: {len(wip)} item(s) with status=wip - "
                   + "; ".join(f"#{r['id']} {r['title'][:48]}" for r in wip))
    warn = db.usage_warning_line(con)
    if warn:
        out.append(warn)
    return ("\n" + "\n".join(out) + "\n") if out else ""


# ------------------------------------------------------------------ inferred item (nothing is lost)
def subagent_active(data: dict, within_s: float = 120.0) -> str | None:
    """Path of a subagent transcript for this session written in the last `within_s` seconds.

    Claude Code puts agent transcripts in ~/.claude/projects/<proj>/<sid>/**.jsonl. A parent that
    is handing back while its agents are still writing is not an unlogged session — it is a
    session whose authors have not finished. The gate defers instead of blocking on files the
    parent did not write and cannot describe yet; it will be asked again at the next handback.
    """
    tp = data.get("transcript_path")
    sid = data.get("session_id")
    if not tp or not sid:
        return None
    try:
        d = Path(tp).parent / str(sid)
        if not d.is_dir():
            return None
        now = __import__("time").time()
        for f in d.rglob("*.jsonl"):
            try:
                if now - f.stat().st_mtime <= within_s:
                    return str(f)
            except OSError:
                continue
    except Exception:
        return None
    return None


def create_inferred_item(con, sid: str, data: dict, trigger: str, claim: bool = True) -> int | None:
    """Auto-create an 'inferred' item for pending file touches so a bypassed/ended session
    never loses the knowledge. Curate later with `tracker edit <id>`.

    `claim=False` records the paths without taking them: on a gate bypass the files may still
    belong to a subagent that is about to log them properly, and an auto item that has already
    claimed them leaves the real author with nothing to attach. The paths go to
    `items.extra.unclaimed_paths` and `session_files.logged_item_id` stays NULL, so the next
    `tracker log --file <path>` claims them cleanly (and drops them from this list).
    """
    pend = db.pending_files(con, sid)
    if not pend:
        return None
    s = con.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    cwd = oscompat.posix((s["cwd"] if s and s["cwd"] else data.get("cwd")) or os.getcwd())
    project = db.derive_project(cwd)
    meta = transcript_meta(data.get("transcript_path"))
    title = (s["title"] if s and s["title"] else None) or meta.get("title") or f"Unlogged changes in {project}"
    title = title.strip()[:140]
    if len(title) < 8:
        title = f"Unlogged changes in {project}"
    paths = [r["path"] for r in pend]
    first_prompt = (s["first_prompt"] if s and s["first_prompt"] else meta.get("last_prompt")) or ""
    summary = (
        f"AUTO-CAPTURED ({trigger}): this session changed {len(paths)} file(s) that were never logged, "
        f"so the ledger recorded them rather than lose them. Files: " + ", ".join(paths[:8]) +
        (f". First ask: {first_prompt[:200]}" if first_prompt else "") +
        " — review and rewrite this entry with `tracker edit`."
    )
    kind = "build" if any(r["op"] == "create" for r in pend) else "enhancement"
    if paths and all(p.lower().endswith(".md") for p in paths):
        kind = "doc"
    usage = (f"Files: {', '.join(paths[:6])}. Session {sid[:8]}"
             + (f" started {s['started_at'][:16]}" if s and s["started_at"] else "")
             + f", cwd {cwd}."
             + (f" Transcript: {data.get('transcript_path')}" if data.get("transcript_path") else ""))
    cur = con.execute(
        "INSERT INTO items(session_id,built_at,kind,title,summary,usage,project,status,confidence,source,tags,model,cost_usd)"
        " VALUES(?,?,?,?,?,?,?,'wip','inferred','auto',?,?,?)",
        (sid, db.now_iso(), kind, title, summary[:4000], usage, project,
         json.dumps([project, "auto-captured", trigger]),
         (s["model"] if s else None) or meta.get("model"), meta.get("cost_usd")))
    extra = {"recap": meta.get("recap"), "first_prompt": first_prompt[:500] or None,
             "trigger": trigger}
    if not claim:
        extra["unclaimed_paths"] = paths
    con.execute("UPDATE items SET extra=? WHERE id=?", (json.dumps(extra), cur.lastrowid))
    item_id = cur.lastrowid
    for r in pend:
        ex = os.path.exists(r["path"])
        con.execute("INSERT OR IGNORE INTO item_files(item_id,path,role,exists_at_log,size_bytes) VALUES(?,?,?,?,?)",
                    (item_id, r["path"], "created" if r["op"] == "create" else "modified", 1 if ex else 0,
                     os.path.getsize(r["path"]) if ex and os.path.isfile(r["path"]) else None))
        if claim:
            con.execute("UPDATE session_files SET logged_item_id=? WHERE id=?", (item_id, r["id"]))
    db.reindex_item(con, item_id)
    db.add_event(con, sid, "auto_item", {"item_id": item_id, "trigger": trigger,
                                         "files": len(pend), "claimed": bool(claim)})
    db.log_line("hooks", f"auto-created inferred item #{item_id} for {sid[:8]} ({trigger}, "
                         f"{len(pend)} files, {'claimed' if claim else 'unclaimed'})")
    return item_id


# ------------------------------------------------------------------ Command Center indexing
def index_transcript(transcript_path: str | None, con=None) -> dict:
    """Feed this session's transcript to the Command Center indexes (module 7 search, module 3
    usage). Both are optional: if the module is missing, being written, or raises, the hook
    carries on — indexing must never be the reason a session cannot end."""
    out: dict = {}
    if not transcript_path or not os.path.exists(transcript_path):
        return out
    for name in ("search", "usage"):
        try:
            mod = __import__(f"modules.{name}", fromlist=["index_session"])
            fn = getattr(mod, "index_session", None)
            if fn is None:
                continue
            res = fn(transcript_path, con) if con is not None else fn(transcript_path)
            out[name] = {k: v for k, v in (res or {}).items() if k != "transcript"}
        except Exception as e:
            db.log_line("hooks", f"index_transcript({name}) skipped: {type(e).__name__}: {e}")
    return out


# ------------------------------------------------------------------ tool-input -> file paths
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "Update"}


def paths_from_tool(tool_name: str, tool_input: dict, tool_response, cwd: str | None) -> list[tuple[str, str]]:
    """[(abs_path, op)] for a file-editing tool call."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    raw = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
    if not raw:
        return []
    p = db.norm_path(str(raw), cwd)
    resp = ""
    try:
        resp = tool_response if isinstance(tool_response, str) else json.dumps(tool_response)
    except Exception:
        resp = str(tool_response)
    created = False
    if tool_name in ("Write", "Update"):
        low = (resp or "").lower()
        if "created" in low or "has been created" in low:
            created = True
        elif "updated" in low or "modified" in low:
            created = False
        else:
            created = True  # Write with no hint in the response: treat as a new file
    return [(p, "create" if created else "modify")]
