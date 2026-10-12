#!/usr/bin/env python3
"""HTTP smoke of every route the server advertises at /api/modules, plus the core
routes the app and the menu bar depend on.

Called by tests/run_all.sh. Not named test_*.py on purpose: run_all.sh runs it
once, at the end, as its own suite.

    python3 tests/smoke_routes.py [-v] [--base http://127.0.0.1:8790]

What it does:
  * GETs every advertised route, filling in the ones that need a parameter from
    real data it just read (a job label, a memory path, a session id).
  * A route that answers 405 is POST-only. Those are NOT smoked here — they
    mutate things. tests/test_api_modules.py probes them with input that must be
    refused; this file only reads.
  * Fails on any non-200, on a route slower than SLOW_FAIL seconds, and if any
    advertised route was never reached.

If nothing is listening it starts its own server on a free port against the real
DB, so the suite works on a machine where launchd is not running. It says which
one it used.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

VERBOSE = "-v" in sys.argv
SLOW_WARN = 1.0      # QA's route budget: a module route should answer inside a second
SLOW_FAIL = 5.0
TIMEOUT = 30

# Core routes that are not owned by a module but that the app and the menu bar
# both depend on. Kept explicit so a regression in one of them fails this suite.
CORE = ["/health", "/api/stats", "/api/items?limit=5", "/api/sessions?limit=5",
        "/api/projects", "/api/modules"]

FAILS: list[str] = []
SLOW: list[str] = []
OK = 0
SKIPPED: list[str] = []


def base_url() -> tuple[str, subprocess.Popen | None]:
    """The running server if there is one, otherwise a fresh one we own."""
    for i, a in enumerate(sys.argv):
        if a == "--base":
            return sys.argv[i + 1], None
    try:
        port = int(__import__("db").config().get("port", 8790))
    except Exception:
        port = 8790
    url = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(url + "/health", timeout=3):
            print(f"smoking the running server at {url}")
            return url, None
    except Exception:
        pass
    s = socket.socket(); s.bind(("127.0.0.1", 0)); free = s.getsockname()[1]; s.close()
    url = f"http://127.0.0.1:{free}"
    proc = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--port", str(free)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(60):
        time.sleep(0.25)
        try:
            with urllib.request.urlopen(url + "/health", timeout=2):
                print(f"nothing was listening — started our own server at {url}")
                return url, proc
        except Exception:
            if proc.poll() is not None:
                break
    proc.kill()
    print("could not start a server; is server.py broken?")
    raise SystemExit(1)


def get(url: str) -> tuple[int, object, float]:
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            raw = r.read()
            dt = time.time() - t0
            try:
                return r.status, json.loads(raw), dt
            except Exception:
                return r.status, {"_bytes": len(raw)}, dt
    except urllib.error.HTTPError as e:
        raw = e.read()
        dt = time.time() - t0
        try:
            return e.code, json.loads(raw), dt
        except Exception:
            return e.code, {"raw": raw[:200].decode(errors="replace")}, dt
    except Exception as e:
        return 0, {"error": str(e)[:200]}, time.time() - t0


def smoke(base: str, path: str, why: str = "") -> None:
    global OK
    st, body, dt = get(base + path)
    label = path if not why else f"{path}   ({why})"
    if st == 405:
        SKIPPED.append(path + " — POST only, see tests/test_api_modules.py")
        if VERBOSE:
            print(f"  skip {label}")
        return
    if st != 200:
        FAILS.append(f"{path} -> {st} {json.dumps(body)[:200]}")
        print(f"  FAIL {st:>3} {dt:5.2f}s {label}")
        return
    OK += 1
    if dt > SLOW_FAIL:
        FAILS.append(f"{path} answered in {dt:.1f}s (budget {SLOW_FAIL}s)")
        print(f"  SLOW {st:>3} {dt:5.2f}s {label}  <- over the {SLOW_FAIL}s hard budget")
    elif dt > SLOW_WARN:
        SLOW.append(f"{path} {dt:.2f}s")
        if VERBOSE:
            print(f"  warn {st:>3} {dt:5.2f}s {label}")
    elif VERBOSE:
        print(f"  ok   {st:>3} {dt:5.2f}s {label}")


def discover(base: str) -> dict:
    """Real values to fill the routes that need a parameter."""
    out: dict = {}
    st, b, _ = get(base + "/api/jobs")
    if st == 200 and (b.get("jobs") or []):
        out["label"] = b["jobs"][0]["label"]
    st, b, _ = get(base + "/api/memory/index")
    if st == 200 and (b.get("files") or []):
        f = b["files"][0]
        out["memory_path"] = f.get("path") or f.get("file")
    # The sid must belong to a session whose transcript is INDEXED, not merely to
    # a row in `sessions` — /api/search/session 404s for an unindexed one. A search
    # hit is by definition indexed, so take the sid from there.
    st, b, _ = get(base + "/api/search?q=usage&limit=1")
    hits = (b or {}).get("results") or []
    if st == 200 and hits:
        out["sid"] = hits[0].get("session_id")
    return out


def concrete(route: str, d: dict) -> tuple[str | None, str]:
    """A regex route from /api/modules -> a real path, plus why it looks like that.
    Returns (None, reason) when we have no data to fill it with."""
    p = route
    for suffix in ("/?$", "/?", "$"):
        if p.endswith(suffix):
            p = p[: -len(suffix)]
    if "(?P<label>" in p:
        if not d.get("label"):
            return None, "no launchd jobs to name"
        p = p.replace(r"(?P<label>[A-Za-z0-9._\-]+)", d["label"])
    if "(?P<" in p:                       # an unknown parameter we cannot fill
        return None, "unfilled parameter"
    q = {
        "/api/config/skill": {"name": "log-build"},
        "/api/handoff": {"cwd": str(ROOT)},
        "/api/launch/project": {"path": str(ROOT)},
        "/api/memory/file": {"path": d.get("memory_path")},
        "/api/search": {"q": "usage"},
        "/api/search/session": {"sid": d.get("sid")},
    }.get(p)
    if q is not None:
        if any(v in (None, "") for v in q.values()):
            return None, "no data to fill its parameter"
        return p + "?" + urllib.parse.urlencode(q), ", ".join(f"{k}={v}" for k, v in q.items())[:60]
    return p, ""


def main() -> int:
    base, proc = base_url()
    try:
        st, mods, _ = get(base + "/api/modules")
        if st != 200 or not isinstance(mods, dict):
            print(f"  FAIL /api/modules -> {st} {mods}")
            return 1
        errors = mods.get("errors") or {}
        if errors:
            FAILS.append(f"/api/modules reports import errors: {json.dumps(errors)[:300]}")
            print(f"  FAIL module import errors: {json.dumps(errors)[:200]}")

        for p in CORE:
            smoke(base, p)

        d = discover(base)
        listed = 0
        for m in mods.get("modules", []):
            for route in m.get("routes", []):
                listed += 1
                path, why = concrete(route, d)
                if path is None:
                    SKIPPED.append(f"{route} — {why}")
                    if VERBOSE:
                        print(f"  skip {route} — {why}")
                    continue
                smoke(base, path, why)

        n_mod = len(mods.get("modules", []))
        print(f"\n{OK} routes answered 200 · {len(SKIPPED)} skipped · "
              f"{listed} advertised by {n_mod} modules")
        if SLOW:
            print(f"over the {SLOW_WARN}s soft budget: " + ", ".join(SLOW))
        if not VERBOSE and SKIPPED:
            print("skipped: " + "; ".join(SKIPPED[:8]) + (" …" if len(SKIPPED) > 8 else ""))
        if FAILS:
            print(f"\n{len(FAILS)} failure(s):")
            for f in FAILS:
                print("  - " + f)
            return 1
        print("all green")
        return 0
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
