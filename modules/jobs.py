"""Command Center module 6 — Automations (launchd).

GET  /api/jobs                    every ~/Library/LaunchAgents/*.plist (+ .disabled) with schedule,
                                  load state, last exit, pid, log paths
GET  /api/jobs/<label>            one job with a log tail
POST /api/jobs/<label>/action     {action: start|stop|kickstart|load|unload}
POST /api/jobs/<label>/logtail    {lines: 200}

Only user agents under ~/Library/LaunchAgents are touched - never /Library or system domains.
Every mutating action is audited into session_events(session_id='cc').

Health is judged on EVIDENCE OF WORK, not on the exit code: a job is healthy when something it
writes has moved recently - its own (unshared) stdout/stderr file, or a log path named in its
arguments or environment. (macOS launchd only; on Windows this module reports no jobs.)
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from modules.common import ApiError, run_cmd  # noqa: E402

HOME = Path.home()
AGENTS = HOME / "Library" / "LaunchAgents"
import oscompat  # noqa: E402
UID = oscompat.uid()
ACTIONS = {"start", "stop", "kickstart", "load", "unload"}
DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


# ---------------------------------------------------------------- schedule in words
def _hhmm(d: dict) -> str:
    h, m = d.get("Hour"), d.get("Minute")
    if h is None and m is None:
        return "every minute"
    if h is None:
        return f"at :{int(m):02d} of every hour"
    return f"at {int(h):02d}:{int(m or 0):02d}"


def describe_schedule(pl: dict) -> str:
    bits = []
    iv = pl.get("StartInterval")
    if iv:
        iv = int(iv)
        if iv % 3600 == 0:
            bits.append(f"every {iv // 3600}h")
        elif iv % 60 == 0:
            bits.append(f"every {iv // 60} min")
        else:
            bits.append(f"every {iv}s")
    cal = pl.get("StartCalendarInterval")
    if cal:
        for d in (cal if isinstance(cal, list) else [cal]):
            if not isinstance(d, dict):
                continue
            when = _hhmm(d)
            if "Weekday" in d:
                try:
                    when += f" on {DAYS[int(d['Weekday']) % 7]}"
                except Exception:
                    pass
            if "Day" in d:
                when += f" on day {d['Day']} of the month"
            if "Month" in d:
                when += f" in month {d['Month']}"
            bits.append(when)
    if pl.get("RunAtLoad"):
        bits.append("at load")
    if pl.get("KeepAlive"):
        ka = pl["KeepAlive"]
        bits.append("kept alive" + ("" if ka is True else f" ({', '.join(sorted(ka))})" if isinstance(ka, dict) else ""))
    if pl.get("WatchPaths"):
        bits.append(f"when {len(pl['WatchPaths'])} watched path(s) change")
    if pl.get("QueueDirectories"):
        bits.append("when a queue directory has files")
    return ", ".join(bits) or "manual only"


def _matches(d: dict, lt) -> bool:
    if not isinstance(d, dict):
        return False
    if "Minute" in d and lt.tm_min != int(d["Minute"]):
        return False
    if "Hour" in d and lt.tm_hour != int(d["Hour"]):
        return False
    if "Weekday" in d and (lt.tm_wday + 1) % 7 != int(d["Weekday"]) % 7:
        return False
    if "Day" in d and lt.tm_mday != int(d["Day"]):
        return False
    if "Month" in d and lt.tm_mon != int(d["Month"]):
        return False
    return True


_CAL_CACHE: dict[tuple, tuple[float, float | None]] = {}


def _calendar_scan(pl: dict, now: float, forward: bool) -> float | None:
    cal = pl.get("StartCalendarInterval")
    if not cal:
        return None
    entries = [d for d in (cal if isinstance(cal, list) else [cal]) if isinstance(d, dict)]
    if not entries:
        return None
    # One answer per (calendar, direction, minute) - 32 jobs x 2 directions x 11,520 minutes of
    # pure-python scanning was 185 ms of every /api/jobs call.
    key = (json.dumps(entries, sort_keys=True, default=str), forward, int(now // 60))
    hit = _CAL_CACHE.get(key)
    if hit is not None:
        return hit[1]
    if len(_CAL_CACHE) > 512:
        _CAL_CACHE.clear()
    sign = 1 if forward else -1
    # Minutes-of-hour any entry can fire at (None = unconstrained, scan every minute).
    minutes: set[int] | None = set()
    for d in entries:
        if "Minute" not in d:
            minutes = None
            break
        minutes.add(int(d["Minute"]) % 60)
    found = None
    for step in range(0, 8 * 24 * 60):  # minute resolution, one week either way
        t = now + sign * step * 60
        lt = time.localtime(t)
        if minutes is not None and lt.tm_min not in minutes:
            continue
        if any(_matches(d, lt) for d in entries):
            found = t
            break
    _CAL_CACHE[key] = (time.time(), found)
    return found


def next_fire(pl: dict, now: float | None = None) -> str | None:
    """Next calendar fire, as an ISO local timestamp (best effort, next 7 days)."""
    t = _calendar_scan(pl, now or time.time(), forward=True)
    return time.strftime("%Y-%m-%dT%H:%M", time.localtime(t)) if t else None


def prev_fire(pl: dict, now: float | None = None) -> float | None:
    """Epoch of the most recent scheduled fire (best effort, last 7 days)."""
    return _calendar_scan(pl, now or time.time(), forward=False)


# ---------------------------------------------------------------- evidence of work
DISPATCH_LINE = re.compile(r"^==\s+(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d)\s+([\w.-]+)\s*$", re.M)
_ACT_CACHE: dict[str, tuple[float, dict]] = {}


def dispatch_logs() -> list[Path]:
    """Any `<project>/logs/ops_dispatch.log` under ~/Desktop - the applet dispatchers write here."""
    out = []
    desktop = HOME / "Desktop"
    try:
        for d in desktop.iterdir():
            p = d / "logs" / "ops_dispatch.log"
            if p.is_file():
                out.append(p)
    except OSError:
        pass
    return out


def dispatch_activity(tail_bytes: int = 400_000) -> dict[str, float]:
    """{mode: epoch of its most recent dispatch}. One tail read per log, cached."""
    hit = _ACT_CACHE.get("dispatch")
    if hit and time.time() - hit[0] < 20:
        return hit[1]
    modes: dict[str, float] = {}
    for p in dispatch_logs():
        try:
            size = p.stat().st_size
            with open(p, "rb") as fh:
                if size > tail_bytes:
                    fh.seek(size - tail_bytes)
                    fh.readline()
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        for ts, mode in DISPATCH_LINE.findall(text):
            try:
                epoch = time.mktime(time.strptime(ts.replace("T", " "), "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                continue
            if epoch > modes.get(mode, 0):
                modes[mode] = epoch
    _ACT_CACHE["dispatch"] = (time.time(), modes)
    return modes


def _log_path_users() -> dict[str, int]:
    """How many jobs write to each stdout/stderr path - a shared file's mtime proves nothing
    about any single job (several agents can share one .err file)."""
    hit = _ACT_CACHE.get("logusers")
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    users: dict[str, int] = {}
    for f in list_plists():
        try:
            pl = plistlib.loads(f.read_bytes())
        except Exception:
            continue
        for key in ("StandardOutPath", "StandardErrorPath"):
            v = pl.get(key)
            if v:
                users[str(v)] = users.get(str(v), 0) + 1
    _ACT_CACHE["logusers"] = (time.time(), users)
    return users


def running_commands(ttl: float = 15.0) -> dict[int, str]:
    """{pid: command line} for everything running, cached - used to spot a KeepAlive job's
    long-running child process, which is the best evidence such a job is doing its work."""
    hit = _ACT_CACHE.get("ps")
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    rc, out, _ = run_cmd(["/bin/ps", "-axo", "pid=,command="], timeout=8)
    procs: dict[int, str] = {}
    if rc == 0:
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                procs[int(parts[0])] = parts[1]
    _ACT_CACHE["ps"] = (time.time(), procs)
    return procs


def _mtime(path: str | None) -> float | None:
    try:
        return os.path.getmtime(path) if path else None
    except OSError:
        return None


def job_evidence(pl: dict) -> dict:
    """Most recent proof this job did something, and where that proof came from."""
    env = {str(k): str(v) for k, v in (pl.get("EnvironmentVariables") or {}).items()}
    args = [str(a) for a in (pl.get("ProgramArguments") or [])]
    applet = any("/Contents/MacOS/" in a or a.endswith(".app") for a in args)
    best_ts, best_src, mode = None, None, None

    # 1. applet dispatcher: match the job's mode against the dispatch log
    acts = dispatch_activity()
    for val in env.values():
        if val in acts:
            mode = val
            if best_ts is None or acts[val] > best_ts:
                best_ts, best_src = acts[val], f"ops_dispatch.log ({val})"

    # 2. its own stdout/stderr, but only when no other job writes to the same file
    shared = _log_path_users()
    for key in ("StandardOutPath", "StandardErrorPath"):
        pth = pl.get(key)
        if not pth or shared.get(str(pth), 0) > 1:
            continue
        m = _mtime(str(pth))
        if m and (best_ts is None or m > best_ts):
            best_ts, best_src = m, os.path.basename(str(pth))

    # 3. a still-running process named after the job's mode (KeepAlive loops the applet spawned)
    if mode:
        stem = mode.replace("-", "_")
        needles = (f"{stem}.py", f"{stem}.sh", f"/{stem}", f"{mode}.py", f"{mode}.sh")
        for pid, cmdline in running_commands().items():
            if any(n in cmdline for n in needles):
                best_ts, best_src = time.time(), f"process {pid} running ({stem})"
                break

    # 4. any log file named in the arguments or the environment
    for cand in [*args, *env.values()]:
        if cand.endswith((".log", ".jsonl", ".json", ".csv")) and cand.startswith("/"):
            if shared.get(cand, 0) > 1:
                continue
            m = _mtime(cand)
            if m and (best_ts is None or m > best_ts):
                best_ts, best_src = m, os.path.basename(cand)
    return {"last_activity_epoch": best_ts, "evidence": best_src, "mode": mode, "applet": applet}


def assess_health(pl: dict, st: dict, ev: dict, now: float | None = None) -> dict:
    """healthy | stale | failing | unknown | not loaded - from activity, not from the exit code."""
    now = now or time.time()
    ts = ev.get("last_activity_epoch")
    age = (now - ts) if ts else None
    interval = pl.get("StartInterval")
    prev = prev_fire(pl, now)
    keepalive = bool(pl.get("KeepAlive"))

    if interval:
        window = 2 * int(interval)
        expected = f"every {int(interval)}s, so within {window}s"
    elif prev:
        window = max(600.0, now - prev + 300)
        expected = f"last scheduled fire {time.strftime('%Y-%m-%dT%H:%M', time.localtime(prev))}"
    elif keepalive:
        window = 3600.0
        expected = "kept alive (the process itself is the evidence)"
    else:
        window = None
        expected = "no schedule - manual only"

    code = st.get("last_exit_code")
    if not st.get("loaded"):
        status, why = "not loaded", "not bootstrapped into this GUI domain"
    elif keepalive and st.get("pid"):
        status, why = "healthy", f"running as pid {st['pid']}"
    elif age is not None and window is not None and age <= window:
        status, why = "healthy", f"worked {int(age)}s ago ({ev['evidence']}); expected {expected}"
    elif age is not None and window is not None:
        status, why = "stale", f"last worked {int(age // 60)} min ago ({ev['evidence']}); expected {expected}"
    elif age is not None:
        status, why = "healthy", f"worked {int(age)}s ago ({ev['evidence']}); no schedule to compare against"
    elif code == 78 and ev.get("applet"):
        # an applet's EX_CONFIG says nothing either way, so with no activity we simply do not know
        status, why = "unknown", "applet job: exit 78 is not a failure signal and nothing it writes was found"
    elif code not in (0, None) and (st.get("runs") or 0) > 0:
        status, why = "failing", f"exit {st.get('last_exit')} and nothing it writes has moved"
    else:
        status, why = "unknown", "no log or dispatch evidence to judge by"

    out = {"status": status, "health_reason": why, "expected_window_s": window,
           "last_activity": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None,
           "last_activity_age_s": round(age) if age is not None else None,
           "evidence": ev.get("evidence"), "dispatch_mode": ev.get("mode")}
    if code == 78 and ev.get("applet"):
        out["exit_note"] = ("78 from an AppleScript applet is not a failure signal - the applet does "
                            "the work and still reports EX_CONFIG to launchd")
    elif code not in (0, None):
        out["exit_note"] = f"launchd recorded exit {st.get('last_exit')}"
    return out


# ---------------------------------------------------------------- launchctl
_PRINT_CACHE: dict[str, tuple[float, dict]] = {}


def launchctl_state(label: str, ttl: float = 5.0) -> dict:
    hit = _PRINT_CACHE.get(label)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    rc, out, _ = run_cmd(["/bin/launchctl", "print", f"gui/{UID}/{label}"], timeout=6)
    st: dict = {"loaded": rc == 0}
    if rc == 0:
        m = re.search(r"^\s*pid\s*=\s*(\d+)", out, re.M)
        st["pid"] = int(m.group(1)) if m else None
        m = re.search(r"^\s*last exit (?:code|status)\s*=\s*(.+?)\s*$", out, re.M)
        raw = m.group(1) if m else None
        st["last_exit"] = raw
        code = None
        if raw and raw[0].isdigit():
            code = int(re.match(r"(\d+)", raw).group(1))
        elif raw and raw.strip() in ("0",):
            code = 0
        st["last_exit_code"] = code
        st["never_exited"] = bool(raw and "never" in raw.lower())
        m = re.search(r"^\s*state\s*=\s*(\S+)", out, re.M)
        st["state"] = m.group(1) if m else ("running" if st.get("pid") else "waiting")
        m = re.search(r"^\s*runs\s*=\s*(\d+)", out, re.M)
        st["runs"] = int(m.group(1)) if m else None
    _PRINT_CACHE[label] = (time.time(), st)
    return st


def prefetch_states(labels: list[str], ttl: float = 5.0) -> None:
    """Warm _PRINT_CACHE for many labels at once. `launchctl print` costs ~35 ms each and
    32 of them in series was the bulk of a cold /api/jobs (1.2 s); in parallel it is ~120 ms."""
    todo = [l for l in labels
            if not (_PRINT_CACHE.get(l) and time.time() - _PRINT_CACHE[l][0] < ttl)]
    if len(todo) < 2:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
        list(pool.map(lambda l: launchctl_state(l, ttl=ttl), todo))


def tail_file(path: str | None, lines: int = 20) -> list[str]:
    if not path:
        return []
    try:
        p = Path(path)
        size = p.stat().st_size
        with open(p, "rb") as fh:
            if size > 200_000:
                fh.seek(size - 200_000)
                fh.readline()
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    return data.splitlines()[-lines:]


def read_job(f: Path, with_logs: int = 0) -> dict:
    try:
        pl = plistlib.loads(f.read_bytes())
    except Exception as e:
        return {"label": f.stem, "plist": str(f), "error": f"unreadable plist: {e}", "disabled": f.suffix == ".disabled"}
    label = pl.get("Label") or f.stem
    args = pl.get("ProgramArguments") or ([pl["Program"]] if pl.get("Program") else [])
    st = launchctl_state(label)
    ev = job_evidence(pl)
    health = assess_health(pl, st, ev)
    out_log, err_log = pl.get("StandardOutPath"), pl.get("StandardErrorPath")
    try:
        mtime = f.stat().st_mtime
    except OSError:
        mtime = None
    job = {
        "label": label,
        "plist": str(f),
        "disabled_file": f.name.endswith(".disabled"),
        "disabled_key": bool(pl.get("Disabled")),
        "program": args,
        "program_str": " ".join(str(a) for a in args)[:400],
        "working_dir": pl.get("WorkingDirectory"),
        "schedule": describe_schedule(pl),
        "next_fire": next_fire(pl),
        "run_at_load": bool(pl.get("RunAtLoad")),
        "keep_alive": pl.get("KeepAlive") if not isinstance(pl.get("KeepAlive"), dict) else sorted(pl["KeepAlive"]),
        "stdout_path": out_log,
        "stderr_path": err_log,
        "plist_mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime)) if mtime else None,
        "project": db.derive_project(str(pl.get("WorkingDirectory") or (args[1] if len(args) > 1 else ""))),
        "env": {str(k): str(v) for k, v in (pl.get("EnvironmentVariables") or {}).items()},
        **st,
        **health,
    }
    if with_logs:
        job["stdout_tail"] = tail_file(out_log, with_logs)
        job["stderr_tail"] = tail_file(err_log, with_logs)
        for p in (out_log, err_log):
            if p:
                try:
                    job.setdefault("log_sizes", {})[p] = os.path.getsize(p)
                except OSError:
                    job.setdefault("log_sizes", {})[p] = None
    return job


def list_plists() -> list[Path]:
    if not AGENTS.is_dir():
        return []
    out = [f for f in AGENTS.iterdir()
           if f.is_file() and (f.name.endswith(".plist") or f.name.endswith(".plist.disabled"))]
    return sorted(out, key=lambda f: f.name.lower())


def _find(label: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._\-]{1,120}", label):
        raise ApiError(400, "bad label")
    for f in list_plists():
        try:
            pl = plistlib.loads(f.read_bytes())
        except Exception:
            pl = {}
        if (pl.get("Label") or f.stem) == label or f.stem == label:
            return f
    raise ApiError(404, f"no launchd job named {label}")


# ---------------------------------------------------------------- handlers
def get_jobs(query=None, body=None, match=None):
    query = query or {}
    logs = int(query.get("logs") or 0)
    files = list_plists()
    labels = []
    for f in files:
        try:
            labels.append(plistlib.loads(f.read_bytes()).get("Label") or f.stem)
        except Exception:
            labels.append(f.stem)
    prefetch_states(labels)
    jobs = [read_job(f, with_logs=logs) for f in files]
    if query.get("q"):
        needle = query["q"].lower()
        jobs = [j for j in jobs if needle in json.dumps(j).lower()]
    loaded = sum(1 for j in jobs if j.get("loaded"))
    running = sum(1 for j in jobs if j.get("pid"))
    by_status: dict[str, int] = {}
    for j in jobs:
        by_status[j.get("status", "unknown")] = by_status.get(j.get("status", "unknown"), 0) + 1
    failing = [j["label"] for j in jobs if j.get("status") == "failing"]
    stale = [j["label"] for j in jobs if j.get("status") == "stale"]
    return 200, {"ok": True, "count": len(jobs), "loaded": loaded, "running": running,
                 "by_status": by_status, "failing": failing, "stale": stale,
                 "note": ("health comes from evidence of work (dispatch log / unshared job logs), "
                          "not from the launchd exit code - an AppleScript applet reports 78 even "
                          "when it did the work"),
                 "jobs": jobs}


def get_job(query=None, body=None, match=None):
    label = match.group("label")
    query = query or {}
    return 200, {"ok": True, "job": read_job(_find(label), with_logs=int(query.get("lines") or 40))}


def post_action(query=None, body=None, match=None):
    label = match.group("label")
    body = body or {}
    action = str(body.get("action", "")).strip().lower()
    if action not in ACTIONS:
        raise ApiError(400, f"action must be one of {'|'.join(sorted(ACTIONS))}")
    f = _find(label)
    target = f"gui/{UID}/{label}"
    if action == "kickstart":
        argv = ["/bin/launchctl", "kickstart", "-k", target]
    elif action == "start":
        argv = ["/bin/launchctl", "start", label]
    elif action == "stop":
        argv = ["/bin/launchctl", "stop", label]
    elif action == "load":
        argv = ["/bin/launchctl", "bootstrap", f"gui/{UID}", str(f)]
    else:
        argv = ["/bin/launchctl", "bootout", target]
    rc, out, err = run_cmd(argv, timeout=20)
    _PRINT_CACHE.pop(label, None)
    con = db.connect()
    db.add_event(con, "cc", "action", {"module": "jobs", "action": action, "label": label, "rc": rc,
                                       "stderr": (err or "")[:200]})
    db.log_line("jobs", f"{action} {label} rc={rc} {(err or out or '').strip()[:160]}")
    if rc != 0:
        raise ApiError(500, f"launchctl {action} failed (rc={rc}): {(err or out or '').strip()[:200]}")
    return 200, {"ok": True, "label": label, "action": action, "state": launchctl_state(label, ttl=0)}


def post_logtail(query=None, body=None, match=None):
    label = match.group("label")
    body = body or {}
    try:
        lines = max(1, min(2000, int(body.get("lines", 200))))
    except (TypeError, ValueError):
        raise ApiError(400, "lines must be an integer")
    job = read_job(_find(label), with_logs=lines)
    return 200, {"ok": True, "label": label, "stdout_path": job.get("stdout_path"),
                 "stderr_path": job.get("stderr_path"),
                 "stdout_tail": job.get("stdout_tail", []), "stderr_tail": job.get("stderr_tail", [])}


ROUTES = [
    ("GET", r"/api/jobs/?$", get_jobs),
    ("GET", r"/api/jobs/(?P<label>[A-Za-z0-9._\-]+)/?$", get_job),
    ("POST", r"/api/jobs/(?P<label>[A-Za-z0-9._\-]+)/action/?$", post_action),
    ("POST", r"/api/jobs/(?P<label>[A-Za-z0-9._\-]+)/logtail/?$", post_logtail),
]

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Command Center jobs module — launchd agents on this Mac.")
    ap.add_argument("--label", help="show one job with its log tail")
    ap.add_argument("--lines", type=int, default=20)
    a = ap.parse_args()
    if a.label:
        print(json.dumps(get_job({"lines": str(a.lines)}, None, re.match(r"(?P<label>.+)", a.label))[1], indent=2, default=str))
    else:
        print(json.dumps(get_jobs()[1], indent=2, default=str))
