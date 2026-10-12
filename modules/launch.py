"""Command Center module 8 — Projects & launcher.

GET  /api/launch/projects        every real project under ~/Desktop: docs, git state, ledger
                                 activity, the launchd jobs that belong to it and how to start it
GET  /api/launch/project?path=   one project with its HANDOFF.md head and full command list
POST /api/launch/action          {path, action: finder|terminal|warp|claude|vscode|run, cmd?}

A "project" is a top-level directory under ~/Desktop that has a CLAUDE.md, HANDOFF.md, README.md,
a .git, or at least one item in the build ledger. `run` only accepts a command this module itself
discovered for that project - never free text from the page.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from modules.common import ApiError, run_cmd  # noqa: E402

HOME = Path.home()
DESKTOP = HOME / "Desktop"
AGENTS = HOME / "Library" / "LaunchAgents"
DOCS = ["CLAUDE.md", "HANDOFF.md", "README.md", "SPEC.md", "RULES.md", "NOTES.md"]
ACTIONS = {"finder", "terminal", "warp", "claude", "vscode", "run"}
SKIP_DIRS = {"Screenshots", "Applications", ".Trash"}

_CACHE: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: float, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (time.time(), val)
    return val


# ---------------------------------------------------------------- discovery
def git_summary(path: Path) -> dict | None:
    if not (path / ".git").exists():
        return None
    def build():
        rc, out, _ = run_cmd(["/usr/bin/git", "-C", str(path), "status", "--porcelain", "-uno"], timeout=8)
        dirty = len([l for l in out.splitlines() if l.strip()]) if rc == 0 else None
        rc2, branch, _ = run_cmd(["/usr/bin/git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"], timeout=6)
        rc3, last, _ = run_cmd(["/usr/bin/git", "-C", str(path), "log", "-1", "--pretty=%h %ad %s",
                                "--date=short"], timeout=6)
        return {"branch": branch.strip() if rc2 == 0 else None,
                "dirty": dirty,
                "last_commit": last.strip()[:160] if rc3 == 0 else None}
    return _cached(f"git:{path}", 30.0, build)


def launchd_jobs_for(path: Path) -> list[str]:
    def build():
        labels = []
        if not AGENTS.is_dir():
            return labels
        for f in AGENTS.iterdir():
            if not f.name.endswith((".plist", ".plist.disabled")):
                continue
            try:
                pl = plistlib.loads(f.read_bytes())
            except Exception:
                continue
            blob = " ".join(str(a) for a in (pl.get("ProgramArguments") or [])) + " " + str(pl.get("WorkingDirectory") or "")
            if str(path) in blob:
                labels.append(pl.get("Label") or f.stem)
        return sorted(labels)
    return _cached(f"jobs:{path}", 60.0, build)


def start_commands(path: Path) -> list[dict]:
    cmds: list[dict] = []
    try:
        entries = sorted(path.iterdir())
    except OSError:
        return cmds
    for f in entries:
        if not f.is_file():
            continue
        n = f.name
        if re.fullmatch(r"(start|run|launch)[\w.-]*\.(sh|command)", n) or n in ("run.sh", "start.sh"):
            cmds.append({"label": n, "cmd": f"bash {json.dumps(str(f))}", "kind": "script"})
        elif n == "Makefile":
            rc, out, _ = run_cmd(["/usr/bin/grep", "-E", r"^[a-zA-Z0-9_.-]+:", str(f)], timeout=5)
            for line in (out or "").splitlines()[:8]:
                t = line.split(":")[0]
                cmds.append({"label": f"make {t}", "cmd": f"cd {json.dumps(str(path))} && make {t}", "kind": "make"})
        elif n == "package.json":
            try:
                scripts = (json.loads(f.read_text()).get("scripts") or {})
            except Exception:
                scripts = {}
            for t in list(scripts)[:8]:
                cmds.append({"label": f"npm run {t}", "cmd": f"cd {json.dumps(str(path))} && npm run {t}",
                             "kind": "npm"})
    for label in launchd_jobs_for(path):
        cmds.append({"label": f"launchctl kickstart {label}",
                     "cmd": f"launchctl kickstart -k gui/{__import__('oscompat').uid()}/{label}", "kind": "launchd"})
    return cmds


def is_project(d: Path, ledger_projects: set[str]) -> bool:
    if not d.is_dir() or d.name.startswith(".") or d.name in SKIP_DIRS:
        return False
    if d.suffix in (".app", ".photoslibrary", ".bundle", ".download", ".localized"):
        return False  # macOS bundles are not projects
    if d.name in ledger_projects or (d / ".git").exists():
        return True
    return any((d / doc).exists() for doc in DOCS)


def project_row(d: Path, con, deep: bool = False) -> dict:
    name = d.name
    docs = []
    for doc in DOCS:
        p = d / doc
        if p.exists():
            try:
                docs.append({"name": doc, "bytes": p.stat().st_size,
                             "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(p.stat().st_mtime))})
            except OSError:
                pass
    last_item = con.execute(
        "SELECT id,title,kind,COALESCE(built_at,created_at) ts FROM items WHERE project=? AND status!='deleted' "
        "ORDER BY ts DESC LIMIT 1", (name,)).fetchone()
    n_items = con.execute("SELECT count(*) n FROM items WHERE project=? AND status!='deleted'",
                          (name,)).fetchone()["n"]
    last_sess = con.execute(
        "SELECT session_id,title,COALESCE(started_at,created_at) ts, cost_usd FROM sessions WHERE project=? "
        "ORDER BY ts DESC LIMIT 1", (name,)).fetchone()
    spark = [r["n"] for r in con.execute(
        "SELECT count(*) n FROM sessions WHERE project=? AND COALESCE(started_at,created_at) >= date('now','-30 day') "
        "GROUP BY date(COALESCE(started_at,created_at)) ORDER BY date(COALESCE(started_at,created_at))", (name,))]
    row = {
        "name": name, "path": str(d),
        "docs": docs,
        "git": git_summary(d),
        "items": n_items,
        "last_item": dict(last_item) if last_item else None,
        "last_session": dict(last_sess) if last_sess else None,
        "sessions_30d": sum(spark),
        "activity": spark,
        "jobs": launchd_jobs_for(d),
        "commands": start_commands(d),
    }
    if deep:
        h = d / "HANDOFF.md"
        if h.exists():
            try:
                row["handoff_head"] = "\n".join(h.read_text(errors="replace").splitlines()[:30])
            except OSError:
                pass
    return row


# ---------------------------------------------------------------- handlers
def get_projects(query=None, body=None, match=None):
    con = db.connect()
    ledger_projects = {r["project"] for r in con.execute(
        "SELECT DISTINCT project FROM items WHERE project IS NOT NULL")}
    rows = []
    try:
        for d in sorted(DESKTOP.iterdir()):
            if is_project(d, ledger_projects):
                rows.append(project_row(d, con))
    except OSError as e:
        raise ApiError(500, f"cannot read ~/Desktop: {e}")
    rows.sort(key=lambda r: ((r["last_item"] or {}).get("ts") or (r["last_session"] or {}).get("ts") or "",
                             r["items"]), reverse=True)
    return 200, {"ok": True, "count": len(rows), "projects": rows}


def get_project(query=None, body=None, match=None):
    query = query or {}
    raw = str(query.get("path") or "").strip()
    if not raw:
        raise ApiError(400, "path is required")
    try:
        p = Path(os.path.expanduser(raw)).resolve()
    except OSError:
        raise ApiError(400, "unresolvable path")
    if not p.is_dir() or not str(p).startswith(str(DESKTOP.resolve()) + "/"):
        raise ApiError(400, "path must be a directory under ~/Desktop")
    return 200, {"ok": True, "project": project_row(p, db.connect(), deep=True)}


def post_action(query=None, body=None, match=None):
    body = body or {}
    action = str(body.get("action", "")).strip().lower()
    if action not in ACTIONS:
        raise ApiError(400, f"action must be one of {'|'.join(sorted(ACTIONS))}")
    raw = str(body.get("path") or "").strip()
    if not raw:
        raise ApiError(400, "path is required")
    try:
        p = Path(os.path.expanduser(raw)).resolve()
    except OSError:
        raise ApiError(400, "unresolvable path")
    home = HOME.resolve()
    if not p.is_dir() or not (p == home or str(p).startswith(str(home) + "/")):
        raise ApiError(400, "path must be an existing directory inside your home folder")
    argv, detail = None, None
    if action == "finder":
        argv = ["/usr/bin/open", str(p)]
    elif action == "vscode":
        argv = ["/usr/bin/open", "-a", "Visual Studio Code", str(p)]
    elif action in ("terminal", "warp", "claude", "run"):
        if action == "claude":
            cmd = f"cd {json.dumps(str(p))} && claude"
        elif action == "run":
            want = str(body.get("cmd", "")).strip()
            allowed = {c["cmd"] for c in start_commands(p)}
            if want not in allowed:
                raise ApiError(400, "cmd must be one of the commands this module discovered for the project")
            cmd = want
        else:
            cmd = f"cd {json.dumps(str(p))}"
        app = "Warp" if (action == "warp" and Path("/Applications/Warp.app").exists()) else "Terminal"
        if app == "Warp":
            script = ('tell application "Warp" to activate\ndelay 0.4\n'
                      'tell application "System Events" to keystroke "t" using command down\ndelay 0.5\n'
                      f'tell application "System Events" to keystroke {json.dumps(cmd)}\n'
                      'tell application "System Events" to key code 36')
        else:
            script = f'tell application "Terminal"\nactivate\ndo script {json.dumps(cmd)}\nend tell'
        argv = ["/usr/bin/osascript", "-e", script]
        detail = {"app": app, "cmd": cmd}
    rc, out, err = run_cmd(argv, timeout=15)
    con = db.connect()
    db.add_event(con, "cc", "action", {"module": "launch", "action": action, "path": str(p),
                                       "rc": rc, **(detail or {})})
    if rc != 0:
        raise ApiError(500, f"{action} failed: {(err or out or '').strip()[:200]}")
    return 200, {"ok": True, "action": action, "path": str(p), **(detail or {})}


ROUTES = [
    ("GET", r"/api/launch/projects/?$", get_projects),
    ("GET", r"/api/launch/project/?$", get_project),
    ("POST", r"/api/launch/action/?$", post_action),
]

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Command Center launch module — projects on the Desktop.")
    ap.add_argument("--path", help="one project")
    a = ap.parse_args()
    res = get_project({"path": a.path})[1] if a.path else get_projects()[1]
    print(json.dumps(res, indent=2, default=str))
