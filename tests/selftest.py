"""Cross-platform self-test (Windows and macOS). Touches nothing real: a throwaway DB and a free port.

    python tests/selftest.py          # or: tracker doctor --full
"""
import json
import os
import py_compile
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="cctest-"))
DB = TMP / "selftest.db"
ENV = {**os.environ, "CLAUDE_TRACKER_DB": str(DB), "NO_COLOR": "1", "PYTHONIOENCODING": "utf-8"}
HOOKS = ["session_start", "user_prompt_submit", "post_tool_use", "stop_gate", "session_end",
         "subagent_stop", "pre_compact", "notification", "permission_request", "statusline"]
fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def hook(name, payload):
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(ROOT / "hooks" / f"{name}.py")], input=data,
                          capture_output=True, text=True, env=ENV, timeout=30, encoding="utf-8")


# 1. every .py compiles
bad = []
for p in ROOT.rglob("*.py"):
    try:
        py_compile.compile(str(p), cfile=str(TMP / "c.pyc"), doraise=True)
    except py_compile.PyCompileError as e:
        bad.append(f"{p.name}: {e.msg[:80]}")
check("compile all .py", not bad, "; ".join(bad))

# 2. migrate a temp DB
os.environ["CLAUDE_TRACKER_DB"] = str(DB)
import db  # noqa: E402
con = db.connect()
db.migrate(con)
check("migrate temp DB", con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() is not None)

# 3. garbage stdin never breaks a hook (exit 0, or 2 only for a real gate block)
for h in HOOKS:
    for junk in ("", "not json", "{}", "[1,2]"):
        r = hook(h, junk)
        check(f"hook {h} survives {junk!r}", r.returncode == 0, f"rc={r.returncode} {r.stderr[-200:]}")

# 4. the real flow: write a file -> gate blocks -> log it -> gate passes
sid = "selftest-" + str(int(time.time()))
# The tracker ignores system temp folders (/tmp, %TEMP%) on purpose, so the file the gate must
# notice lives in a throwaway folder under the home folder, removed at the end.
import atexit  # noqa: E402
import shutil  # noqa: E402
work = Path(tempfile.mkdtemp(prefix="cc-selftest-", dir=Path.home()))
atexit.register(shutil.rmtree, work, True)
f = work / "thing.py"
f.write_text("print('hi')\n")
base = {"session_id": sid, "cwd": str(work), "transcript_path": str(TMP / "t.jsonl")}
hook("session_start", {**base, "hook_event_name": "SessionStart", "source": "startup"})
hook("post_tool_use", {**base, "hook_event_name": "PostToolUse", "tool_name": "Write",
                       "tool_input": {"file_path": str(f), "content": "x"}, "tool_response": {"success": True}})
r = hook("stop_gate", {**base, "hook_event_name": "Stop", "stop_hook_active": False})
check("gate blocks an unlogged .py", r.returncode == 2 and "TRACKER GATE" in (r.stdout + r.stderr),
      f"rc={r.returncode}")
r = subprocess.run([sys.executable, str(ROOT / "tracker.py"), "log", "--title", "Selftest thing",
                    "--summary", "A throwaway file the self-test writes to prove the gate and the log work end to end.",
                    "--usage", "python tests/selftest.py", "--file", str(f)],
                   capture_output=True, text=True, env={**ENV, "CLAUDE_SESSION_ID": sid}, cwd=str(work),
                   encoding="utf-8")
check("tracker log", r.returncode == 0, (r.stdout + r.stderr)[-300:])
r = hook("stop_gate", {**base, "hook_event_name": "Stop", "stop_hook_active": False})
check("gate passes once logged", r.returncode == 0, f"rc={r.returncode} {r.stderr[-200:]}")

# 5. path handling is forward-slash on every OS
import oscompat  # noqa: E402
check("norm_path uses forward slashes", "\\" not in db.norm_path(str(f)))
check("derive_project", db.derive_project(str(f)) not in ("", "unknown"))

# 6. team: an employee's file lands in the owner's ledger under a profile
share, roster = TMP / "share", TMP / "team.json"
tenv = {**ENV, "CLAUDE_TEAM_ROSTER": str(roster)}
subprocess.run([sys.executable, str(ROOT / "team.py"), "send", "--name", "tester", "--to", str(share)],
               env=tenv, capture_output=True, text=True)
odb = TMP / "owner.db"
r = subprocess.run([sys.executable, str(ROOT / "team.py"), "collect", "--from", str(share)],
                   env={**tenv, "CLAUDE_TRACKER_DB": str(odb)}, capture_output=True, text=True, encoding="utf-8")
check("team send -> collect", r.returncode == 0 and "1 new item" in r.stdout, (r.stdout + r.stderr)[-300:])
check("team roster profile created", roster.exists() and json.loads(roster.read_text())["people"])

# 7. server answers /health on a free port
s = socket.socket()
s.bind(("127.0.0.1", 0))
port = s.getsockname()[1]
s.close()
srv = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--port", str(port)], env=ENV,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
ok = False
try:
    for _ in range(50):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as resp:
                ok = resp.status == 200
                break
        except Exception:
            time.sleep(0.2)
    check("server /health", ok)
    for route in ("/api/usage/limits", "/"):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{route}", timeout=5) as resp:
                check(f"server {route}", resp.status == 200)
        except Exception as e:
            check(f"server {route}", False, str(e)[:120])
finally:
    srv.terminate()
    srv.wait(timeout=10)

print(f"\n{'ALL GREEN' if not fails else str(len(fails)) + ' FAILED'}  (platform {sys.platform}, python {sys.version.split()[0]})")
sys.exit(1 if fails else 0)
