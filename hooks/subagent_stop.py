#!/usr/bin/env python3
"""SubagentStop hook: close the subagent's own row and attribute its file writes to it.

Why this exists (A hole hit in practice): every tool call a
subagent makes reaches PostToolUse carrying the PARENT's `session_id`. Until this hook existed
the parent was gated at handback for files it had never touched and could not describe. The
subagent's `agent_id` is on every one of those payloads, so `hooks/post_tool_use.py` now records
the author in `agent_files`; this hook closes the loop at the moment the agent finishes:

  * upserts the `session_agents` row (type, transcript, last message, recap, file count),
  * records a `subagent_stop` session event listing exactly which paths that agent wrote, and
    which of them nothing in the ledger claims yet,
  * leaves the paths PENDING on the parent — this is deliberate: a subagent's files count
    as the parent's, and the parent is the one who must `tracker log --file …` them. What
    changes is that `db.gate_reason()` can now name the author of each path.

Payload (confirmed against Claude Code 2.1.269 — hooks/HOOK_CONTRACT.md §1):
  session_id, transcript_path, cwd, hook_event_name="SubagentStop", stop_hook_active (bool),
  agent_id, agent_transcript_path, agent_type, last_assistant_message?, background_tasks,
  session_crons.

This hook never blocks. Exit 2 on SubagentStop would block the *subagent*, which would make a
tracker bug into an agent that cannot finish; the gate belongs at the parent's Stop.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hook_common as hc
import db


def main(data):
    sid = data.get("session_id") or "unknown"
    agent_id = data.get("agent_id") or None
    agent_type = data.get("agent_type") or None
    con = db.connect()
    hc.ensure_session(con, data)

    if not agent_id:
        # Nothing to attribute. Record it so the hole is visible rather than silent.
        db.add_event(con, sid, "subagent_stop", {"agent_id": None, "agent_type": agent_type,
                                                 "note": "payload carried no agent_id"})
        db.log_line("hooks", f"subagent_stop {sid[:8]} with no agent_id (type={agent_type})")
        return

    tp = data.get("agent_transcript_path") or None
    meta = hc.transcript_meta(tp) if tp else {}
    last_msg = (data.get("last_assistant_message") or meta.get("recap") or "").strip()

    rows = con.execute(
        "SELECT path, op FROM agent_files WHERE agent_id=? ORDER BY last_ts", (agent_id,)).fetchall()
    paths = [r["path"] for r in rows]
    durable = [p for p in paths if db.is_durable(p)]
    # Only the paths NOBODY has logged yet are worth saying anything about. The first version of
    # this hook listed every durable file the agent had touched, which meant an agent that had
    # already logged its work properly got told to log it again — and a reminder that fires when
    # there is nothing to do is a reminder people learn to skip. `pending_files()` is the same
    # source of truth the Stop gate uses, so the two can never disagree.
    pending = {r["path"] for r in db.pending_files(con, sid)}
    unlogged = [p for p in durable if p in pending]

    db.upsert_agent(
        con, sid, agent_id,
        agent_type=agent_type, cwd=data.get("cwd"), transcript_path=tp,
        started_at=db.now_iso(), last_message=last_msg[:1200] or None,
        recap=(meta.get("recap") or "")[:1200] or None, files=len(paths))
    con.execute("UPDATE session_agents SET ended_at=?, stops=stops+1, files=?, "
                "updated_at=? WHERE parent_session_id=? AND agent_id=?",
                (db.now_iso(), len(paths), db.now_iso(), sid, agent_id))

    db.add_event(con, sid, "subagent_stop", {
        "agent_id": agent_id, "agent_type": agent_type, "files": paths[:60],
        "n_files": len(paths), "n_durable": len(durable), "n_unlogged": len(unlogged),
        "unlogged": unlogged[:60],
        "transcript": tp, "stop_hook_active": bool(data.get("stop_hook_active")),
        "last_message": last_msg[:600] or None,
    })
    db.log_line("hooks", f"subagent_stop {sid[:8]} agent={agent_type or '?'}/{agent_id[:8]} "
                         f"files={len(paths)} durable={len(durable)} unlogged={len(unlogged)}")

    # Tell the agent, in its own thread, what is still owed. additionalContext on SubagentStop is
    # "non-error feedback delivered to the subagent" (2.1.269 schema), so this is the one place a
    # subagent can be reminded to name its own files without the parent having to
    # guess. Silent when the agent wrote nothing durable, and silent when it already logged what
    # it wrote — a nag with nothing behind it is how a real warning gets ignored.
    if unlogged:
        shown = "\n  ".join(unlogged[:8])
        extra = "" if len(unlogged) <= 8 else f"\n  (+{len(unlogged) - 8} more)"
        ctx = (f"CLAUDE TRACKER: this subagent ({agent_type or 'agent'}) wrote {len(unlogged)} "
               f"durable file(s) that nothing in the ledger claims yet:\n  {shown}{extra}\n"
               "They are pending on the parent session's gate, and the parent cannot describe "
               "work it did not do. Log them yourself, with EXPLICIT absolute paths so they are "
               "not attributed to another agent:\n"
               '  tracker log --title "..." --summary "..." --usage "..." --file '
               + " --file ".join(unlogged[:4]) + "\n")
        hc.emit({"hookSpecificOutput": {"hookEventName": "SubagentStop",
                                        "additionalContext": ctx[:hc.MAX_CONTEXT_CHARS]}})


if __name__ == "__main__":
    hc.run("subagent_stop", main)
