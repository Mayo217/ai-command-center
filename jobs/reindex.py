#!/usr/bin/env python3
"""Daily reindex — the job that makes the usage history permanent.

Claude Code deletes transcripts under `~/.claude/projects` on a
rolling ~30-day window: on this Mac the oldest file on disk is, reliably, almost exactly 30
days old. The ledger already holds the mined history, but nothing guaranteed ongoing capture —
so any day nobody happened to run `backfill.py` before day 30 was lost for good, silently.

This runs `backfill.py --usage --transcripts` over whatever is new and records what it added.

**Cheap when there is nothing new.** A watermark (`meta.reindex_watermark`, the moment the last
scan began) is compared against a `stat()`-only walk of the projects dir — nothing is opened, so
the walk of 800+ files costs a few tens of milliseconds. No file newer than the watermark means
no backfill at all. When there IS something new, `--since` is set to the day before the oldest
new file so the mining passes only look at the groups that can have changed.

Every run writes `meta.reindex_last_run` (JSON), which `GET /api/usage/coverage` reports and
`tracker doctor` can age-check, so a job that has quietly stopped running is visible.

    python3 jobs/reindex.py            # what the LaunchAgent runs
    python3 jobs/reindex.py --json     # machine-readable
    python3 jobs/reindex.py --full     # ignore the watermark, re-scan everything
    python3 jobs/reindex.py --dry-run  # say what it would do, write nothing

Installed as `com.aicommandcenter.reindex` by `install.py --autostart` (daily 05:45 local — after
the overnight window in which a day's transcripts are still being written, and before a
normal working day starts).
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db          # noqa: E402
import backfill    # noqa: E402

WATERMARK_KEY = "reindex_watermark"
LAST_RUN_KEY = "reindex_last_run"
# The watermark is the wall-clock moment the scan STARTED, minus this margin — not the newest
# mtime found. Two reasons. (a) A file modified while the scan is walking the tree may be
# stat()ed before its final write; anchoring on the scan's start time means its new mtime is
# still greater than the watermark next time, so it is picked up rather than missed once and
# never again. (b) Anchoring on the newest mtime instead would make that same newest file
# qualify as "new" on every subsequent run, so the cheap "nothing new" path could never fire.
SLACK_S = 5.0


def _projects_dir() -> Path:
    return Path(db.config().get("projects_dir") or (Path.home() / ".claude/projects")).expanduser()


def _meta_get(con, key):
    try:
        r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    except Exception:
        return None
    return r["value"] if r else None


def _meta_set(con, key, value):
    con.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def scan(root: Path, since_epoch: float | None) -> dict:
    """stat()-only walk. Returns the newest mtime overall and the files newer than the mark."""
    newest = 0.0
    oldest_new = None
    n_files = 0
    new_files: list[str] = []
    if not root.is_dir():
        return {"exists": False, "files": 0, "new": [], "newest": 0.0, "oldest_new": None}
    for f in root.rglob("*.jsonl"):
        try:
            m = f.stat().st_mtime
        except OSError:
            continue
        n_files += 1
        newest = max(newest, m)
        if since_epoch is None or m > since_epoch:
            new_files.append(str(f))
            oldest_new = m if oldest_new is None else min(oldest_new, m)
    return {"exists": True, "files": n_files, "new": new_files, "newest": newest,
            "oldest_new": oldest_new}


def run(full: bool = False, dry_run: bool = False, quiet: bool = True) -> dict:
    t0 = time.time()
    started = db.now_iso()
    con = db.connect()
    try:
        root = _projects_dir()
        scan_started = time.time()
        mark = None if full else _meta_get(con, WATERMARK_KEY)
        try:
            mark_f = float(mark) if mark else None
        except (TypeError, ValueError):
            mark_f = None
        sc = scan(root, mark_f)

        rep: dict = {
            "started_at": started, "projects_dir": str(root), "full": bool(full),
            "dry_run": bool(dry_run), "transcripts_on_disk": sc["files"],
            "new_or_changed_files": len(sc["new"]),
            "watermark_before": _iso(mark_f),
        }

        if not sc["exists"]:
            rep.update({"skipped": "projects dir does not exist", "added": {}})
        elif not sc["new"]:
            # The cheap path: nothing has been written since the last run.
            rep.update({"skipped": "nothing new since the last run", "added": {}})
        elif dry_run:
            rep.update({"skipped": "dry run", "added": {},
                        "would_index_since": _day(sc["oldest_new"])})
        else:
            since = _day(sc["oldest_new"] - 86400) if (sc["oldest_new"] and not full) else None
            args = backfill.parse_args([
                "--usage", "--transcripts", "--quiet",
                *(["--since", since] if since else []),
            ])
            before = _counts(con)
            con.close()                      # backfill opens its own connection
            brep = backfill.run(args)
            con = db.connect()
            after = _counts(con)
            rep["since"] = since
            rep["added"] = {k: after[k] - before[k] for k in after}
            rep["backfill"] = {
                "files_parsed": brep.get("files_parsed"),
                "extra_files": brep.get("extra_files"),
                "sessions_written": brep.get("sessions_written"),
                "items": brep.get("items"),
                "usage_inserted": (brep.get("usage") or {}).get("inserted"),
                "chunks_added": (brep.get("transcripts") or {}).get("chunks_added"),
                "seconds": brep.get("elapsed_s"),
                "report": brep.get("log_path"),
            }

        if not dry_run:
            _meta_set(con, WATERMARK_KEY, repr(scan_started - SLACK_S))
            rep["finished_at"] = db.now_iso()
            rep["seconds"] = round(time.time() - t0, 2)
            _meta_set(con, LAST_RUN_KEY, json.dumps(rep))
            db.log_line("reindex", json.dumps({k: v for k, v in rep.items()
                                               if k not in ("projects_dir",)}))
        else:
            rep["finished_at"] = db.now_iso()
            rep["seconds"] = round(time.time() - t0, 2)
        return rep
    finally:
        try:
            con.close()
        except Exception:
            pass


def _counts(con) -> dict:
    def one(sql):
        try:
            return con.execute(sql).fetchone()[0] or 0
        except Exception:
            return 0
    return {
        "usage_events": one("SELECT count(*) FROM usage_events"),
        "transcript_chunks": one("SELECT count(*) FROM transcript_chunks"),
        "artifacts": one("SELECT count(*) FROM artifacts"),
        "sessions": one("SELECT count(*) FROM sessions"),
        "items": one("SELECT count(*) FROM items"),
        "indexed_days": one("SELECT count(DISTINCT substr(ts,1,10)) FROM usage_events"),
    }


def _iso(epoch):
    if not epoch:
        return None
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(epoch):
    if not epoch:
        return None
    return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="reindex.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true", help="ignore the watermark; re-scan everything")
    ap.add_argument("--dry-run", action="store_true", help="report what it would do, write nothing")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    a = ap.parse_args(argv)
    try:
        rep = run(full=a.full, dry_run=a.dry_run)
    except Exception as exc:                     # a failing job must still say why, loudly
        msg = f"reindex FAILED: {type(exc).__name__}: {exc}"
        db.log_line("reindex", msg)
        print(msg, file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(rep, indent=2, default=str))
        return 0
    print(f"reindex {rep['started_at'][:19]}Z · {rep['transcripts_on_disk']} transcripts on disk · "
          f"{rep['new_or_changed_files']} new/changed · {rep['seconds']}s")
    if rep.get("skipped"):
        print(f"  nothing to do: {rep['skipped']}")
        return 0
    add = rep.get("added") or {}
    grew = ", ".join(f"{v:+,} {k}" for k, v in add.items() if v)
    print("  added: " + (grew or "nothing new to add"))
    bf = rep.get("backfill") or {}
    print(f"  backfill: {bf.get('extra_files')} files mined since {rep.get('since') or 'the start'}"
          f" in {bf.get('seconds')}s · report {bf.get('report')}")
    return 0


def scan_other_agents() -> None:
    """Other AI tools' logs (Codex, ...) ride along with the daily reindex. Never fails the job."""
    try:
        import agents
        con = db.connect()
        db.migrate(con)
        for ad in agents.all_adapters().values():
            if ad.capabilities.get("capture") and ad.name != "claude" and ad.detected():
                print(f"  agents: {ad.name} {ad.scan(con)}")
    except Exception as e:
        db.log_line("reindex", f"agent scan failed: {e}")


if __name__ == "__main__":
    rc = main()
    scan_other_agents()
    sys.exit(rc)
