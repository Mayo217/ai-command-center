#!/usr/bin/env python3
"""PermissionRequest hook: record that a session is blocked waiting for a human. Never decides.

A session sitting on a permission prompt in a background terminal is invisible today — the
Live board sees a quiet transcript and reads it as idle. This records the ask so "waiting on
you" becomes a state the API can report.

Payload (confirmed against Claude Code 2.1.269 — hooks/HOOK_CONTRACT.md §1):
  session_id, transcript_path, cwd, hook_event_name="PermissionRequest",
  tool_name, tool_input, permission_suggestions?[]
The matcher for this event is `tool_name`.

**This hook is observe-only, by design.** The event's `hookSpecificOutput.decision` can
`{"behavior":"allow"}` or `{"behavior":"deny"}` a tool call outright. The tracker observes; it
does not decide what Claude may run, and a bug in a tracker hook must never silently approve
something. So nothing is ever written to stdout: an empty stdout with exit 0 carries no
decision and the normal permission flow proceeds untouched.
"""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hook_common as hc
import db

MAX_INPUT_CHARS = 400


def _brief(tool_name: str, tool_input) -> str:
    """One readable line about what is being asked for — no decision, just the ask."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    for key in ("command", "file_path", "notebook_path", "path", "url", "pattern"):
        if ti.get(key):
            return f"{tool_name}: {str(ti[key])[:MAX_INPUT_CHARS]}"
    try:
        return f"{tool_name}: {json.dumps(ti)[:MAX_INPUT_CHARS]}"
    except Exception:
        return tool_name


def main(data):
    sid = data.get("session_id") or "unknown"
    con = db.connect()
    hc.ensure_session(con, data)
    tool = data.get("tool_name") or "?"
    brief = _brief(tool, data.get("tool_input"))
    sug = data.get("permission_suggestions")
    db.add_event(con, sid, "permission_request", {
        "tool_name": tool,
        "brief": brief,
        "agent_id": data.get("agent_id"),
        "agent_type": data.get("agent_type"),
        "permission_mode": data.get("permission_mode"),
        "n_suggestions": len(sug) if isinstance(sug, list) else None,
        "cwd": data.get("cwd"),
    })
    db.log_line("hooks", f"permission_request {sid[:8]} {brief[:120]}")
    # deliberately no stdout: see the module docstring.


if __name__ == "__main__":
    hc.run("permission_request", main)
