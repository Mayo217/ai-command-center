#!/usr/bin/env python3
"""UserPromptSubmit hook: count turns, remember the first ask, and tell Claude where it stands.

Plain stdout from UserPromptSubmit is added to Claude's context, so every session sees its own
context fill, the limit windows with their burn, and any drain/anomaly alert
(modules/dynamics.py brief, cached 120 s). Set CLAUDE_USAGE_BRIEF=0 to silence it."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hook_common as hc
import db


def main(data):
    con = db.connect()
    sid = hc.ensure_session(con, data)
    prompt = (data.get("prompt") or "").strip()
    row = con.execute("SELECT first_prompt FROM sessions WHERE session_id=?", (sid,)).fetchone()
    if prompt and (not row or not row["first_prompt"]):
        con.execute("UPDATE sessions SET first_prompt=? WHERE session_id=?", (prompt[:500], sid))
    hc.bump(con, sid, user_turns=1)
    if os.environ.get("CLAUDE_USAGE_BRIEF", "1") != "0":
        try:
            from modules import dynamics
            text = dynamics.brief(dynamics.cached(max_age=120), data.get("session_id"))
            if text:
                sys.stdout.write(text + "\n")
        except Exception as e:      # the brief is a courtesy; the turn count is the hook's job
            hc.log("user_prompt_submit", f"brief failed: {e!r}") if hasattr(hc, "log") else None


if __name__ == "__main__":
    hc.run("user_prompt_submit", main)
