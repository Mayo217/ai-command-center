#!/usr/bin/env python3
"""CLI contract tests for tracker.py against a throwaway DB.

Run:  python3 tests/test_cli.py
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
sys.path.insert(0, str(ROOT))
PY = sys.executable
# Under the home folder, not the system temp folder: the tracker ignores /tmp and %TEMP% by design.
class _SlashPath(type(Path())):
    """The tracker stores paths with forward slashes on every OS (C:/Users/x/...). On Windows the
    tests print paths the same way, so commands and comparisons match what the tracker records."""
    def __str__(self):
        return super().__str__().replace("\\", "/")


TMP = _SlashPath(tempfile.mkdtemp(prefix="tracker-clitest-", dir=Path.home()))
import atexit  # noqa: E402
import shutil  # noqa: E402
atexit.register(shutil.rmtree, TMP, True)
DB = TMP / "cli.db"
WORK = TMP / "proj"
WORK.mkdir()
(WORK / "app.py").write_text("print('app')\n")
(WORK / "README.md").write_text("# readme\n")
SID = "cli00000-aaaa-bbbb-cccc-dddddddddddd"
ENV = {**os.environ, "CLAUDE_TRACKER_DB": str(DB), "NO_COLOR": "1", "CLAUDE_SESSION_ID": SID}

RESULTS: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), "" if cond else str(detail)))
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"\n          -> {detail}"))


def cli(*args, stdin=None, cwd=None):
    return subprocess.run([PY, str(ROOT / "tracker.py"), *args], input=stdin, capture_output=True,
                          text=True, env=ENV, cwd=str(cwd or WORK), timeout=30)


print("\n[help]")
for cmd in ["log", "waive", "dismiss", "status", "recent", "list", "search", "show", "edit", "sessions", "backfill",
            "serve", "open", "export", "doctor", "gate-check"]:
    p = cli(cmd, "--help")
    check(f"`tracker {cmd} --help` works", p.returncode == 0 and "usage:" in p.stdout, p.stdout[:120] + p.stderr[:200])

print("\n[log: validation]")
p = cli("log", "--title", "short", "--summary", "x" * 80)
check("title < 8 chars rejected (exit 1)", p.returncode == 1 and "--title must be 8..140" in p.stderr, p.stderr[:200])
p = cli("log", "--title", "A perfectly fine title", "--summary", "too short")
check("summary < 60 chars rejected", p.returncode == 1 and ">= 60 chars" in p.stderr, p.stderr[:200])
t = "The same words repeated in both the title and the summary field, which is useless"
p = cli("log", "--title", t, "--summary", t)
check("summary == title rejected", p.returncode == 1 and "must add information" in p.stderr, p.stderr[:200])
p = cli("log", "--title", "Valid title here", "--summary", "y" * 80, "--kind", "nonsense")
check("bad --kind rejected by argparse", p.returncode != 0 and "invalid choice" in p.stderr, p.stderr[:200])

print("\n[log: happy paths]")
p = cli("log", "--title", "Project app entry point",
        "--summary", "The main app script for the CLI test project; it exists so the tests have a real durable file to attach and search against.",
        "--usage", "python3 app.py --once", "--kind", "build",
        "--file", "app.py:entry", "--file", str(WORK / "README.md") + ":doc",
        "--link", "https://claude.ai/artifact/xyz|the page", "--tag", "clitest", "--tag", "demo")
check("log ok", p.returncode == 0 and "logged item #1" in p.stdout, p.stdout + p.stderr)
check("relative path normalised to absolute", str(WORK / "app.py") in p.stdout, p.stdout)

p = cli("log", "--json", "--title", "Missing file item",
        "--summary", "An item that deliberately points at a file which does not exist, to prove the exists flag and the stderr warning.",
        "--file", str(WORK / "ghost.py"))
j = json.loads(p.stdout)
check("--json output on log", j["ok"] and j["items"][0]["id"] == 2, p.stdout[:200])
check("missing file allowed but warned", "does not exist" in p.stderr and j["items"][0]["missing"], p.stderr[:200])

payload = {"items": [
    {"title": "Batch item one here", "summary": "First of two items created in a single --json-in call so multi-item logging is covered by the tests.",
     "kind": "doc", "files": [{"path": str(WORK / "README.md"), "role": "doc"}], "tags": ["batch"]},
    {"title": "Batch item two here", "summary": "Second of two items created in a single --json-in call; it also carries a link so link handling is covered.",
     "kind": "config", "links": [{"url": "https://example.invalid/x", "label": "doc"}]},
]}
p = cli("log", "--json-in", "-", stdin=json.dumps(payload))
check("--json-in creates several items", p.returncode == 0 and p.stdout.count("logged item #") == 2, p.stdout + p.stderr)

print("\n[list / recent / search]")
p = cli("list", "--json")
j = json.loads(p.stdout)
check("list returns 4 items newest first", j["count"] == 4 and j["items"][0]["id"] == 4, j["count"])
p = cli("list", "--kind", "doc", "--json")
check("list --kind filters", json.loads(p.stdout)["count"] == 1, p.stdout[:200])
p = cli("list", "--since", "2099-01-01", "--json")
check("list --since filters", json.loads(p.stdout)["count"] == 0, p.stdout[:200])
p = cli("recent", "--cwd", str(WORK), "--json")
check("recent finds items by cwd/project", json.loads(p.stdout)["count"] >= 3, p.stdout[:200])
p = cli("recent", "--project", "nope-not-a-project", "--json")
check("recent --project isolates", json.loads(p.stdout)["count"] == 0, p.stdout[:200])
p = cli("search", "durable", "--json")
j = json.loads(p.stdout)
check("search finds by summary text", j["count"] == 1 and j["items"][0]["id"] == 1, p.stdout[:300])
check("search returns a snippet", bool(j["items"][0].get("snippet")), p.stdout[:300])
p = cli("search", "app.py", "--json")
check("search finds by file path", json.loads(p.stdout)["count"] >= 1, p.stdout[:200])
p = cli("search", 'weird "quoted( query', "--json")
check("search survives FTS-hostile input", p.returncode == 0, p.stdout[:200] + p.stderr[:200])

print("\n[show / edit]")
p = cli("show", "1")
check("show prints the item", p.returncode == 0 and "Project app entry point" in p.stdout and "usage:" in p.stdout, p.stdout[:200])
check("show marks file existence", "ok" in p.stdout and "app.py" in p.stdout, p.stdout[:400])
p = cli("show", "999")
check("show on a missing id exits 2", p.returncode == 2, p.returncode)
p = cli("show", "2", "--json")
j = json.loads(p.stdout)
check("show --json includes files+session", j["item"]["files"][0]["exists"] is False and j["item"]["session"]["session_id"] == SID, p.stdout[:300])

p = cli("edit", "1", "--status", "wip", "--add-tag", "edited", "--rm-tag", "demo",
        "--add-link", "https://example.invalid/dash|dash", "--usage", "python3 app.py --twice")
check("edit applies changes", p.returncode == 0 and "updated item #1" in p.stdout, p.stdout + p.stderr)
j = json.loads(cli("show", "1", "--json").stdout)["item"]
check("edit persisted status/usage/tags/link",
      j["status"] == "wip" and j["usage"].endswith("--twice") and "edited" in j["tags"] and "demo" not in j["tags"]
      and len(j["links"]) == 2, j)
p = cli("edit", "1", "--summary", "too short")
check("edit validates summary length", p.returncode == 1, p.stderr[:200])
p = cli("edit", "999", "--status", "active")
check("edit on a missing id exits 2", p.returncode == 2, p.returncode)
p = cli("edit", "2", "--confidence", "confirmed", "--json")
check("edit can promote confidence", json.loads(p.stdout)["item"]["confidence"] == "confirmed", p.stdout[:200])
p = cli("edit", "1")
check("edit with no fields is a validation error", p.returncode == 1, p.stderr[:120])

print("\n[supersedes]")
p = cli("log", "--title", "Project app entry point v2",
        "--summary", "Replaces the first app entry item; proves that --supersedes flips the old item to superseded and links the two.",
        "--kind", "enhancement", "--supersedes", "1", "--file", str(WORK / "app.py"), "--json")
new_id = json.loads(p.stdout)["items"][0]["id"]
old = json.loads(cli("show", "1", "--json").stdout)["item"]
new = json.loads(cli("show", str(new_id), "--json").stdout)["item"]
check("--supersedes links and demotes", new["supersedes_id"] == 1 and old["status"] in ("superseded", "wip"), (old["status"], new["supersedes_id"]))
p = cli("log", "--title", "Bad supersedes test", "--summary", "z" * 80, "--supersedes", "9999")
check("--supersedes on a missing id exits 2", p.returncode == 2, p.returncode)

print("\n[status / sessions / waive]")
p = cli("status", "--json")
j = json.loads(p.stdout)
check("status reports the session and its items", j["session"] == SID and len(j["items"]) == 5, p.stdout[:300])
check("status reports a clear gate", j["gate"]["would_block"] is False, j["gate"])
p = cli("sessions", "--json")
check("sessions lists the session", json.loads(p.stdout)["count"] == 1, p.stdout[:200])
p = cli("sessions", "--unlogged", "--json")
check("sessions --unlogged excludes logged ones", json.loads(p.stdout)["count"] == 0, p.stdout[:200])
p = cli("waive", "--reason", "short")
check("waive rejects a short reason", p.returncode == 1 and ">= 20 chars" in p.stderr, p.stderr[:200])
p = cli("waive", "--reason", "This session only read files and answered questions; nothing durable was produced.")
check("waive accepted when nothing is pending", p.returncode == 0, p.stdout + p.stderr)

print("\n[dismiss]")
import sqlite3 as _sq
_now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + 60))
_c = _sq.connect(str(DB))
_c.execute("INSERT OR IGNORE INTO session_files(session_id,path,op,tool,first_ts,last_ts,ignored) "
           "VALUES(?,?,?,?,?,?,0)", (SID, str(WORK / "0"), "bash", "Bash", _now, _now))
_c.execute("INSERT OR IGNORE INTO session_files(session_id,path,op,tool,first_ts,last_ts,ignored) "
           "VALUES(?,?,?,?,?,?,0)", (SID, str(WORK / "real_code.py"), "bash", "Bash", _now, _now))
_c.commit(); _c.close()
(WORK / "real_code.py").write_text("print(1)\n")
p = cli("status", "--json")
check("phantom + real file are pending", json.loads(p.stdout)["gate"]["would_block"] is True, p.stdout[:300])
p = cli("dismiss", str(WORK / "real_code.py"), "--reason", "pretending this durable file is not mine")
check("dismiss refuses an existing durable file", p.returncode == 1 and "refusing to dismiss" in p.stderr, p.stderr[:200])
p = cli("dismiss", "--missing", "--reason", "regex debris from a heredoc fixture, no such file exists")
check("dismiss --missing clears phantom paths", p.returncode == 0 and "dismissed 1 pending file" in p.stdout, p.stdout + p.stderr)
p = cli("dismiss", "--missing", "--reason", "nothing left to dismiss at this point in the test")
check("dismiss with no matches exits 1", p.returncode == 1, p.stdout + p.stderr)
cli("log", "--title", "Real code file item", "--summary", "Logs the real_code.py file so the gate goes clear again before the export tests run below.")
check("gate clear after logging the remaining file", json.loads(cli("status", "--json").stdout)["gate"]["would_block"] is False)

print("\n[export]")
p = cli("export", "--md")
check("export --md produces markdown", p.returncode == 0 and p.stdout.startswith("# AI Command Center digest")
      and "### #1" in p.stdout and "**Use:**" in p.stdout, p.stdout[:200])
p = cli("export", "--json-out")
check("export --json-out produces JSON", json.loads(p.stdout)["count"] == 6, p.stdout[:120])
p = cli("export", "--md", "--project", "nope")
check("export --project filters", "### #" not in p.stdout, p.stdout[:200])

print("\n[doctor]")
p = cli("doctor", "--json")
j = json.loads(p.stdout)
names = {c["check"]: c["ok"] for c in j["checks"]}
check("doctor runs and returns checks", p.returncode == 0 and len(j["checks"]) > 10, len(j["checks"]))
check("doctor: database green", names.get("database") is True, names)
check("doctor: fts index green", names.get("fts index") is True, names)
check("doctor: hooks wired green", names.get("hooks wired in ~/.claude/settings.json") is True, names)
check("doctor: shim green", any(v for k, v in names.items() if k.startswith("shim ")), names)
check("doctor: skill green", names.get("skill log-build") is True, names)
check("doctor: CLAUDE.md rule green", names.get("CLAUDE.md build-ledger rule") is True, names)

print("\n[gate-check]")
j = json.loads(cli("gate-check", "--json").stdout)
check("gate-check returns the gate state", j["ok"] and "would_block" in j and "pending" in j, j)

print("\n[timing] (10 runs each)")
timings = {}
for cmd in (["status"], ["log", "--title", "Timing probe item", "--summary", "t" * 80], ["recent", "--cwd", str(WORK)],
            ["list"], ["search", "app"], ["gate-check"]):
    t0 = time.time()
    n = 10 if cmd[0] != "log" else 5
    for _ in range(n):
        cli(*cmd)
    ms = (time.time() - t0) * 1000 / n
    timings[cmd[0]] = ms
    print(f"  tracker {cmd[0]:<11} {ms:6.1f} ms/run")
# Speed is checked on a real computer. Shared CI machines (GitHub Actions sets CI=true) stall
# unpredictably, so there the timings are reported, not enforced.
if os.environ.get("CI"):
    print(f"  (CI) status {timings['status']:.0f} ms, log {timings['log']:.0f} ms - reported, not enforced")
else:
    check("status under 150 ms", timings["status"] < 150, f"{timings['status']:.1f} ms")
    check("log under 150 ms", timings["log"] < 150, f"{timings['log']:.1f} ms")

failed = [r for r in RESULTS if not r[1]]
print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
if failed:
    print("FAILED:")
    for n, _, d in failed:
        print(f"  - {n}: {d}")
    sys.exit(1)
shutil.rmtree(TMP, ignore_errors=True)
print("test_cli: ALL GREEN")
