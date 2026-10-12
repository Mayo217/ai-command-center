"""Command Center module 2 — Live sessions.

GET  /api/live/sessions      Claude Code sessions running on this Mac right now
GET  /api/live/recent?hours= sessions that ended recently, with a resume command
POST /api/live/open          {sid} -> open a terminal window running `claude --resume <sid>`

Everything except the POST is read-only. Detection is process-first (`ps` + `lsof` for the cwd,
`/tmp/cc-socks/<pid>.sock` for the control socket) and then joined to the transcript under
~/.claude/projects by the encoded-cwd directory name, so a session is identified even when it has
not yet written a row into the tracker DB.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from modules.common import ApiError, run_cmd  # noqa: E402

HOME = Path.home()
PROJECTS = HOME / ".claude" / "projects"
SOCK_DIR = Path("/tmp/cc-socks")
TAIL_BYTES = 300_000

_CACHE: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: float, fn):
    hit = _CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (now, val)
    return val


# ---------------------------------------------------------------- processes
_ETIME = re.compile(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$")


def parse_etime(s: str) -> int:
    """`03-14:59:56` / `14:45:20` / `10:58` -> seconds."""
    m = _ETIME.match(s.strip())
    if not m:
        return 0
    d, h, mi, sec = m.groups()
    return int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi) * 60 + int(sec)


def is_claude_cli(command: str) -> bool:
    """The CLI, not Claude.app (Electron), not a grep, not this server."""
    if not command:
        return False
    if "Claude.app" in command or "ai-command-center" in command or "/grep" in command:
        return False
    argv0 = command.split()[0]
    base = os.path.basename(argv0)
    if base == "claude":
        return True
    # `node .../versions/2.1.269/cli.js` style launches
    return "/share/claude/versions/" in command and base in ("node", "bun", "cli.js")


def claude_processes() -> list[dict]:
    rc, out, _ = run_cmd(["/bin/ps", "-axo", "pid=,ppid=,etime=,command="], timeout=8)
    if rc != 0:
        return []
    procs = []
    for line in out.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid, ppid, etime, command = parts
        if not pid.isdigit() or not is_claude_cli(command):
            continue
        procs.append({"pid": int(pid), "ppid": int(ppid), "elapsed_s": parse_etime(etime),
                      "elapsed": etime, "command": command[:300]})
    return procs


def proc_cwd(pid: int) -> str | None:
    rc, out, _ = run_cmd(["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], timeout=6)
    if rc != 0:
        return None
    for line in out.splitlines():
        if line.startswith("n/"):
            return line[1:].strip()
    return None


def socket_for(pid: int) -> str | None:
    p = SOCK_DIR / f"{pid}.sock"
    try:
        return str(p) if p.exists() else None
    except OSError:
        return None


# ---------------------------------------------------------------- transcripts
def encode_cwd(cwd: str) -> str:
    """Claude Code names a project dir after the cwd with every separator turned into '-'."""
    return re.sub(r"[/\\.:]", "-", cwd.rstrip("/\\")) or "-"


def transcripts_for_cwd(cwd: str, active_within_s: float = 7 * 86400) -> list[dict]:
    """Transcripts in this cwd's project dir, newest activity first, with birth/modify times."""
    d = PROJECTS / encode_cwd(cwd)
    if not d.is_dir():
        return []
    now = time.time()
    out = []
    try:
        for f in d.glob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            if now - st.st_mtime > active_within_s:
                continue
            out.append({"path": f, "mtime": st.st_mtime,
                        "birth": getattr(st, "st_birthtime", st.st_ctime)})
    except OSError:
        return []
    out.sort(key=lambda r: -r["mtime"])
    return out


def match_processes_to_transcripts(procs: list[dict]) -> dict[int, dict]:
    """claude does not hold its transcript open (it appends and closes), so a pid cannot be mapped
    to a session id by lsof. Match instead on time: among the transcripts of the process's cwd that
    were written *after* the process started, take the one whose creation time is closest to the
    process start, and let each transcript be claimed by only one pid. Confidence is reported.
    Several sessions in the same directory are the case this exists to get right."""
    now = time.time()
    claimed: set[str] = set()
    result: dict[int, dict] = {}
    for pr in sorted(procs, key=lambda p: -p["elapsed_s"]):  # oldest process first
        cwd = pr.get("cwd")
        if not cwd:
            continue
        start = now - pr["elapsed_s"]
        best, best_score = None, None
        for t in transcripts_for_cwd(cwd):
            if str(t["path"]) in claimed or t["mtime"] < start:
                continue
            score = abs(t["birth"] - start)
            if best_score is None or score < best_score:
                best, best_score = t, score
        if best is not None:
            claimed.add(str(best["path"]))
            result[pr["pid"]] = {"path": best["path"], "delta_s": round(best_score, 1),
                                 "confidence": "high" if best_score <= 180 else
                                               "medium" if best_score <= 3600 else "low"}
    return result


def transcript_summary(path: str | Path, tail_bytes: int = TAIL_BYTES) -> dict:
    """O(tail) read of a transcript: title, last prompt, model, cost, rough turn counts."""
    out: dict = {"transcript": str(path)}
    try:
        p = Path(path)
        size = p.stat().st_size
        out["size_bytes"] = size
        out["mtime"] = p.stat().st_mtime
        with open(p, "rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()
            chunk = fh.read().decode("utf-8", "replace")
    except OSError as e:
        out["error"] = str(e)
        return out
    users = assistants = 0
    last_ts = None
    for line in chunk.splitlines():
        if not line.startswith("{"):
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        t = r.get("type")
        if r.get("timestamp"):
            last_ts = r["timestamp"]
        if t == "ai-title" and r.get("aiTitle"):
            out["title"] = str(r["aiTitle"])[:140]
        elif t == "last-prompt" and r.get("lastPrompt"):
            out["last_prompt"] = str(r["lastPrompt"])[:400]
        elif t == "cost-state":
            out["cost_usd"] = r.get("totalCostUSD")
            out["lines_added"] = r.get("totalLinesAdded")
            out["lines_removed"] = r.get("totalLinesRemoved")
            mu = r.get("modelUsage") or {}
            if mu:
                try:
                    out["model"] = max(mu.items(), key=lambda kv: (kv[1] or {}).get("costUSD", 0))[0]
                except Exception:
                    pass
        elif t == "user" and not r.get("isSidechain") and isinstance((r.get("message") or {}).get("content"), str):
            users += 1
            out["session_id"] = r.get("sessionId", out.get("session_id"))
        elif t == "assistant":
            assistants += 1
            out["session_id"] = r.get("sessionId", out.get("session_id"))
            m = (r.get("message") or {}).get("model")
            if m:
                out.setdefault("model", m)
    out["tail_user_turns"] = users
    out["tail_assistant_turns"] = assistants
    out["last_event_at"] = last_ts
    out["partial"] = size > tail_bytes
    if not out.get("session_id"):
        out["session_id"] = Path(path).stem
    return out


# ---------------------------------------------------------------- handlers
def _db_row(con, sid: str) -> dict | None:
    r = con.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    return dict(r) if r else None


def get_sessions(query=None, body=None, match=None):
    """Running Claude Code sessions, newest activity first."""
    def build():
        con = db.connect()
        rows = []
        seen: set[str] = set()
        procs = claude_processes()
        for pr in procs:
            pr["cwd"] = proc_cwd(pr["pid"])
        matches = match_processes_to_transcripts(procs)
        for pr in procs:
            cwd = pr["cwd"]
            mt = matches.get(pr["pid"])
            info = transcript_summary(mt["path"]) if mt else {}
            sid = info.get("session_id")
            drow = _db_row(con, sid) if sid else None
            idle = (time.time() - info["mtime"]) if info.get("mtime") else None
            rows.append({
                "pid": pr["pid"], "elapsed": pr["elapsed"], "elapsed_s": pr["elapsed_s"],
                "command": pr["command"], "cwd": cwd,
                "project": db.derive_project(cwd) if cwd else None,
                "session_id": sid,
                "title": info.get("title") or (drow or {}).get("title"),
                "last_prompt": info.get("last_prompt") or (drow or {}).get("first_prompt"),
                "model": info.get("model") or (drow or {}).get("model"),
                "cost_usd": info.get("cost_usd") if info.get("cost_usd") is not None else (drow or {}).get("cost_usd"),
                "user_turns": (drow or {}).get("user_turns") or info.get("tail_user_turns"),
                "tool_calls": (drow or {}).get("tool_calls"),
                "transcript_path": info.get("transcript"),
                "idle_s": round(idle, 1) if idle is not None else None,
                "pulse": ("active" if idle is not None and idle < 90 else
                          "thinking" if idle is not None and idle < 600 else "idle"),
                "socket": socket_for(pr["pid"]),
                "match_confidence": (mt or {}).get("confidence"),
                "resume_cmd": f"claude --resume {sid}" if sid else None,
                "logged_items": con.execute(
                    "SELECT count(*) n FROM items WHERE session_id=? AND status!='deleted'", (sid,)).fetchone()["n"]
                if sid else 0,
                "pending_files": len(db.pending_files(con, sid)) if sid else 0,
            })
            if sid:
                seen.add(sid)
        rows.sort(key=lambda r: (r["idle_s"] if r["idle_s"] is not None else 1e9))
        return {"ok": True, "count": len(rows), "sessions": rows, "generated_at": db.now_iso()}
    return 200, _cached("sessions", 4.0, build)


def get_recent(query=None, body=None, match=None):
    """Sessions that were active recently, whether or not their process is still alive."""
    query = query or {}
    try:
        hours = max(1, min(24 * 30, int(query.get("hours", 48))))
    except ValueError:
        raise ApiError(400, "hours must be an integer")
    limit = max(1, min(200, int(query.get("limit", 40) or 40)))
    since = time.time() - hours * 3600
    con = db.connect()
    live_sids = {s["session_id"] for s in get_sessions()[1]["sessions"] if s.get("session_id")}
    files: list[tuple[float, Path]] = []
    try:
        for d in PROJECTS.iterdir():
            if not d.is_dir() or d.name == "memory":
                continue
            for f in d.glob("*.jsonl"):
                try:
                    m = f.stat().st_mtime
                except OSError:
                    continue
                if m >= since:
                    files.append((m, f))
    except OSError:
        pass
    files.sort(reverse=True)
    rows = []
    for m, f in files[:limit]:
        sid = f.stem
        drow = _db_row(con, sid) or {}
        info = transcript_summary(f, tail_bytes=120_000)
        rows.append({
            "session_id": sid,
            "ended_at": drow.get("ended_at"),
            "last_activity": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(m)),
            "minutes_ago": round((time.time() - m) / 60, 1),
            "project": drow.get("project") or db.derive_project(drow.get("cwd") or ""),
            "cwd": drow.get("cwd"),
            "title": info.get("title") or drow.get("title"),
            "last_prompt": info.get("last_prompt") or drow.get("first_prompt"),
            "model": info.get("model") or drow.get("model"),
            "cost_usd": info.get("cost_usd") if info.get("cost_usd") is not None else drow.get("cost_usd"),
            "live": sid in live_sids,
            "logged": bool(drow.get("logged")),
            "waived": bool(drow.get("waived_at")),
            "transcript_path": str(f),
            "resume_cmd": f"claude --resume {sid}",
        })
    return 200, {"ok": True, "count": len(rows), "hours": hours, "sessions": rows}


SID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def known_session(con, sid: str) -> bool:
    """A sid is only real if the ledger knows it or a transcript exists for it. Nothing reaches a
    shell on the strength of the caller's word."""
    if con.execute("SELECT 1 FROM sessions WHERE session_id=?", (sid,)).fetchone():
        return True
    try:
        return any((d / f"{sid}.jsonl").is_file() for d in PROJECTS.iterdir() if d.is_dir())
    except OSError:
        return False


def safe_dir(path: str | None) -> str | None:
    """An existing directory inside this user's home, or nothing. Used for every cwd we hand to a
    shell or to osascript."""
    if not path:
        return None
    try:
        p = Path(os.path.expanduser(str(path))).resolve()
    except OSError:
        return None
    if not p.is_dir():
        return None
    home = HOME.resolve()
    return str(p) if p == home or str(p).startswith(str(home) + "/") else None


def _terminal_app() -> str:
    return "Warp" if Path("/Applications/Warp.app").exists() else "Terminal"


def post_open(query=None, body=None, match=None):
    """Open a new terminal window resuming a session. Audited into session_events."""
    body = body or {}
    sid = str(body.get("sid") or body.get("session_id") or "").strip().lower()
    if not SID_RE.match(sid):
        raise ApiError(400, "sid must be a Claude Code session UUID "
                            "(8-4-4-4-12 lowercase hex), e.g. 00000000-0000-0000-0000-000000000000")
    con = db.connect()
    if not known_session(con, sid):
        raise ApiError(400, f"unknown session {sid}: it is not in the ledger and has no transcript "
                            "under ~/.claude/projects - refusing to run anything for it")
    row = _db_row(con, sid)
    cwd = safe_dir((row or {}).get("cwd")) or str(HOME)
    cmd = f"cd {json.dumps(cwd)} && claude --resume {sid}"
    app = _terminal_app()
    if app == "Warp":
        script = (f'tell application "Warp" to activate\n'
                  f'delay 0.4\n'
                  f'tell application "System Events" to keystroke "t" using command down\n'
                  f'delay 0.5\n'
                  f'tell application "System Events" to keystroke {json.dumps(cmd)}\n'
                  f'tell application "System Events" to key code 36')
    else:
        script = f'tell application "Terminal"\nactivate\ndo script {json.dumps(cmd)}\nend tell'
    rc, out, err = run_cmd(["/usr/bin/osascript", "-e", script], timeout=15)
    db.add_event(con, "cc", "action", {"module": "live", "action": "open", "sid": sid, "app": app, "rc": rc})
    if rc != 0:
        raise ApiError(500, f"could not open {app}: {(err or out or '').strip()[:200]}")
    return 200, {"ok": True, "sid": sid, "app": app, "cmd": cmd}


ROUTES = [
    ("GET", r"/api/live/sessions/?$", get_sessions),
    ("GET", r"/api/live/recent/?$", get_recent),
    ("POST", r"/api/live/open/?$", post_open),
]

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Command Center live module — running Claude Code sessions.")
    ap.add_argument("--recent", type=int, metavar="HOURS", help="show recently ended sessions instead")
    a = ap.parse_args()
    res = get_recent({"hours": str(a.recent)})[1] if a.recent else get_sessions()[1]
    print(json.dumps(res, indent=2, default=str))
