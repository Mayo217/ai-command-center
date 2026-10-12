#!/usr/bin/env python3
"""Stop hook: the gate. Refuses the handback while durable file changes are unlogged.

Blocks iff  pending >= 1  AND  not stop_hook_active  AND  blocks_this_session < max (3).
Block channel (see hooks/HOOK_CONTRACT.md): the reason goes out on BOTH supported channels -
JSON on stdout ({"decision":"block","reason":...} plus hookSpecificOutput) and stderr with
exit 2, which the 2.1.x docs guarantee blocks a Stop. Everything else exits 0.
"""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hook_common as hc
import db

# A session whose subagents are still writing gets its gate deferred - but only so often. After
# this many deferrals it is gated normally, so a long-running agent cannot become a way out.
MAX_DEFERRALS = 5


def main(data):
    sid = data.get("session_id") or "unknown"
    con = db.connect()
    hc.ensure_session(con, data)
    stop_active = bool(data.get("stop_hook_active"))
    gs = db.gate_state(con, sid)
    pend = gs["pending"]

    if not pend:
        return  # clean handback

    if (db.config().get("gate") or {}).get("enabled", True) is False:
        # config.json gate.enabled = false: keep recording, never block.
        db.add_event(con, sid, "gate_off", {"n": len(pend)})
        return

    paths = [p["path"] for p in pend]

    # A parent whose subagents are still writing is not an unlogged session yet. Defer, record it,
    # and gate again at the next handback once the agents are done.
    if not stop_active:
        deferrals = con.execute(
            "SELECT count(*) n FROM session_events WHERE session_id=? AND event='gate_deferred'",
            (sid,)).fetchone()["n"]
        agent = hc.subagent_active(data) if deferrals < MAX_DEFERRALS else None
        if agent:
            db.add_event(con, sid, "gate_deferred",
                         {"files": paths, "n": len(paths), "subagent_transcript": agent,
                          "deferral": deferrals + 1})
            db.log_line("hooks", f"stop_gate {sid[:8]} DEFERRED ({len(paths)} files) - subagent "
                                 f"active: {agent}")
            return

    if stop_active:
        # We already blocked once and Claude is stopping again inside the gate loop: never wedge it.
        con.execute("UPDATE sessions SET gate_bypassed=gate_bypassed+1, updated_at=? WHERE session_id=?",
                    (db.now_iso(), sid))
        db.add_event(con, sid, "gate_bypass", {"files": paths, "reason": "stop_hook_active"})
        # claim=False: the paths may still belong to a subagent that will log them itself.
        item_id = hc.create_inferred_item(con, sid, data, "stop_hook_active", claim=False)
        db.log_line("hooks", f"stop_gate {sid[:8]} BYPASS (stop_hook_active) {len(paths)} files -> item #{item_id}")
        return

    if gs["blocks"] >= gs["max_blocks"]:
        db.add_event(con, sid, "gate_cap", {"files": paths, "blocks": gs["blocks"]})
        db.log_line("hooks", f"stop_gate {sid[:8]} CAP reached ({gs['blocks']}), allowing stop")
        return

    authors = db.file_authors(con, sid)
    reason = db.gate_reason(paths, gs.get("project"), authors)
    if len(reason) > hc.MAX_REASON_CHARS:
        reason = reason[:hc.MAX_REASON_CHARS - 3] + "..."
    con.execute("UPDATE sessions SET gate_blocks=gate_blocks+1, updated_at=? WHERE session_id=?",
                (db.now_iso(), sid))
    db.add_event(con, sid, "gate_block", {"files": paths, "n": len(paths), "block": gs["blocks"] + 1,
                                          "by_subagent": sorted(p for p in paths if p in authors)})
    db.log_line("hooks", f"stop_gate {sid[:8]} BLOCK #{gs['blocks'] + 1} on {len(paths)} files")

    sys.stdout.write(json.dumps({
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {"hookEventName": "Stop", "shouldContinue": True, "reason": reason},
    }))
    sys.stdout.flush()
    print(reason, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    hc.run("stop_gate", main)
