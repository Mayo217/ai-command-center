#!/usr/bin/env python3
"""SessionStart hook: register the session and tell it what was already built here.
Matchers: startup|resume|clear|compact. Output: hookSpecificOutput.additionalContext (<=1800 chars)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hook_common as hc
import db


def main(data):
    con = db.connect()
    sid = hc.ensure_session(con, data, source="live")
    src = data.get("source") or data.get("matcher") or "startup"
    db.add_event(con, sid, "session_start", {"source": src, "cwd": data.get("cwd")})
    ctx = hc.recent_context(con, sid, data.get("cwd"))
    hc.emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}})
    db.log_line("hooks", f"session_start {sid[:8]} src={src} cwd={data.get('cwd')} ctx={len(ctx)}c")


if __name__ == "__main__":
    hc.run("session_start", main)
