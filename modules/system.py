"""Command Center module 9 — Machine & services.

GET  /api/system                 one snapshot of this Mac: uptime, load, memory pressure,
                                 disk free, the sizes that matter (~/.claude, transcripts,
                                 tracker.db), tool versions, listening ports, clocks.
POST /api/system/action          {action: restart_server|backfill|rebuild_fts|open_logs}

Read-only except the POST, which is audited into session_events(session_id='cc').
Stdlib only; every shell-out goes through modules.common.run_cmd (no shell, timeouts).
"""
from __future__ import annotations

import datetime
import json
import os
import plistlib
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402  (shared foundation: config, connect, log_line)
from modules.common import ApiError, run_cmd  # noqa: E402
import oscompat  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HOME = Path.home()
LAUNCH_AGENTS = HOME / "Library" / "LaunchAgents"
CLAUDE_DIR = HOME / ".claude"
import oscompat  # noqa: E402
IST = oscompat.local_tz()  # name kept for history: this is the LOCAL display timezone
TZ_LABEL = oscompat.tz_label()

# Ports we always look for, whatever launchd says.
KNOWN_PORTS = {
    8790: "AI Command Center",
}

_CACHE: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: float, fn):
    """Tiny TTL cache so the header status strip can poll without hammering the box."""
    hit = _CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (now, val)
    return val


# ---------------------------------------------------------------- host metrics
def _uptime() -> dict:
    rc, out, _ = run_cmd(["/usr/sbin/sysctl", "-n", "kern.boottime"], timeout=3)
    boot_epoch = None
    if rc == 0:
        m = re.search(r"sec\s*=\s*(\d+)", out)
        if m:
            boot_epoch = int(m.group(1))
    secs = int(time.time() - boot_epoch) if boot_epoch else None
    return {
        "boot_epoch": boot_epoch,
        "boot_at": datetime.datetime.fromtimestamp(boot_epoch, datetime.timezone.utc).isoformat() if boot_epoch else None,
        "seconds": secs,
        "human": _dur(secs),
    }


def _dur(secs: int | None) -> str | None:
    if secs is None:
        return None
    d, r = divmod(int(secs), 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _cpu() -> dict:
    load = None
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        pass
    rc, out, _ = run_cmd(["/usr/sbin/sysctl", "-n", "hw.ncpu"], timeout=3)
    ncpu = int(out.strip()) if rc == 0 and out.strip().isdigit() else None
    return {
        "load_1_5_15": load,
        "ncpu": ncpu,
        "load_per_cpu": round(load[0] / ncpu, 2) if load and ncpu else None,
    }


def _memory() -> dict:
    """vm_stat pages -> a real memory-pressure read, the way Activity Monitor computes it."""
    rc, out, _ = run_cmd(["/usr/bin/vm_stat"], timeout=4)
    if rc != 0:
        return {"error": "vm_stat unavailable"}
    page = 4096
    m = re.search(r"page size of (\d+) bytes", out)
    if m:
        page = int(m.group(1))
    vals: dict[str, int] = {}
    for line in out.splitlines():
        mm = re.match(r'"?([A-Za-z][^:"]*)"?:\s+(\d+)', line.strip())
        if mm:
            vals[mm.group(1).strip().lower()] = int(mm.group(2))

    def pages(*names: str) -> int:
        for n in names:
            if n in vals:
                return vals[n]
        return 0

    rc2, out2, _ = run_cmd(["/usr/sbin/sysctl", "-n", "hw.memsize"], timeout=3)
    total = int(out2.strip()) if rc2 == 0 and out2.strip().isdigit() else None

    wired = pages("pages wired down") * page
    compressed = pages("pages occupied by compressor") * page
    anon = pages("anonymous pages") * page
    purgeable = pages("pages purgeable") * page
    free = (pages("pages free") + pages("pages speculative")) * page
    # Apple's pressure proxy: (wired + compressed + anonymous-purgeable) / total
    used_for_pressure = wired + compressed + max(anon - purgeable, 0)
    pressure = round(100.0 * used_for_pressure / total, 1) if total else None
    return {
        "total_bytes": total,
        "total_gb": round(total / 1e9, 1) if total else None,
        "free_bytes": free,
        "free_gb": round(free / 1e9, 2),
        "wired_gb": round(wired / 1e9, 2),
        "compressed_gb": round(compressed / 1e9, 2),
        "pressure_pct": pressure,
        "swapins": vals.get("swapins"),
        "swapouts": vals.get("swapouts"),
    }


def _disk(path: str) -> dict:
    try:
        st = os.statvfs(path)
    except OSError as exc:
        return {"path": path, "error": str(exc)}
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    return {
        "path": path,
        "total_gb": round(total / 1e9, 1),
        "free_gb": round(free / 1e9, 1),
        "used_pct": round(100.0 * (total - free) / total, 1) if total else None,
    }


# ------------------------------------------------------------------ disk sizes
def _du_bytes(path: Path, timeout: float = 12.0) -> int | None:
    """`du -sk` — fast enough on ~/.claude and far cheaper than walking in Python."""
    if not path.exists():
        return None
    rc, out, _ = run_cmd(["/usr/bin/du", "-sk", str(path)], timeout=timeout)
    if rc != 0:
        return None
    try:
        return int(out.split()[0]) * 1024
    except (ValueError, IndexError):
        return None


def _transcript_stats() -> dict:
    projects = Path(db.config().get("projects_dir") or (CLAUDE_DIR / "projects"))
    files = 0
    newest = None
    if projects.is_dir():
        for p in projects.rglob("*.jsonl"):
            files += 1
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if newest is None or mt > newest:
                newest = mt
    return {
        "dir": str(projects),
        "jsonl_files": files,
        "bytes": _du_bytes(projects),
        "newest_mtime": datetime.datetime.fromtimestamp(newest, datetime.timezone.utc).isoformat() if newest else None,
    }


def _db_stats() -> dict:
    out: dict = {"path": str(db.DB_PATH), "exists": db.DB_PATH.exists()}
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db.DB_PATH) + suffix)
        if p.exists():
            total += p.stat().st_size
    out["bytes"] = total
    out["mb"] = round(total / 1e6, 2)
    try:
        con = db.connect()
        for name, sql in (
            ("items", "SELECT count(*) FROM items WHERE status!='deleted'"),
            ("sessions", "SELECT count(*) FROM sessions"),
            ("session_files", "SELECT count(*) FROM session_files"),
        ):
            try:
                out[name] = con.execute(sql).fetchone()[0]
            except sqlite3.Error:
                out[name] = None
        con.close()
    except Exception as exc:  # DB not migrated yet, locked, etc. — never 500 the whole view
        out["error"] = str(exc)
    return out


# -------------------------------------------------------------------- versions
def _versions() -> dict:
    py = db.config().get("python") or sys.executable
    rc, out, err = run_cmd([py, "-V"], timeout=5)
    python_v = (out or err).strip() or None
    claude_bin = str(HOME / ".local" / "bin" / "claude")
    if not os.path.exists(claude_bin):
        claude_bin = "claude"
    rc, out, _ = run_cmd([claude_bin, "--version"], timeout=8)
    claude_v = out.strip().splitlines()[0] if rc == 0 and out.strip() else None
    rc, out, _ = run_cmd(["/usr/bin/sw_vers", "-productVersion"], timeout=4)
    macos = out.strip() or None
    rc, out, _ = run_cmd(["/usr/bin/uname", "-m"], timeout=3)
    return {
        "python": python_v,
        "python_path": py,
        "sqlite": sqlite3.sqlite_version,
        "claude_code": claude_v,
        "macos": macos,
        "arch": out.strip() or None,
        "tracker_schema": _schema_version(),
    }


def _schema_version() -> str | None:
    try:
        con = db.connect()
        row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None


# --------------------------------------------------------------------- processes
def _pgrep(pattern: str) -> list[int]:
    rc, out, _ = run_cmd(["/usr/bin/pgrep", "-f", pattern], timeout=5)
    if rc != 0:
        return []
    return [int(x) for x in out.split() if x.isdigit()]


def _apps() -> dict:
    claude_procs = [p for p in _pgrep(r"(^|/)claude( |$)") if p != os.getpid()]
    server = _pgrep(f"{ROOT}/server.py")
    return {
        "claude_sessions_running": len(claude_procs),
        "server_pids": server,
        "server_running": bool(server),
    }


# ------------------------------------------------------------------------ ports
def _launchd_ports() -> dict[int, str]:
    """Any port literal mentioned in a LaunchAgent plist, so new services show up for free."""
    found: dict[int, str] = {}
    if not LAUNCH_AGENTS.is_dir():
        return found
    for p in sorted(LAUNCH_AGENTS.glob("*.plist")):
        try:
            with open(p, "rb") as fh:
                data = plistlib.load(fh)
        except Exception:
            continue
        label = data.get("Label") or p.stem
        blob = json.dumps(data, default=str)
        for m in re.finditer(r"(?<![\d.])(\d{4,5})(?![\d.])", blob):
            port = int(m.group(1))
            if 1024 <= port <= 65535:
                found.setdefault(port, label)
    return found


def _ports() -> list[dict]:
    wanted = dict(KNOWN_PORTS)
    for port, label in _launchd_ports().items():
        wanted.setdefault(port, label)
    rc, out, _ = run_cmd(["/usr/sbin/lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], timeout=8)
    listening: dict[int, dict] = {}
    if rc == 0:
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 9:
                continue
            m = re.search(r":(\d+)$", parts[8])
            if not m:
                continue
            port = int(m.group(1))
            listening.setdefault(port, {"command": parts[0], "pid": parts[1], "addr": parts[8]})
    rows = []
    for port in sorted(wanted):
        live = listening.get(port)
        rows.append({
            "port": port,
            "label": wanted[port],
            "listening": bool(live),
            "command": live["command"] if live else None,
            "pid": int(live["pid"]) if live and live["pid"].isdigit() else None,
            "addr": live["addr"] if live else None,
        })
    # anything else listening that we did not ask about, so surprises are visible
    for port, live in sorted(listening.items()):
        if port not in wanted and port >= 1024:
            rows.append({"port": port, "label": "(unlisted)", "listening": True,
                         "command": live["command"], "pid": int(live["pid"]) if live["pid"].isdigit() else None,
                         "addr": live["addr"]})
    return rows


def _launchd_job() -> dict:
    label = "com.aicommandcenter.server"
    uid = oscompat.uid()
    rc, out, _ = run_cmd(["/bin/launchctl", "print", f"gui/{uid}/{label}"], timeout=6)
    if rc != 0:
        return {"label": label, "loaded": False, "pid": None, "last_exit_status": None}
    pid = re.search(r"^\s*pid = (\d+)", out, re.M)
    exit_st = re.search(r"last exit code = (\d+)", out)
    state = re.search(r"^\s*state = (\S+)", out, re.M)
    return {
        "label": label,
        "loaded": True,
        "pid": int(pid.group(1)) if pid else None,
        "state": state.group(1) if state else None,
        "last_exit_status": int(exit_st.group(1)) if exit_st else None,
        "plist": str(LAUNCH_AGENTS / f"{label}.plist"),
    }


def _clocks() -> dict:
    now = datetime.datetime.now(datetime.timezone.utc)
    return {
        "utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ist": now.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S ") + TZ_LABEL,
        "local": now.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "epoch": int(now.timestamp()),
    }


# ------------------------------------------------------------------- handlers
def get_system(query=None, body=None, match=None):
    """GET /api/system — full snapshot. `?fast=1` skips the `du` walks (header polling)."""
    query = query or {}
    fast = str(query.get("fast", "")).lower() in ("1", "true", "yes")
    snap = {
        "generated_at": db.now_iso(),
        "host": {
            "uptime": _cached("uptime", 20, _uptime),
            "cpu": _cpu(),
            "memory": _cached("mem", 5, _memory),
            "disks": [_disk("/"), _disk(str(HOME / "Desktop"))],
        },
        "clocks": _clocks(),
        "versions": _cached("versions", 300, _versions),
        "processes": _cached("apps", 5, _apps),
        "ports": _cached("ports", 10, _ports),
        "launchd": _cached("job", 10, _launchd_job),
        "tracker_db": _cached("db", 10, _db_stats),
        "root": str(ROOT),
    }
    if fast:
        snap["sizes"] = {"skipped": True}
    else:
        snap["sizes"] = _cached("sizes", 120, lambda: {
            "claude_dir_bytes": _du_bytes(CLAUDE_DIR, timeout=20),
            "claude_dir_path": str(CLAUDE_DIR),
            "transcripts": _transcript_stats(),
            "tracker_root_bytes": _du_bytes(ROOT, timeout=10),
        })
    return 200, snap


ACTIONS = ("restart_server", "backfill", "rebuild_fts", "open_logs")


def post_action(query=None, body=None, match=None):
    """POST /api/system/action {action} — the four safe maintenance actions, audited."""
    body = body or {}
    action = str(body.get("action") or "").strip()
    if action not in ACTIONS:
        raise ApiError(400, f"unknown action {action!r}; expected one of {', '.join(ACTIONS)}")

    uid = oscompat.uid()
    py = db.config().get("python") or sys.executable
    result: dict = {"action": action}

    if action == "restart_server" and not oscompat.has_launchd():
        import subprocess
        db.LOG_DIR.mkdir(exist_ok=True)
        with open(db.LOG_DIR / "server.out", "ab") as fh:
            subprocess.Popen([py, str(ROOT / "tracker.py"), "restart"], cwd=str(ROOT), stdout=fh,
                             stderr=fh, **oscompat.detached_kwargs())
        result["detail"] = "restart started in the background; the server is back in a moment"

    elif action == "restart_server":
        rc, out, err = run_cmd(["/bin/launchctl", "kickstart", "-k",
                                f"gui/{uid}/com.aicommandcenter.server"], timeout=10)
        if rc != 0:
            raise ApiError(500, f"launchctl kickstart failed (rc={rc}): {(err or out).strip()[:300]}")
        result["detail"] = "launchd job kickstarted; the server restarts in a moment"

    elif action == "backfill":
        script = ROOT / "backfill.py"
        if not script.exists():
            raise ApiError(503, "backfill.py is not installed yet")
        # Detached: backfill over the full corpus takes far longer than an HTTP request.
        import subprocess
        logf = db.LOG_DIR / "backfill_run.out"
        db.LOG_DIR.mkdir(exist_ok=True)
        with open(logf, "ab") as fh:
            subprocess.Popen([py, str(script)], cwd=str(ROOT), stdout=fh, stderr=fh,
                             **oscompat.detached_kwargs())
        result["detail"] = f"backfill started in the background; output → {logf}"

    elif action == "rebuild_fts":
        con = db.connect()
        try:
            n = db.rebuild_fts(con)
        finally:
            con.close()
        result["detail"] = f"rebuilt the search index over {n} items"
        result["items_indexed"] = n
        _CACHE.pop("db", None)

    elif action == "open_logs":
        rc, out, err = run_cmd(["/usr/bin/open", str(db.LOG_DIR)], timeout=6)
        if rc != 0:
            raise ApiError(500, f"could not open {db.LOG_DIR}: {(err or out).strip()[:200]}")
        result["detail"] = f"opened {db.LOG_DIR} in Finder"

    _audit(action, result)
    result["ok"] = True
    return 200, result


def _audit(action: str, payload: dict) -> None:
    """Every mutating action lands in session_events so the centre keeps an audit trail."""
    try:
        con = db.connect()
        try:
            db.upsert_session(con, "cc", source="manual", cwd=str(ROOT), title="Command Center actions")
            db.add_event(con, "cc", "action", {"module": "system", **payload})
        finally:
            con.close()
    except Exception as exc:
        db.log_line("server", f"system.action audit failed: {exc}")


# server.py prefixes a pattern with /api/<module>/ unless it already starts with /api/,
# and matches with re.fullmatch — so these are written absolute.
ROUTES = [
    ("GET", r"/api/system/?$", get_system),
    ("POST", r"/api/system/action/?$", post_action),
]

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Command Center system module — print the machine snapshot as JSON.")
    ap.add_argument("--fast", action="store_true", help="skip the du size walks")
    a = ap.parse_args()
    print(json.dumps(get_system({"fast": "1"} if a.fast else {}, None, None)[1], indent=2, default=str))
