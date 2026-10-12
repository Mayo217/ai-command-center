#!/usr/bin/env python3
"""PreCompact hook: the moment a session loses its memory, written down.

Compaction replaces the conversation with a summary. Everything the session knew but never
said out loud is gone at that instant, and afterwards nobody can tell whether a session
"forgot" or was simply never told. This hook records a `compact` event carrying the session's
recap (the closing assistant text from the transcript tail), the file touches that were pending
at that moment, and how many turns/tools had happened — so the continuity block in
`hook_common.recent_context()` and the Ledger timeline can both explain the discontinuity.

Payload (confirmed against Claude Code 2.1.269 — hooks/HOOK_CONTRACT.md §1):
  session_id, transcript_path, cwd, hook_event_name="PreCompact",
  trigger: "manual" | "auto",  custom_instructions: string | null
The matcher for this event is `trigger`, so registering it without a matcher catches both.

PreCompact has no additionalContext channel in 2.1.269 (its hookSpecificOutput is not in the
union), so this hook writes and stays quiet. It always exits 0: a tracker bug must never be
the reason a session cannot compact.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hook_common as hc
import db


def main(data):
    sid = data.get("session_id") or "unknown"
    con = db.connect()
    hc.ensure_session(con, data)
    trigger = data.get("trigger") or data.get("matcher") or "auto"
    custom = data.get("custom_instructions")

    meta = hc.transcript_meta(data.get("transcript_path"))
    recap = (meta.get("recap") or "").strip()
    pend = [r["path"] for r in db.pending_files(con, sid)]
    s = con.execute("SELECT user_turns, tool_calls, writes, edits, bash_calls, title "
                    "FROM sessions WHERE session_id=?", (sid,)).fetchone()

    payload = {
        "trigger": trigger,
        "custom_instructions": (str(custom)[:500] if custom else None),
        "recap": recap[:1200] or None,
        "title": (meta.get("title") or (s["title"] if s else None)),
        "last_prompt": (meta.get("last_prompt") or "")[:300] or None,
        "pending_files": pend[:40], "n_pending": len(pend),
        "user_turns": (s["user_turns"] if s else None),
        "tool_calls": (s["tool_calls"] if s else None),
        "writes": (s["writes"] if s else None),
        "edits": (s["edits"] if s else None),
        "bash_calls": (s["bash_calls"] if s else None),
        "cost_usd": meta.get("cost_usd"),
    }
    db.add_event(con, sid, "compact", payload)
    # A recap event is what db.session_recap() reads first from session_events; recording one
    # here means the pre-compaction memory survives even if the session never ends cleanly.
    if recap:
        db.add_event(con, sid, "recap", {"text": recap[:1200], "at": "pre_compact"})
    # Whatever it knew is about to go: make sure the transcript itself is indexed now, so the
    # searchable record predates the compaction rather than the summary that replaces it.
    indexed = hc.index_transcript(data.get("transcript_path"), con)
    db.log_line("hooks", f"pre_compact {sid[:8]} trigger={trigger} pending={len(pend)} "
                         f"recap={len(recap)}c index={indexed}")


if __name__ == "__main__":
    hc.run("pre_compact", main)
