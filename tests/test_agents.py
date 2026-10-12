"""Adapter tests: registry, Codex log parsing (synthetic rollout), rules block round trip, scaffold.
Runs anywhere; touches only a temp DB and a temp CODEX_HOME."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="cc-agents-"))
CODEX = TMP / "codex"
ENV = {**os.environ, "CLAUDE_TRACKER_DB": str(TMP / "t.db"), "CODEX_HOME": str(CODEX), "PYTHONIOENCODING": "utf-8"}
fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def tracker(*args):
    return subprocess.run([sys.executable, str(ROOT / "tracker.py"), *args], env=ENV, capture_output=True,
                          text=True, encoding="utf-8", cwd=str(TMP))


# a synthetic Codex rollout in the documented shape
day = CODEX / "sessions" / "2026" / "10" / "01"
day.mkdir(parents=True)
work = TMP / "proj"
work.mkdir()
rec = [
    {"timestamp": "2026-10-01T10:00:00Z", "type": "session_meta",
     "payload": {"id": "abc-123", "cwd": str(work), "timestamp": "2026-10-01T10:00:00Z", "cli_version": "0.144.5"}},
    {"timestamp": "2026-10-01T10:00:01Z", "type": "turn_context", "payload": {"model": "gpt-5.5", "cwd": str(work)}},
    {"timestamp": "2026-10-01T10:00:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": "add a parser"}},
    {"timestamp": "2026-10-01T10:00:03Z", "type": "response_item",
     "payload": {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": "ls"}), "call_id": "c1"}},
    {"timestamp": "2026-10-01T10:00:04Z", "type": "response_item",
     "payload": {"type": "custom_tool_call", "name": "apply_patch",
                 "input": "*** Begin Patch\n*** Add File: parser.py\n+x = 1\n*** End Patch"}},
    {"timestamp": "2026-10-01T10:00:05Z", "type": "event_msg",
     "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 1234}},
                 "rate_limits": {"plan_type": "plus", "primary": {"used_percent": 42.0, "window_minutes": 300,
                                                                  "resets_at": 1790000000},
                                 "secondary": {"used_percent": 7.0, "window_minutes": 10080, "resets_at": 1790500000}}}},
    {"timestamp": "2026-10-01T10:00:06Z", "type": "something_new", "payload": {"x": 1}},
]
(day / "rollout-2026-10-01T10-00-00-abc-123.jsonl").write_text("\n".join(json.dumps(r) for r in rec) + "\nnot json\n")

r = tracker("agents", "--json")
names = [x["name"] for x in json.loads(r.stdout)] if r.returncode == 0 else []
check("registry lists claude, codex, gemini, generic", {"claude", "codex", "gemini", "generic"} <= set(names), r.stderr[-200:])

r = tracker("agents", "scan", "codex")
check("codex scan reads one session", "'sessions': 1" in r.stdout, r.stdout + r.stderr[-200:])
r = tracker("agents", "scan", "codex")
check("codex scan is incremental", "'skipped': 1" in r.stdout, r.stdout)

sys.path.insert(0, str(ROOT))
os.environ.update(ENV)
import db  # noqa: E402
con = db.connect()
s = con.execute("SELECT * FROM sessions WHERE session_id='codex:abc-123'").fetchone()
check("codex session fields", s is not None and s["agent"] == "codex" and s["model"] == "gpt-5.5"
      and s["user_turns"] == 1 and s["tool_calls"] == 2 and s["bash_calls"] == 1 and s["edits"] == 1,
      dict(s) if s else "missing")
lim = {r["window"]: r for r in con.execute("SELECT * FROM agent_usage WHERE agent='codex'")}
check("codex plan limits", lim.get("primary") and lim["primary"]["used_pct"] == 42.0
      and lim.get("secondary") and lim["secondary"]["window_minutes"] == 10080, lim)

r = tracker("log", "--agent", "codex", "--title", "Parser from Codex", "--summary",
            "A parser written in a Codex session, logged through the shared ledger protocol for tests.",
            "--usage", "python parser.py")
item = con.execute("SELECT agent, tags FROM items WHERE title='Parser from Codex'").fetchone()
check("tracker log --agent codex", r.returncode == 0 and item and item["agent"] == "codex"
      and "codex" in item["tags"], r.stdout + r.stderr[-300:])

(CODEX / "AGENTS.md").write_text("# mine\n")
tracker("agents", "connect", "codex")
tracker("agents", "connect", "codex")
txt = (CODEX / "AGENTS.md").read_text()
check("connect adds one block and keeps the user's text", txt.count("ai-command-center:begin") == 1 and "# mine" in txt)
tracker("agents", "disconnect", "codex")
check("disconnect removes only the block", (CODEX / "AGENTS.md").read_text().strip() == "# mine")

r = tracker("agents", "new", "zz-test-tool")
f = ROOT / "agents" / "zz_test_tool.py"
ok = r.returncode == 0 and f.exists()
r2 = tracker("agents", "--json")
check("scaffold creates a loadable adapter", ok and "zz-test-tool" in r2.stdout, r.stdout + r.stderr)
f.unlink(missing_ok=True)

print(f"\n{'ALL GREEN' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
