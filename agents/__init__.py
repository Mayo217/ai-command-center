"""AI tool adapters. Each module in this folder that defines `ADAPTER` is picked up automatically.

    tracker agents                      # every known tool: installed? connected? what it supports
    tracker agents connect codex        # teach a tool the ledger (writes its instruction file)
    tracker agents disconnect codex
    tracker agents scan [codex]         # import sessions and plan limits from tools' own logs
    tracker agents new windsurf         # scaffold an adapter for another tool
"""
from __future__ import annotations

import importlib
import json
import pkgutil
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def all_adapters() -> dict:
    found = {}
    for m in sorted(pkgutil.iter_modules([str(HERE)]), key=lambda m: m.name):
        if m.name.startswith("_") or m.name in ("base",):
            continue
        try:
            mod = importlib.import_module(f"agents.{m.name}")
        except Exception as e:  # one broken adapter must not take the others down
            print(f"warning: adapter {m.name} failed to load: {e}", file=sys.stderr)
            continue
        a = getattr(mod, "ADAPTER", None)
        if a is not None:
            found[a.name] = a
    order = ["claude", "codex", "gemini"]
    return dict(sorted(found.items(), key=lambda kv: (order.index(kv[0]) if kv[0] in order else 50, kv[0])))


def get(name: str):
    a = all_adapters().get(name)
    if a is None:
        raise SystemExit(f"error: no adapter '{name}'. Known: {', '.join(all_adapters())}. "
                         f"Add one with: tracker agents new {name}")
    return a


TEMPLATE = '''"""{display}: <one line on what this adapter covers>.

Fill in what the tool supports. Only `rules` is needed for the ledger to work: the tool then logs
with `tracker log --agent {name}`. Add `scan` once you know where the tool keeps its session logs.
"""
from agents.base import Adapter


class {cls}(Adapter):
    name = "{name}"
    display = "{display}"
    vendor = ""
    binaries = ("{name}",)                 # command names that mean it is installed
    data_dirs = (".{name}",)               # or folders under your home folder
    rules_file = None                      # its global instruction file, relative to home, if any
    capabilities = {{"capture": False, "rules": True, "gate": False, "usage": False}}
    notes = "rules only; add scan() to read its session logs"

    # def scan(self, con, full=False):     # read the tool's logs into the sessions table
    #     ...                              # see agents/codex.py for a complete example
    #     return {{"files": 0, "sessions": 0}}


ADAPTER = {cls}()
'''


def _yn(v):
    return "yes" if v else "-"


def cli(argv: list[str]) -> int:
    import argparse
    import db
    ap = argparse.ArgumentParser(prog="tracker agents", description="AI tools connected to the ledger")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("connect", "disconnect"):
        sp = sub.add_parser(name)
        sp.add_argument("name")
        sp.add_argument("--project", help="for project-level tools (generic): the folder")
    sp = sub.add_parser("scan")
    sp.add_argument("name", nargs="?")
    sp.add_argument("--full", action="store_true", help="re-read every log, not just changed ones")
    sp = sub.add_parser("new")
    sp.add_argument("name")
    sp.add_argument("--display")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd in (None,):
        con = db.connect()
        db.migrate(con)
        rows = []
        for ad in all_adapters().values():
            n = con.execute("SELECT count(*) n FROM sessions WHERE agent=?", (ad.name,)).fetchone()["n"]
            i = con.execute("SELECT count(*) n FROM items WHERE agent=?", (ad.name,)).fetchone()["n"]
            rows.append({"name": ad.name, "display": ad.display, "vendor": ad.vendor, "installed": ad.detected(),
                         "connected": ad.connected(), **ad.capabilities, "sessions": n, "items": i,
                         "limits": ad.usage(con), "notes": ad.notes})
        if a.json:
            print(json.dumps(rows, indent=1, default=str))
            return 0
        print(f"{'agent':<9} {'tool':<20} {'installed':<10} {'connected':<10} {'capture':<8} {'gate':<5} "
              f"{'usage':<6} {'sessions':>8} {'items':>6}")
        for r in rows:
            print(f"{r['name']:<9} {r['display']:<20} {_yn(r['installed']):<10} {_yn(r['connected']):<10} "
                  f"{_yn(r['capture']):<8} {_yn(r['gate']):<5} {_yn(r['usage']):<6} {r['sessions']:>8} {r['items']:>6}")
            for u in r["limits"] or []:
                mins = u.get("window_minutes") or 0
                span = f"{mins // 1440}d" if mins >= 1440 else f"{mins // 60}h"
                print(f"{'':<9} plan {u.get('plan') or '?'}: {span} window {u.get('used_pct') or 0:.0f}% used")
        return 0

    if a.cmd == "connect":
        print(get(a.name).connect(a.project))
    elif a.cmd == "disconnect":
        print(get(a.name).disconnect(a.project))
    elif a.cmd == "scan":
        con = db.connect()
        db.migrate(con)
        targets = [get(a.name)] if a.name else [x for x in all_adapters().values() if x.capabilities.get("capture")
                                                and x.name != "claude"]
        for ad in targets:
            print(f"{ad.name}: {ad.scan(con, full=a.full)}")
    elif a.cmd == "new":
        name = re.sub(r"[^a-z0-9]+", "_", a.name.lower()).strip("_")
        if not name or name in ("base", "generic"):
            raise SystemExit("error: pick another name")
        f = HERE / f"{name}.py"
        if f.exists():
            raise SystemExit(f"error: {f} already exists")
        cls = "".join(w.title() for w in name.split("_"))
        f.write_text(TEMPLATE.format(name=name.replace("_", "-"), display=a.display or a.name, cls=cls),
                     encoding="utf-8")
        print(f"created {f}\nfill it in, then: tracker agents connect {name.replace('_', '-')}")
    return 0
