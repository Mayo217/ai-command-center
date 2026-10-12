#!/usr/bin/env python3
"""PostToolUse hook: capture every durable file touch.
Matchers: "Write|Edit|MultiEdit|NotebookEdit" (tool_input.file_path) and "Bash"
(tool_input.command parsed by db.bash_written_paths). Never outputs anything."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hook_common as hc
import db


def main(data):
    tool = data.get("tool_name") or ""
    con = db.connect()
    sid = hc.ensure_session(con, data)
    cwd = data.get("cwd")
    counters = {"tool_calls": 1}
    touched = []
    # A tool call made inside a subagent carries the PARENT's session_id plus its own agent_id
    # (HOOK_CONTRACT.md 1). The parent keeps the session_files row - By design, a subagent's
    # writes count as the parent's - but we also record WHO wrote it, so the Stop gate can name
    # the author instead of blocking the parent for a path it never touched.
    agent_id = data.get("agent_id") or None
    agent_type = data.get("agent_type") or None
    if agent_id:
        db.upsert_agent(con, sid, agent_id, agent_type=agent_type, cwd=cwd,
                        started_at=db.now_iso())

    def attribute(path, op, tool_name):
        if agent_id:
            db.record_agent_file(con, sid, agent_id, path, agent_type=agent_type, op=op,
                                 tool=tool_name)

    if tool in hc.WRITE_TOOLS:
        if tool in ("Write", "Update"):
            counters["writes"] = 1
        else:
            counters["edits"] = 1
        for path, op in hc.paths_from_tool(tool, data.get("tool_input") or {}, data.get("tool_response"), cwd):
            if db.is_ignored(path):
                continue
            prev = con.execute("SELECT op FROM session_files WHERE session_id=? AND path=?", (sid, path)).fetchone()
            if prev:
                op = prev["op"] if prev["op"] == "create" else "modify"
            db.touch_file(con, sid, path, op=op, tool=tool)
            attribute(path, op, tool)
            touched.append(path)
    elif tool == "Bash":
        counters["bash_calls"] = 1
        ti = data.get("tool_input") or {}
        cmd = ti.get("command") or ""
        for path, _op in db.bash_written_paths(cmd, cwd):
            if db.is_ignored(path):
                continue
            db.touch_file(con, sid, path, op="bash", tool="Bash")
            attribute(path, "bash", "Bash")
            touched.append(path)
        # Keep the command itself, not only the paths it wrote: "how did I run that last time"
        # is a question this ledger exists to answer.
        db.record_command(con, sid, cmd, cwd=cwd, description=ti.get("description"),
                          wrote=len(touched),
                          event_key=(data.get("tool_use_id") or data.get("toolUseID") or None),
                          source="hook")

    hc.bump(con, sid, **counters)
    if touched:
        con.execute("UPDATE sessions SET substantive=1 WHERE session_id=?", (sid,))
        who = f" (agent {agent_type or '?'} {agent_id[:8]})" if agent_id else ""
        db.log_line("hooks", f"post_tool_use {sid[:8]} {tool}{who} -> {', '.join(touched[:6])}")


if __name__ == "__main__":
    hc.run("post_tool_use", main)
