#!/usr/bin/env python3
"""Notification hook: record every notification Claude Code raises for this session.

`notification_type` is the interesting field — `agent_needs_input` and `agent_completed` are
the two states the Live board cannot currently see (a session waiting on a human looks exactly
like an idle one from the outside). Recording them makes "session X is waiting on you" a fact
in the ledger rather than something you notice by eye.

Payload (confirmed against Claude Code 2.1.269 — hooks/HOOK_CONTRACT.md §1):
  session_id, transcript_path, cwd, hook_event_name="Notification",
  message, title?, notification_type
The matcher for this event is `notification_type`, so registering without a matcher catches all.

Observe-only. `hookSpecificOutput.additionalContext` exists for this event but the tracker has
nothing to say back at a notification, so nothing is written to stdout. Always exits 0.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hook_common as hc
import db

# Types that mean a human is being asked for something. Anything else is informational.
BLOCKING_TYPES = {"agent_needs_input", "permission_request", "idle_prompt", "elicitation"}


def main(data):
    sid = data.get("session_id") or "unknown"
    con = db.connect()
    hc.ensure_session(con, data)
    ntype = (data.get("notification_type") or data.get("matcher") or "unknown")
    msg = str(data.get("message") or "")[:600]
    db.add_event(con, sid, "notification", {
        "notification_type": ntype,
        "title": (str(data.get("title"))[:200] if data.get("title") else None),
        "message": msg or None,
        "needs_human": ntype in BLOCKING_TYPES,
        "agent_id": data.get("agent_id"),
        "agent_type": data.get("agent_type"),
        "cwd": data.get("cwd"),
    })
    db.log_line("hooks", f"notification {sid[:8]} {ntype} {msg[:120]}")


if __name__ == "__main__":
    hc.run("notification", main)
