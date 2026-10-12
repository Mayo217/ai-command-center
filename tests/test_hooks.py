#!/usr/bin/env python3
"""Simulate every AI Command Center hook with real-shaped stdin JSON against a throwaway DB.

Run:  python3 tests/test_hooks.py          (exit 0 = all green)
Nothing here touches the real tracker.db - CLAUDE_TRACKER_DB points at a temp file.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "hooks"
sys.path.insert(0, str(ROOT))

PY = sys.executable
# Under the home folder, not the system temp folder: the tracker ignores /tmp and %TEMP% by design.
class _SlashPath(type(Path())):
    """The tracker stores paths with forward slashes on every OS (C:/Users/x/...). On Windows the
    tests print paths the same way, so commands and comparisons match what the tracker records."""
    def __str__(self):
        return super().__str__().replace("\\", "/")


TMP = _SlashPath(tempfile.mkdtemp(prefix="tracker-hooktest-", dir=Path.home()))
import atexit  # noqa: E402
import shutil  # noqa: E402
atexit.register(shutil.rmtree, TMP, True)
DB = TMP / "test.db"
WORK = TMP / "work"
WORK.mkdir()
ENV = {**os.environ, "CLAUDE_TRACKER_DB": str(DB), "NO_COLOR": "1"}
ENV.pop("CLAUDE_SESSION_ID", None)

SID = "11111111-2222-3333-4444-555555555555"
TRANSCRIPT = TMP / "transcript.jsonl"
TRANSCRIPT.write_text("\n".join([
    json.dumps({"type": "ai-title", "aiTitle": "Hook simulation session", "sessionId": SID}),
    json.dumps({"type": "cost-state", "sessionId": SID, "totalCostUSD": 1.25, "totalLinesAdded": 40,
                "totalLinesRemoved": 3,
                "modelUsage": {"claude-opus-5[1m]": {"costUSD": 1.2}, "claude-haiku-4-5": {"costUSD": 0.05}}}),
]) + "\n")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond, detail=""):
    RESULTS.append((name, bool(cond), "" if cond else str(detail)))
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"\n          -> {detail}"))


def base(event: str, **extra) -> dict:
    d = {"session_id": SID, "transcript_path": str(TRANSCRIPT), "cwd": str(WORK),
         "hook_event_name": event, "permission_mode": "bypassPermissions"}
    d.update(extra)
    return d


def hook(script: str, payload: dict):
    p = subprocess.run([PY, str(HOOKS / script)], input=json.dumps(payload), capture_output=True,
                       text=True, env=ENV, timeout=20)
    return p.returncode, p.stdout, p.stderr


def cli(*args, expect=None):
    p = subprocess.run([PY, str(ROOT / "tracker.py"), *args], capture_output=True, text=True, env=ENV, timeout=20)
    if expect is not None and p.returncode != expect:
        print(f"    (cli {args} rc={p.returncode} out={p.stdout[:400]} err={p.stderr[:400]})")
    return p


def con():
    import importlib
    import db as _db
    importlib.reload(_db)
    return _db.connect(DB)


def q(sql, args=()):
    c = con()
    rows = c.execute(sql, args).fetchall()
    c.close()
    return rows


# ------------------------------------------------------------------ 1. SessionStart
print("\n[1] SessionStart")
rc, out, err = hook("session_start.py", base("SessionStart", source="startup", model="claude-opus-5[1m]"))
check("session_start exits 0", rc == 0, f"rc={rc} err={err}")
try:
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
except Exception as e:
    ctx = ""
    check("session_start emits additionalContext", False, f"{e}: {out[:200]}")
check("additionalContext is the tracker briefing", "CLAUDE TRACKER (mandatory)" in ctx and "tracker log" in ctx, ctx[:200])
check("additionalContext <= 1800 chars", len(ctx) <= 1800, len(ctx))
check("session row created", len(q("SELECT 1 FROM sessions WHERE session_id=?", (SID,))) == 1)
check("session_start event recorded", len(q("SELECT 1 FROM session_events WHERE session_id=? AND event='session_start'", (SID,))) == 1)

# ------------------------------------------------------------------ 2. UserPromptSubmit
print("\n[2] UserPromptSubmit")
hook("user_prompt_submit.py", base("UserPromptSubmit", prompt="build me a thing that does X", prompt_id="p1"))
hook("user_prompt_submit.py", base("UserPromptSubmit", prompt="now make it faster", prompt_id="p2"))
s = q("SELECT * FROM sessions WHERE session_id=?", (SID,))[0]
check("user_turns counted", s["user_turns"] == 2, s["user_turns"])
check("first_prompt kept from the first turn", s["first_prompt"] == "build me a thing that does X", s["first_prompt"])

# ------------------------------------------------------------------ 3. PostToolUse
print("\n[3] PostToolUse capture")
made = WORK / "thing.py"
made.write_text("print('hi')\n")
rc, out, err = hook("post_tool_use.py", base("PostToolUse", tool_name="Write",
                                             tool_input={"file_path": str(made), "content": "print('hi')"},
                                             tool_response="File created successfully at: " + str(made),
                                             tool_use_id="t1"))
check("post_tool_use (Write) exits 0 silently", rc == 0 and out == "", f"rc={rc} out={out!r}")
edited = WORK / "notes.md"
edited.write_text("# notes\n")
hook("post_tool_use.py", base("PostToolUse", tool_name="Edit",
                              tool_input={"file_path": str(edited), "old_string": "a", "new_string": "b"},
                              tool_response={"filePath": str(edited), "structuredPatch": []}, tool_use_id="t2"))
scratch = "/private/tmp/claude-501/-Users-you-Desktop/abc/scratchpad/junk.py"
hook("post_tool_use.py", base("PostToolUse", tool_name="Write", tool_input={"file_path": scratch},
                              tool_response="File created", tool_use_id="t3"))
bash_target = WORK / "report.md"
hook("post_tool_use.py", base("PostToolUse", tool_name="Bash",
                              tool_input={"command": f"echo hello > {bash_target} && cat {made} | tee /tmp/ignored.txt"},
                              tool_response="", tool_use_id="t4"))
hook("post_tool_use.py", base("PostToolUse", tool_name="Bash",
                              tool_input={"command": "python3 -c \"x=5\nif x > 0: print(1)\" && node -e 'a.sort((a,b)=>a.len-b.len)'"},
                              tool_response="", tool_use_id="t4b"))
sub = WORK / "sub"
sub.mkdir(exist_ok=True)
hook("post_tool_use.py", base("PostToolUse", tool_name="Bash",
                              tool_input={"command": f"cd {sub} && echo x > inner.md"},
                              tool_response="", tool_use_id="t4c"))
files = {r["path"]: r for r in q("SELECT * FROM session_files WHERE session_id=?", (SID,))}
check("relative path resolved against the command's `cd`, not the hook cwd",
      str(sub / "inner.md") in files and str(WORK / "inner.md") not in files, list(files))
check("no phantom paths from `if x > 0` / JS arrows (db.plausible_path)",
      not any(f.endswith("/0") or "a.len-b.len" in f for f in files), list(files))
check("Write captured as create", files.get(str(made)) and files[str(made)]["op"] == "create", list(files))
check("Edit captured as modify", files.get(str(edited)) and files[str(edited)]["op"] == "modify", list(files))
check("Bash redirect captured", str(bash_target) in files, list(files))
check("scratchpad path NOT captured", scratch not in files and "/tmp/ignored.txt" not in files, list(files))
s = q("SELECT * FROM sessions WHERE session_id=?", (SID,))[0]
check("counters bumped (6 tools, 2 writes, 1 edit, 3 bash)",
      (s["tool_calls"], s["writes"], s["edits"], s["bash_calls"]) == (6, 2, 1, 3),
      (s["tool_calls"], s["writes"], s["edits"], s["bash_calls"]))

# ------------------------------------------------------------------ 4. Stop gate blocks
print("\n[4] Stop gate blocks")
rc, out, err = hook("stop_gate.py", base("Stop", stop_hook_active=False, last_assistant_message="done"))
check("stop gate blocks (exit 2)", rc == 2, f"rc={rc} out={out[:200]} err={err[:200]}")
payload = json.loads(out) if out.strip().startswith("{") else {}
check("block reason on stdout JSON", payload.get("decision") == "block" and "TRACKER GATE" in payload.get("reason", ""),
      out[:300])
check("block reason names the pending files",
      str(made) in payload.get("reason", "") and str(edited) in payload.get("reason", ""), payload.get("reason", "")[:400])
check("block reason tells it how to log/waive",
      "tracker log --title" in payload.get("reason", "") and "tracker waive --reason" in payload.get("reason", ""))
check("block reason <= 1200 chars", len(payload.get("reason", "")) <= 1200, len(payload.get("reason", "")))
check("reason also on stderr (exit-2 channel)", "TRACKER GATE" in err, err[:200])
check("gate_blocks incremented", q("SELECT gate_blocks FROM sessions WHERE session_id=?", (SID,))[0][0] == 1)
check("gate_block event recorded", len(q("SELECT 1 FROM session_events WHERE session_id=? AND event='gate_block'", (SID,))) == 1)

# ------------------------------------------------------------------ 5. waive refused, then log
print("\n[5] waive refusal + log auto-attach")
p = cli("waive", "--reason", "nothing durable here at all, just poking around the repo", "--session", SID)
check("waive refused while a .py is pending", p.returncode == 1 and "refused" in p.stderr, f"rc={p.returncode} {p.stderr[:200]}")
check("refusal names the durable file", str(made) in p.stderr, p.stderr[:300])

p = cli("log", "--session", SID, "--title", "Thing that does X",
        "--summary", "A small script that does X and the note next to it, built so the hook test has a real item to attach files to.",
        "--usage", "python3 thing.py")
check("log succeeds", p.returncode == 0 and "logged item #" in p.stdout, p.stdout + p.stderr)
check("log auto-attached the pending files", "auto-attached 4 file(s)" in p.stdout, p.stdout)
rows = q("SELECT * FROM session_files WHERE session_id=? AND logged_item_id IS NULL AND ignored=0", (SID,))
check("no pending files left after log", len(rows) == 0, [r["path"] for r in rows])
check("session marked logged", q("SELECT logged FROM sessions WHERE session_id=?", (SID,))[0][0] == 1)

gc = json.loads(cli("gate-check", "--session", SID).stdout)
check("gate-check says clear", gc["would_block"] is False and gc["pending_count"] == 0, gc)

rc, out, err = hook("stop_gate.py", base("Stop", stop_hook_active=False))
check("stop gate now allows the handback", rc == 0 and out == "", f"rc={rc} out={out[:200]}")

# waive is accepted for pure data churn
data_dir = WORK / "data"
data_dir.mkdir()
csv = data_dir / "prices.csv"
csv.write_text("a,b\n1,2\n")
hook("post_tool_use.py", base("PostToolUse", tool_name="Bash",
                              tool_input={"command": f"echo 'a,b' > {csv}"}, tool_response="", tool_use_id="t5"))
p = cli("waive", "--reason", "Only refreshed the prices csv data file; nothing new to know about.", "--session", SID)
check("waive accepted for a data-only touch", p.returncode == 0 and "waived" in p.stdout, p.stdout + p.stderr)
rc, out, err = hook("stop_gate.py", base("Stop", stop_hook_active=False))
check("gate clear after waive", rc == 0, f"rc={rc} {out[:200]}")

# ------------------------------------------------------------------ 6. stop_hook_active bypass
print("\n[6] stop_hook_active bypass -> inferred item")
SID2 = "bypass-6666-7777-8888-999999999999"
hook("session_start.py", {**base("SessionStart", source="startup"), "session_id": SID2})
f2 = WORK / "bypass_thing.py"
f2.write_text("x=1\n")
hook("post_tool_use.py", {**base("PostToolUse", tool_name="Write", tool_input={"file_path": str(f2)},
                                 tool_response="File created successfully at: " + str(f2)), "session_id": SID2})
rc, out, err = hook("stop_gate.py", {**base("Stop", stop_hook_active=True), "session_id": SID2})
check("bypass exits 0 (never wedges Claude)", rc == 0 and out == "", f"rc={rc} out={out[:200]}")
check("gate_bypassed recorded", q("SELECT gate_bypassed FROM sessions WHERE session_id=?", (SID2,))[0][0] == 1)
check("gate_bypass event recorded", len(q("SELECT 1 FROM session_events WHERE session_id=? AND event='gate_bypass'", (SID2,))) == 1)
inf = q("SELECT * FROM items WHERE session_id=? AND confidence='inferred'", (SID2,))
check("inferred item auto-created", len(inf) == 1, [dict(r) for r in inf])
if inf:
    check("inferred item carries the transcript title", inf[0]["title"] == "Hook simulation session", inf[0]["title"])
    check("inferred item attached the file",
          len(q("SELECT 1 FROM item_files WHERE item_id=? AND path=?", (inf[0]["id"], str(f2)))) == 1)
    check("inferred item is wip (needs curation)", inf[0]["status"] == "wip", inf[0]["status"])
# The bypass item deliberately does NOT claim the files:
# a subagent may still be mid-write and must be able to log them itself. The paths are recorded
# on the auto item as extra.unclaimed_paths and released when a real item claims them.
check("bypass leaves the paths unclaimed for the real author",
      len(q("SELECT 1 FROM session_files WHERE session_id=? AND ignored=0 AND logged_item_id IS NULL", (SID2,))) == 1)
if inf:
    _extra = json.loads(inf[0]["extra"] or "{}")
    check("bypass item records extra.unclaimed_paths",
          _extra.get("unclaimed_paths") == [str(f2)], _extra.get("unclaimed_paths"))
    _p = cli("log", "--session", SID2, "--title", "Bypass thing script",
             "--summary", "The real author logging the file the gate bypass only recorded, "
                          "to prove an unclaimed path can still be claimed properly.",
             "--file", str(f2), expect=0)
    check("the real author can still log the bypassed file", _p.returncode == 0, _p.stderr[:200])
    check("claiming the file clears it from session_files",
          len(q("SELECT 1 FROM session_files WHERE session_id=? AND ignored=0 AND logged_item_id IS NULL", (SID2,))) == 0)
    _extra2 = json.loads(q("SELECT extra FROM items WHERE id=?", (inf[0]["id"],))[0][0] or "{}")
    check("claiming the file clears extra.unclaimed_paths",
          not _extra2.get("unclaimed_paths"), _extra2.get("unclaimed_paths"))

# ------------------------------------------------------------------ 7. three-block cap
print("\n[7] three-block cap")
SID3 = "cap00000-1111-2222-3333-444444444444"
hook("session_start.py", {**base("SessionStart"), "session_id": SID3})
f3 = WORK / "capped.py"
f3.write_text("y=2\n")
hook("post_tool_use.py", {**base("PostToolUse", tool_name="Write", tool_input={"file_path": str(f3)},
                                 tool_response="File created successfully at: " + str(f3)), "session_id": SID3})
codes = []
for _ in range(4):
    rc, out, err = hook("stop_gate.py", {**base("Stop", stop_hook_active=False), "session_id": SID3})
    codes.append(rc)
check("blocks 3 times then lets go", codes == [2, 2, 2, 0], codes)
check("gate_blocks capped at 3", q("SELECT gate_blocks FROM sessions WHERE session_id=?", (SID3,))[0][0] == 3)
check("cap event recorded", len(q("SELECT 1 FROM session_events WHERE session_id=? AND event='gate_cap'", (SID3,))) == 1)

# ------------------------------------------------------------------ 8. SessionEnd
print("\n[8] SessionEnd")
rc, out, err = hook("session_end.py", {**base("SessionEnd", reason="clear"), "session_id": SID3})
check("session_end exits 0", rc == 0, f"rc={rc} {err[:200]}")
s3 = q("SELECT * FROM sessions WHERE session_id=?", (SID3,))[0]
check("ended_at + reason stored", bool(s3["ended_at"]) and s3["end_reason"] == "clear", dict(s3))
check("cost pulled from transcript cost-state", s3["cost_usd"] == 1.25, s3["cost_usd"])
check("model pulled from modelUsage", s3["model"] == "claude-opus-5[1m]", s3["model"])
check("title pulled from ai-title", s3["title"] == "Hook simulation session", s3["title"])
check("unlogged work auto-captured at session end",
      len(q("SELECT 1 FROM items WHERE session_id=? AND confidence='inferred'", (SID3,))) == 1)


# --------------------------------------------------- 8b. SubagentStop (the subagent hole)
# A subagent's PostToolUse carries the PARENT's session_id and its
# own agent_id, so its writes used to be indistinguishable from the parent's own work.
print("\n[8b] SubagentStop — subagent attribution")
SID4 = "agent000-1111-2222-3333-444444444444"
AGENT = "ag-aaaabbbbccccdddd"
AGENT2 = "ag-eeeeffff00001111"
hook("session_start.py", {**base("SessionStart"), "session_id": SID4})

parent_file = WORK / "parent_wrote.py"
parent_file.write_text("parent = 1\n")
hook("post_tool_use.py", {**base("PostToolUse", tool_name="Write",
                                 tool_input={"file_path": str(parent_file)},
                                 tool_response="File created successfully at: " + str(parent_file)),
                          "session_id": SID4})
agent_file = WORK / "agent_wrote.py"
agent_file.write_text("agent = 1\n")
hook("post_tool_use.py", {**base("PostToolUse", tool_name="Write",
                                 tool_input={"file_path": str(agent_file)},
                                 tool_response="File created successfully at: " + str(agent_file)),
                          "session_id": SID4, "agent_id": AGENT, "agent_type": "general-purpose"})
agent_bash = WORK / "agent_report.md"
agent_bash.write_text("hi\n")  # the simulated command really would create it - the gate ignores paths not on disk
hook("post_tool_use.py", {**base("PostToolUse", tool_name="Bash",
                                 tool_input={"command": f"echo hi > {agent_bash}"}, tool_response=""),
                          "session_id": SID4, "agent_id": AGENT, "agent_type": "general-purpose"})

af = {r["path"]: dict(r) for r in q("SELECT * FROM agent_files WHERE parent_session_id=?", (SID4,))}
check("subagent's Write attributed to the agent", str(agent_file) in af, list(af))
check("subagent's Bash write attributed to the agent", str(agent_bash) in af, list(af))
check("the parent's own write is NOT attributed to an agent", str(parent_file) not in af, list(af))
check("agent_type recorded on the file row",
      af.get(str(agent_file), {}).get("agent_type") == "general-purpose", af.get(str(agent_file)))
check("the parent still owns every path",
      {r["path"] for r in q("SELECT path FROM session_files WHERE session_id=? AND ignored=0", (SID4,))}
      >= {str(parent_file), str(agent_file), str(agent_bash)})
check("session_agents row opened at first write",
      len(q("SELECT 1 FROM session_agents WHERE parent_session_id=? AND agent_id=?", (SID4, AGENT))) == 1)

AGENT_TRANSCRIPT = TMP / "agent.jsonl"
AGENT_TRANSCRIPT.write_text(json.dumps({"type": "ai-title", "aiTitle": "the subagent run"}) + "\n")
rc, out, err = hook("subagent_stop.py", {
    **base("SubagentStop"), "session_id": SID4, "agent_id": AGENT,
    "agent_type": "general-purpose", "agent_transcript_path": str(AGENT_TRANSCRIPT),
    "stop_hook_active": False,
    "last_assistant_message": "Wrote agent_wrote.py and a report. Done.",
    "background_tasks": [], "session_crons": []})
check("subagent_stop exits 0 (never blocks a subagent)", rc == 0, f"rc={rc} err={err[:200]}")
ag = q("SELECT * FROM session_agents WHERE parent_session_id=? AND agent_id=?", (SID4, AGENT))
check("session_agents row closed", bool(ag) and bool(ag[0]["ended_at"]), [dict(r) for r in ag])
check("session_agents counts the agent's files", bool(ag) and ag[0]["files"] == 2,
      ag[0]["files"] if ag else None)
check("session_agents keeps last_assistant_message",
      bool(ag) and "agent_wrote.py" in (ag[0]["last_message"] or ""), ag[0]["last_message"] if ag else None)
ev = q("SELECT payload FROM session_events WHERE session_id=? AND event='subagent_stop'", (SID4,))
check("subagent_stop event recorded", len(ev) == 1, len(ev))
if ev:
    pl = json.loads(ev[0][0])
    check("event names the agent and its files",
          pl["agent_type"] == "general-purpose" and str(agent_file) in pl["files"], pl)
try:
    actx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
except Exception as e:
    actx = ""
    check("subagent_stop emits additionalContext to the subagent", False, f"{e}: {out[:200]}")
check("additionalContext names the agent's own files and tracker log",
      str(agent_file) in actx and "tracker log" in actx, actx[:300])
check("additionalContext declares hookEventName SubagentStop",
      json.loads(out).get("hookSpecificOutput", {}).get("hookEventName") == "SubagentStop", out[:200])

rc, out, err = hook("stop_gate.py", {**base("Stop", stop_hook_active=False), "session_id": SID4})
check("parent is still gated for the subagent's files", rc == 2, f"rc={rc}")
reason = json.loads(out).get("reason", "") if out.strip().startswith("{") else ""
check("the gate message names the SUBAGENT as the author",
      "written by subagent general-purpose" in reason, reason[:500])
check("the block message lists the agent's files as unlogged",
      str(agent_file) in reason and str(agent_bash) in reason, reason[:600])
check("the gate message does not blame the agent for the parent's own file",
      reason.count("written by subagent") == 2, reason[:500])
check("the gate message explains subagent files count as yours",
      "count as yours" in reason, reason[:500])
check("gate reason still <= 1200 chars with authorship", len(reason) <= 1200, len(reason))

rc, out, err = hook("subagent_stop.py", {**base("SubagentStop"), "session_id": SID4,
                                         "agent_type": "explorer"})
check("subagent_stop with no agent_id still exits 0", rc == 0, f"rc={rc} {err[:200]}")
check("a payload with no agent_id is recorded, not dropped",
      len(q("SELECT 1 FROM session_events WHERE session_id=? AND event='subagent_stop'", (SID4,))) == 2)
rc, out, err = hook("subagent_stop.py", {**base("SubagentStop"), "session_id": SID4,
                                         "agent_id": AGENT2, "agent_type": "Explore",
                                         "stop_hook_active": False})
check("an agent that wrote nothing emits no context", rc == 0 and out == "", f"rc={rc} out={out[:150]}")

# The nag must fire ONLY while the work is actually unlogged. The first version of this hook
# listed every durable file the agent had touched, so an agent that had already logged its work
# properly was told to log it again — observed for real on the session that built this hook.
# A reminder that fires when there is nothing to do is one people learn to skip.
_p = cli("log", "--session", SID4, "--title", "What the subagent wrote",
         "--summary", "The agent's own files, logged with explicit paths as intended, "
                      "so the next SubagentStop has nothing left to ask for.",
         "--file", str(agent_file), "--file", str(agent_bash), expect=0)
check("the subagent's files can be logged with explicit paths", _p.returncode == 0, _p.stderr[:200])
rc, out, err = hook("subagent_stop.py", {
    **base("SubagentStop"), "session_id": SID4, "agent_id": AGENT,
    "agent_type": "general-purpose", "agent_transcript_path": str(AGENT_TRANSCRIPT),
    "stop_hook_active": False, "last_assistant_message": "done"})
check("once the work is logged the hook stops nagging", rc == 0 and out == "",
      f"it asked again for files that are already logged: {out[:300]!r}")
_ev = json.loads(q("SELECT payload FROM session_events WHERE session_id=? AND event='subagent_stop' "
                   "ORDER BY id DESC LIMIT 1", (SID4,))[0][0])
check("the event still records what the agent wrote", _ev["n_durable"] == 2, _ev)
check("but records nothing left unlogged", _ev["n_unlogged"] == 0 and _ev["unlogged"] == [], _ev)

# ------------------------------------------------------------------ 8c. PreCompact
print("\n[8c] PreCompact — the moment a session loses its memory")
rc, out, err = hook("pre_compact.py", {**base("PreCompact"), "session_id": SID4,
                                       "trigger": "auto", "custom_instructions": None})
check("pre_compact exits 0", rc == 0, f"rc={rc} err={err[:200]}")
ce = q("SELECT payload FROM session_events WHERE session_id=? AND event='compact'", (SID4,))
check("compact event recorded", len(ce) == 1, len(ce))
if ce:
    pl = json.loads(ce[0][0])
    check("compact event records the trigger", pl["trigger"] == "auto", pl.get("trigger"))
    # By this point the subagent's two files have been logged (section 8b), so the only thing
    # still pending is the file the PARENT wrote itself. The compact event must record exactly
    # the state at that instant, not a stale list.
    check("compact event records what was pending at that moment",
          pl["n_pending"] == 1 and pl["pending_files"] == [str(parent_file)],
          pl.get("pending_files"))
    check("compact event carries the session title from the transcript",
          pl.get("title") == "Hook simulation session", pl.get("title"))
rc, out, err = hook("pre_compact.py", {**base("PreCompact"), "session_id": SID4,
                                       "trigger": "manual",
                                       "custom_instructions": "keep the launchd details"})
check("manual compaction recorded separately",
      len(q("SELECT 1 FROM session_events WHERE session_id=? AND event='compact'", (SID4,))) == 2)
pl2 = json.loads(q("SELECT payload FROM session_events WHERE session_id=? AND event='compact' "
                   "ORDER BY id DESC LIMIT 1", (SID4,))[0][0])
check("custom_instructions kept", pl2.get("custom_instructions") == "keep the launchd details", pl2)
check("pre_compact writes nothing to stdout (no output channel on this event)", out == "", out[:150])

# ------------------------------------------------------------------ 8d. Notification
print("\n[8d] Notification — a session waiting on a human")
rc, out, err = hook("notification.py", {**base("Notification"), "session_id": SID4,
                                        "message": "Claude needs your permission to use Bash",
                                        "title": "Claude Code",
                                        "notification_type": "agent_needs_input"})
check("notification exits 0 silently", rc == 0 and out == "", f"rc={rc} out={out[:150]}")
ne = q("SELECT payload FROM session_events WHERE session_id=? AND event='notification'", (SID4,))
check("notification event recorded", len(ne) == 1, len(ne))
if ne:
    pl = json.loads(ne[0][0])
    check("notification_type kept", pl["notification_type"] == "agent_needs_input", pl)
    check("needs_human flagged for agent_needs_input", pl["needs_human"] is True, pl)
hook("notification.py", {**base("Notification"), "session_id": SID4, "message": "finished",
                         "notification_type": "agent_completed"})
pl = json.loads(q("SELECT payload FROM session_events WHERE session_id=? AND event='notification' "
                  "ORDER BY id DESC LIMIT 1", (SID4,))[0][0])
check("an informational notification is not flagged as needing a human", pl["needs_human"] is False, pl)

# ------------------------------------------------------------------ 8e. PermissionRequest
print("\n[8e] PermissionRequest — observed, never decided")
rc, out, err = hook("permission_request.py", {
    **base("PermissionRequest"), "session_id": SID4, "tool_name": "Bash",
    "tool_input": {"command": "rm -rf /tmp/whatever", "description": "clean up"},
    "permission_suggestions": [{"type": "addRules", "rules": [{"toolName": "Bash"}]}]})
check("permission_request exits 0", rc == 0, f"rc={rc} err={err[:200]}")
check("permission_request returns NO decision (empty stdout)", out == "",
      f"a tracker hook must never allow or deny a tool: {out[:200]!r}")
pe = q("SELECT payload FROM session_events WHERE session_id=? AND event='permission_request'", (SID4,))
check("permission_request event recorded", len(pe) == 1, len(pe))
if pe:
    pl = json.loads(pe[0][0])
    check("the ask is readable in the event",
          pl["tool_name"] == "Bash" and "rm -rf /tmp/whatever" in pl["brief"], pl)
    check("permission_suggestions counted", pl["n_suggestions"] == 1, pl)

# ------------------------------------------------------------------ 8f. paths with spaces
# Regression: `cd ~/Desktop/My\\ Apps/Demo && cat > Sources/Main.swift` used to harvest
# the phantom `/Users/you/Desktop/My\\/Main.swift` and block a handback for an already-logged file.
print("\n[8f] escaped/quoted paths + the not-on-disk guard")
SIDP = "spaces1-2222-3333-4444-555555555555"
hook("session_start.py", {**base("SessionStart", source="startup"), "session_id": SIDP})
spaced = WORK / "My Apps" / "Demo" / "Sources"
spaced.mkdir(parents=True)
flat = spaced / "Main.swift"
flat.write_text("// swift\n")
esc = str(WORK).replace(" ", "\\ ") + "/My\\ Apps/Demo"
hook("post_tool_use.py", {**base("PostToolUse", tool_name="Bash", tool_use_id="tp1", tool_response="",
                                 tool_input={"command": f"cd {esc} && cat > Sources/Main.swift <<'EOF'\n// swift\nEOF"}),
                          "session_id": SIDP})
rows = [r["path"] for r in q("SELECT path FROM session_files WHERE session_id=?", (SIDP,))]
check("escaped space in `cd` resolves to the real path (no backslash phantom)", rows == [str(flat)], rows)
quoted = spaced / "Quoted.swift"
quoted.write_text("// swift\n")
hook("post_tool_use.py", {**base("PostToolUse", tool_name="Bash", tool_use_id="tp2", tool_response="",
                                 tool_input={"command": f'cat > "{quoted}" <<EOF\n// swift\nEOF'}),
                          "session_id": SIDP})
rows = sorted(r["path"] for r in q("SELECT path FROM session_files WHERE session_id=?", (SIDP,)))
check("quoted path containing a space captured", rows == sorted([str(flat), str(quoted)]), rows)
rc, out, err = hook("stop_gate.py", {**base("Stop", stop_hook_active=False), "session_id": SIDP})
check("real unlogged files in a spaced directory still BLOCK", rc == 2, f"rc={rc} out={out[:200]}")

c = con()
c.execute("DELETE FROM session_files WHERE session_id=?", (SIDP,))
for path, op, tool in ((str(WORK) + "/My\\/Main.swift", "bash", "Bash"),      # parser debris, not on disk
                       (str(spaced / "Gone.swift"), "create", "Write")):        # written then deleted
    c.execute("INSERT INTO session_files(session_id,path,op,tool,first_ts,last_ts,count,ignored) VALUES("
              "?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now'),1,0)",
              (SIDP, path, op, tool))
c.commit()
gc = json.loads(cli("gate-check", "--session", SIDP).stdout)
check("a Bash-harvested path that is not on disk is not pending",
      len(gc["phantom_pending"]) == 1 and "My\\" in gc["phantom_pending"][0], gc.get("phantom_pending"))
check("a Write/Edit file deleted this session is still pending",
      [p["path"] for p in gc["pending"]] == [str(spaced / "Gone.swift")], gc["pending"])
c = con()
c.execute("DELETE FROM session_files WHERE session_id=? AND tool='Write'", (SIDP,))
c.commit()
gc = json.loads(cli("gate-check", "--session", SIDP).stdout)
check("phantom alone leaves the gate clear", gc["would_block"] is False and gc["pending_count"] == 0, gc)
rc, out, err = hook("stop_gate.py", {**base("Stop", stop_hook_active=False), "session_id": SIDP})
check("phantom alone does not block the handback", rc == 0 and out == "", f"rc={rc} out={out[:200]}")

# ------------------------------------------------------------------ 9. robustness
print("\n[9] robustness")
for script in ["session_start.py", "user_prompt_submit.py", "post_tool_use.py", "stop_gate.py",
               "session_end.py", "subagent_stop.py", "pre_compact.py", "notification.py",
               "permission_request.py"]:
    p = subprocess.run([PY, str(HOOKS / script)], input="not json at all", capture_output=True, text=True, env=ENV)
    check(f"{script} survives garbage stdin", p.returncode == 0, f"rc={p.returncode} {p.stderr[:150]}")
p = subprocess.run([PY, str(HOOKS / "post_tool_use.py")], input="", capture_output=True, text=True, env=ENV)
check("post_tool_use survives empty stdin", p.returncode == 0, p.stderr[:150])
for script in ["subagent_stop.py", "pre_compact.py", "notification.py", "permission_request.py"]:
    p = subprocess.run([PY, str(HOOKS / script)], input="", capture_output=True, text=True, env=ENV)
    check(f"{script} survives empty stdin", p.returncode == 0, p.stderr[:150])
    p = subprocess.run([PY, str(HOOKS / script)], input=json.dumps({"session_id": None, "cwd": 42}),
                       capture_output=True, text=True, env=ENV)
    check(f"{script} survives wrong-typed fields", p.returncode == 0, p.stderr[:150])

# ------------------------------------------------------------------ timings
print("\n[timing]")
for cmd in (["status", "--session", SID], ["gate-check", "--session", SID], ["recent", "--cwd", str(WORK)]):
    t0 = time.time()
    cli(*cmd)
    print(f"  tracker {' '.join(cmd)}: {(time.time() - t0) * 1000:.0f} ms")
t0 = time.time()
hook("stop_gate.py", base("Stop", stop_hook_active=False))
print(f"  stop_gate hook (clean session): {(time.time() - t0) * 1000:.0f} ms")

failed = [r for r in RESULTS if not r[1]]
print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
if failed:
    print("FAILED:")
    for n, _, d in failed:
        print(f"  - {n}: {d}")
    sys.exit(1)
shutil.rmtree(TMP, ignore_errors=True)
print("test_hooks: ALL GREEN")
