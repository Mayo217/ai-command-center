"""Command Center module 11 — Handoff & digest.

GET /api/handoff?project=   the "State of <project>" markdown the CLI writes
GET /api/handoff/digest     the current week's digest markdown

`tracker handoff [--project P|--cwd .] [--write]` and `tracker digest` render the same text, so
the page and the terminal can never drift. `--write` saves HANDOFF_AUTO.md next to the project -
a human-written HANDOFF.md is never touched.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from modules.common import ApiError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = Path.home() / "Desktop"
KIND_ORDER = ["build", "enhancement", "automation", "config", "fix", "doc", "analysis", "research", "other"]


def _first_path(con, item_id: int) -> str | None:
    r = con.execute("SELECT path FROM item_files WHERE item_id=? ORDER BY CASE role WHEN 'entry' THEN 0 "
                    "WHEN 'created' THEN 1 ELSE 2 END, id LIMIT 1", (item_id,)).fetchone()
    return r["path"] if r else None


def render_handoff(con, project: str) -> str:
    items = con.execute(
        "SELECT * FROM items WHERE project=? AND status NOT IN ('deleted') "
        "ORDER BY COALESCE(built_at, created_at) DESC", (project,)).fetchall()
    if not items:
        return f"# State of {project}\n\n_Nothing logged for this project yet._\n"
    active = [i for i in items if i["status"] == "active"]
    wip = [i for i in items if i["status"] == "wip"]
    out = [f"# State of {project}",
           f"_Generated {time.strftime('%Y-%m-%d %H:%M')} by `tracker handoff` from the build ledger._",
           "", f"{len(active)} active item(s), {len(wip)} in progress, {len(items)} total.", ""]

    by_kind: dict[str, list] = {}
    for i in active:
        by_kind.setdefault(i["kind"], []).append(i)
    for kind in [k for k in KIND_ORDER if k in by_kind] + [k for k in by_kind if k not in KIND_ORDER]:
        out.append(f"## {kind}")
        out.append("")
        for i in by_kind[kind]:
            path = _first_path(con, i["id"])
            summary = " ".join((i["summary"] or "").split())
            out.append(f"### #{i['id']} {i['title']}")
            out.append(summary[:400] + ("..." if len(summary) > 400 else ""))
            if path:
                out.append(f"- entry: `{path}`" + ("" if os.path.exists(path) else "  **(missing)**"))
            if i["usage"]:
                out.append(f"- use: {' '.join(i['usage'].split())[:240]}")
            links = con.execute("SELECT url,label FROM item_links WHERE item_id=?", (i["id"],)).fetchall()
            for l in links:
                out.append(f"- link: [{l['label'] or l['url']}]({l['url']})")
            out.append("")

    if wip:
        out += ["## in progress", ""]
        for i in wip:
            conf = " _(auto-captured, needs a rewrite)_" if i["confidence"] == "inferred" else ""
            out.append(f"- #{i['id']} {i['title']}{conf}")
        out.append("")

    sessions = con.execute(
        "SELECT session_id, title, first_prompt, COALESCE(ended_at, started_at, created_at) ts, cost_usd "
        "FROM sessions WHERE project=? ORDER BY ts DESC LIMIT 5", (project,)).fetchall()
    if sessions:
        out += ["## recent sessions", ""]
        for s in sessions:
            cost = f" · ${s['cost_usd']:.2f}" if s["cost_usd"] else ""
            title = (s["title"] or (s["first_prompt"] or "")[:70] or s["session_id"][:8]).replace("\n", " ")
            out.append(f"**{(s['ts'] or '')[:10]} · {title[:90]}**{cost}  ")
            recap = db.session_recap(con, s["session_id"], 300)
            out.append(f"{recap}" if recap else "_no recap captured_")
            out.append(f"`claude --resume {s['session_id']}`")
            out.append("")

    gaps = con.execute(
        "SELECT count(*) n FROM sessions WHERE project=? AND logged=0 AND waived_at IS NULL AND "
        "EXISTS(SELECT 1 FROM session_files f WHERE f.session_id=sessions.session_id AND f.ignored=0)",
        (project,)).fetchone()["n"]
    bypassed = con.execute("SELECT count(*) n FROM sessions WHERE project=? AND gate_bypassed>0",
                           (project,)).fetchone()["n"]
    out += ["## gaps", "",
            f"- {gaps} session(s) in this project changed files and never logged or waived",
            f"- {bypassed} session(s) bypassed the Stop gate",
            "", "_Check with `tracker sessions --unlogged --project " + project + "`._", ""]
    return "\n".join(out)


def project_dir(con, project: str) -> Path | None:
    """Where this project actually lives: the newest session cwd recorded for it, else the
    directory of its newest entry file, else ~/Desktop/<project>. Only an existing dir is returned."""
    # the directory most of this project's sessions ran in, newest first as the tie-break
    for r in con.execute(
            "SELECT cwd, count(*) n, max(COALESCE(ended_at, started_at, created_at)) last FROM sessions "
            "WHERE project=? AND cwd IS NOT NULL GROUP BY cwd ORDER BY n DESC, last DESC LIMIT 5", (project,)):
        p = Path(r["cwd"])
        if p.is_dir():
            return p
    for r in con.execute(
            "SELECT f.path FROM item_files f JOIN items i ON i.id=f.item_id "
            "WHERE i.project=? ORDER BY f.id DESC LIMIT 20", (project,)):
        p = Path(r["path"]).parent
        if p.is_dir():
            return p
    d = DESKTOP / project
    return d if d.is_dir() else None


def iso_week(d: datetime.date | None = None) -> str:
    d = d or datetime.date.today()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def render_digest(con, days: int = 7) -> str:
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    items = con.execute(
        "SELECT * FROM items WHERE COALESCE(built_at, created_at) >= ? AND status!='deleted' "
        "ORDER BY COALESCE(built_at, created_at) DESC", (since,)).fetchall()
    out = [f"# AI Command Center digest — {iso_week()}",
           f"_Week to {datetime.date.today().isoformat()}; {len(items)} item(s) built since {since}._", ""]
    by_project: dict[str, list] = {}
    for i in items:
        by_project.setdefault(i["project"] or "unknown", []).append(i)
    out.append("## what got built")
    out.append("")
    for proj in sorted(by_project, key=lambda p: -len(by_project[p])):
        out.append(f"**{proj}** ({len(by_project[proj])})  ")
        for i in by_project[proj][:12]:
            out.append(f"- #{i['id']} `{i['kind']}` {i['title']}")
        out.append("")

    try:
        costs = con.execute(
            "SELECT project, round(sum(cost_usd_est),2) cost, count(DISTINCT session_id) sessions "
            "FROM usage_events WHERE ts >= ? GROUP BY project ORDER BY cost DESC LIMIT 12",
            (since,)).fetchall()
        if costs:
            out += ["## cost by project", "", "| project | sessions | est cost |", "|---|---:|---:|"]
            out += [f"| {r['project'] or '-'} | {r['sessions']} | ${r['cost'] or 0:,.2f} |" for r in costs]
            out.append("")
    except Exception:
        pass

    unlogged = con.execute(
        "SELECT count(*) n FROM sessions WHERE COALESCE(started_at, created_at) >= ? AND logged=0 "
        "AND waived_at IS NULL AND EXISTS(SELECT 1 FROM session_files f "
        "WHERE f.session_id=sessions.session_id AND f.ignored=0)", (since,)).fetchone()["n"]
    out += ["## ledger hygiene", "", f"- {unlogged} session(s) this week changed files without logging",
            f"- {con.execute('SELECT count(*) n FROM items WHERE confidence=\'inferred\' AND status=\'wip\'').fetchone()['n']} "
            "auto-captured item(s) still awaiting a rewrite", ""]
    try:
        lim = con.execute("SELECT ts, five_hour_pct, seven_day_pct FROM rate_limit_samples "
                          "WHERE ts >= ? ORDER BY ts DESC LIMIT 14", (since,)).fetchall()
        if lim:
            out += ["## plan limits this week", "", "| when | 5h | 7d |", "|---|---:|---:|"]
            out += [f"| {(r['ts'] or '')[:16].replace('T', ' ')} | {r['five_hour_pct'] or 0:.0f}% | "
                    f"{r['seven_day_pct'] or 0:.0f}% |" for r in lim]
            out.append("")
    except Exception:
        pass
    return "\n".join(out)


# ---------------------------------------------------------------- handlers
def get_handoff(query=None, body=None, match=None):
    query = query or {}
    con = db.connect()
    project = query.get("project")
    if not project and query.get("cwd"):
        project = db.derive_project(query["cwd"])
    if not project:
        raise ApiError(400, "project= or cwd= is required")
    known = {r["project"] for r in con.execute("SELECT DISTINCT project FROM items WHERE project IS NOT NULL")}
    md = render_handoff(con, project)
    return 200, {"ok": True, "project": project, "known_project": project in known,
                 "markdown": md, "chars": len(md)}


def get_digest(query=None, body=None, match=None):
    query = query or {}
    try:
        days = max(1, min(90, int(query.get("days", 7))))
    except ValueError:
        raise ApiError(400, "days must be an integer")
    md = render_digest(db.connect(), days)
    return 200, {"ok": True, "week": iso_week(), "days": days, "markdown": md, "chars": len(md)}


ROUTES = [
    ("GET", r"/api/handoff/digest/?$", get_digest),
    ("GET", r"/api/handoff/?$", get_handoff),
]

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Command Center handoff module — state-of-project and weekly digest.")
    ap.add_argument("--project", default="ai-command-center")
    ap.add_argument("--digest", action="store_true")
    a = ap.parse_args()
    print(render_digest(db.connect()) if a.digest else render_handoff(db.connect(), a.project))
