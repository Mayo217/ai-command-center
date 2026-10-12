#!/usr/bin/env python3
"""AI Command Center — local API + static UI server (stdlib only).

Serves the ledger over 127.0.0.1 so ui/index.html can read and curate it.

    python3 server.py [--port 8790] [--db /path/tracker.db]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402  (shared foundation: connect/migrate/derive_project/reindex_item/log_line)
from modules.common import ApiError  # noqa: E402  (module contract)

ROOT = Path(__file__).resolve().parent
UI_FILE = ROOT / "ui" / "index.html"

KINDS = ("build", "enhancement", "fix", "research", "doc", "config", "automation", "analysis", "other")
STATUSES = ("active", "wip", "superseded", "abandoned", "deleted")
CONFIDENCES = ("confirmed", "inferred")
ROLES = ("created", "modified", "entry", "doc", "output")
SORTS = {
    "date": "COALESCE(i.built_at, i.created_at) DESC, i.id DESC",
    "date_asc": "COALESCE(i.built_at, i.created_at) ASC, i.id ASC",
    "title": "i.title COLLATE NOCASE ASC",
    "title_desc": "i.title COLLATE NOCASE DESC",
    "project": "i.project COLLATE NOCASE ASC, COALESCE(i.built_at, i.created_at) DESC",
    "cost": "COALESCE(i.cost_usd,0) DESC, i.id DESC",
}
MAX_BODY = 1 << 20  # 1 MB


class HttpError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


# --------------------------------------------------------------------------- db
_local = threading.local()


def con() -> sqlite3.Connection:
    c = getattr(_local, "con", None)
    if c is None:
        c = db.connect()
        _local.con = c
    return c


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def jtags(raw) -> list[str]:
    try:
        v = json.loads(raw or "[]")
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return [t for t in re.split(r"[,\s]+", str(raw or "")) if t]


# --------------------------------------------------------------------- validation
def need_str(payload: dict, key: str, *, lo: int = 0, hi: int = 10 ** 9, required=True, label=None):
    label = label or key
    if key not in payload or payload[key] is None:
        if required:
            raise HttpError(400, f"{label} is required")
        return None
    v = payload[key]
    if not isinstance(v, str):
        raise HttpError(400, f"{label} must be a string")
    v = v.strip()
    if len(v) < lo:
        raise HttpError(400, f"{label} must be at least {lo} characters (got {len(v)})")
    if len(v) > hi:
        raise HttpError(400, f"{label} must be at most {hi} characters (got {len(v)})")
    return v


def check_enum(value, allowed, label):
    if value is None:
        return None
    if value not in allowed:
        raise HttpError(400, f"{label} must be one of: {', '.join(allowed)}")
    return value


def validate_title(v):
    return need_str({"title": v}, "title", lo=8, hi=140)


def validate_summary(v, title=None):
    s = need_str({"summary": v}, "summary", lo=int(db.config().get("gate", {}).get("min_summary_chars", 60)))
    if title and s.strip().lower() == str(title).strip().lower():
        raise HttpError(400, "summary must say more than the title (what it is and why it exists)")
    return s


def file_stat(path: str) -> tuple[int, int | None]:
    try:
        st = os.stat(path)
        return 1, st.st_size
    except OSError:
        return 0, None


# ------------------------------------------------------------------------ queries
_FTS_TOKEN = re.compile(r"[A-Za-z0-9_./-]+")


def fts_query(q: str) -> str | None:
    """Turn free text into a safe FTS5 MATCH expression (never let user syntax raise)."""
    toks = [t for t in _FTS_TOKEN.findall(q or "") if len(t) > 1 or t.isdigit()]
    if not toks:
        return None
    parts = ['"' + t.replace('"', "") + '"' for t in toks[:12]]
    parts[-1] = parts[-1] + "*"
    return " AND ".join(parts)


def item_list(params: dict) -> dict:
    c = con()
    limit = clamp_int(params.get("limit"), 100, 1, 1000)
    offset = clamp_int(params.get("offset"), 0, 0, 10 ** 7)
    where, args = ["1=1"], []
    status = params.get("status")
    if status:
        for s in str(status).split(","):
            check_enum(s, STATUSES, "status")
        marks = ",".join("?" for _ in str(status).split(","))
        where.append(f"i.status IN ({marks})")
        args += str(status).split(",")
    else:
        where.append("i.status != 'deleted'")
    for key, col in (("project", "i.project"), ("kind", "i.kind"), ("confidence", "i.confidence")):
        v = params.get(key)
        if v:
            vals = [x for x in str(v).split(",") if x]
            if key == "kind":
                [check_enum(x, KINDS, "kind") for x in vals]
            if key == "confidence":
                [check_enum(x, CONFIDENCES, "confidence") for x in vals]
            where.append(f"{col} IN ({','.join('?' for _ in vals)})")
            args += vals
    since = params.get("since")
    if since:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ].*)?", str(since)):
            raise HttpError(400, "since must be YYYY-MM-DD")
        where.append("COALESCE(i.built_at, i.created_at) >= ?")
        args.append(str(since))
    until = params.get("until")
    if until:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ].*)?", str(until)):
            raise HttpError(400, "until must be YYYY-MM-DD")
        where.append("COALESCE(i.built_at, i.created_at) <= ?")
        args.append(str(until) + "￿")
    session = params.get("session")
    if session:
        where.append("i.session_id = ?")
        args.append(session)

    q = (params.get("q") or "").strip()
    sort = params.get("sort") or ("relevance" if q else "date")
    if sort not in SORTS and sort != "relevance":
        raise HttpError(400, f"sort must be one of: {', '.join(list(SORTS) + ['relevance'])}")

    join, order, sel = "", SORTS.get(sort, SORTS["date"]), ""
    if q:
        expr = fts_query(q)
        if expr is None:
            return {"items": [], "total": 0, "limit": limit, "offset": offset, "q": q, "sort": sort}
        join = "JOIN items_fts f ON f.rowid = i.id"
        where.append("items_fts MATCH ?")
        args.append(expr)
        sel = ", bm25(items_fts, 8.0, 4.0, 2.0, 2.0, 1.0, 1.0) AS rank, snippet(items_fts, 1, '‹', '›', '…', 14) AS snippet"
        if sort == "relevance":
            order = "rank ASC"
    elif sort == "relevance":
        order = SORTS["date"]

    wsql = " AND ".join(where)
    try:
        total = c.execute(f"SELECT count(*) FROM items i {join} WHERE {wsql}", args).fetchone()[0]
        cur = c.execute(
            f"SELECT i.*{sel}, (SELECT count(*) FROM item_files WHERE item_id=i.id) AS files_count,"
            f" (SELECT count(*) FROM item_links WHERE item_id=i.id) AS links_count,"
            f" s.model AS session_model, s.cwd AS session_cwd"
            f" FROM items i {join} LEFT JOIN sessions s ON s.session_id=i.session_id"
            f" WHERE {wsql} ORDER BY {order} LIMIT ? OFFSET ?",
            args + [limit, offset],
        )
        out = rows(cur)
    except sqlite3.OperationalError as e:
        raise HttpError(400, f"bad query: {e}")
    for r in out:
        r["tags"] = jtags(r.get("tags"))
    return {"items": out, "total": total, "limit": limit, "offset": offset, "q": q, "sort": sort}


def item_detail(item_id: int) -> dict:
    c = con()
    it = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not it:
        raise HttpError(404, f"item {item_id} not found")
    it = dict(it)
    it["tags"] = jtags(it.get("tags"))
    try:
        it["extra"] = json.loads(it["extra"]) if it.get("extra") else None
    except Exception:
        pass
    files = rows(c.execute("SELECT * FROM item_files WHERE item_id=? ORDER BY CASE role WHEN 'entry' THEN 0 WHEN 'created' THEN 1 ELSE 2 END, path", (item_id,)))
    for f in files:
        ex, size = file_stat(f["path"])
        f["exists"] = ex
        f["size_bytes"] = size if size is not None else f.get("size_bytes")
        f["name"] = os.path.basename(f["path"])
    links = rows(c.execute("SELECT * FROM item_links WHERE item_id=? ORDER BY id", (item_id,)))
    sess = c.execute("SELECT * FROM sessions WHERE session_id=?", (it.get("session_id"),)).fetchone()
    if sess:
        sess = dict(sess)
        sess["state"] = session_state(sess)
        sess["sid8"] = (sess.get("session_id") or "")[:8]
        sess["pending_files"] = len(db.pending_files(c, sess["session_id"]))
    supersedes = None
    if it.get("supersedes_id"):
        r = c.execute("SELECT id,title FROM items WHERE id=?", (it["supersedes_id"],)).fetchone()
        supersedes = dict(r) if r else None
    superseded_by = rows(c.execute("SELECT id,title FROM items WHERE supersedes_id=? AND status!='deleted'", (item_id,)))
    return {"item": it, "files": files, "links": links, "session": sess,
            "supersedes": supersedes, "superseded_by": superseded_by}


def session_state(r: dict) -> str:
    if r.get("gate_bypassed"):
        return "bypassed"
    if r.get("waived_at"):
        return "waived"
    if r.get("logged"):
        return "logged"
    if r.get("substantive"):
        return "unlogged"
    return "quiet"


def session_list(params: dict) -> dict:
    c = con()
    limit = clamp_int(params.get("limit"), 200, 1, 2000)
    offset = clamp_int(params.get("offset"), 0, 0, 10 ** 7)
    where, args = ["1=1"], []
    if str(params.get("unlogged") or "") in ("1", "true", "yes"):
        where.append("((substantive=1 AND logged=0 AND waived_at IS NULL) OR gate_bypassed>0)")
    states = [s for s in str(params.get("state") or "").split(",") if s]
    if states:
        frags = []
        for s in states:
            if s == "unlogged":
                frags.append("(substantive=1 AND logged=0 AND waived_at IS NULL AND gate_bypassed=0)")
            elif s == "bypassed":
                frags.append("(gate_bypassed>0)")
            elif s == "waived":
                frags.append("(waived_at IS NOT NULL)")
            elif s == "logged":
                frags.append("(logged=1)")
            elif s == "quiet":
                frags.append("(substantive=0 AND logged=0 AND waived_at IS NULL AND gate_bypassed=0)")
            else:
                raise HttpError(400, "state must be unlogged, bypassed, waived, logged or quiet")
        where.append("(" + " OR ".join(frags) + ")")
    if params.get("project"):
        where.append("project = ?")
        args.append(params["project"])
    if params.get("since"):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ].*)?", str(params["since"])):
            raise HttpError(400, "since must be YYYY-MM-DD")
        where.append("COALESCE(started_at, created_at) >= ?")
        args.append(str(params["since"]))
    if str(params.get("substantive") or "") in ("1", "true"):
        where.append("substantive=1")
    wsql = " AND ".join(where)
    total = c.execute(f"SELECT count(*) FROM sessions WHERE {wsql}", args).fetchone()[0]
    out = rows(c.execute(
        f"SELECT *, (SELECT count(*) FROM session_files sf WHERE sf.session_id=sessions.session_id AND sf.ignored=0) AS files_count,"
        f" (SELECT count(*) FROM session_files sf WHERE sf.session_id=sessions.session_id AND sf.ignored=0 AND sf.logged_item_id IS NULL) AS pending_files,"
        f" (SELECT count(*) FROM items it WHERE it.session_id=sessions.session_id AND it.status!='deleted') AS items_count"
        f" FROM sessions WHERE {wsql} ORDER BY COALESCE(started_at, created_at) DESC LIMIT ? OFFSET ?",
        args + [limit, offset]))
    for r in out:
        r["state"] = session_state(r)
        r["sid8"] = (r.get("session_id") or "")[:8]
    return {"sessions": out, "total": total, "limit": limit, "offset": offset}


def session_detail(sid: str) -> dict:
    c = con()
    s = c.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    if not s:
        raise HttpError(404, f"session {sid} not found")
    s = dict(s)
    s["state"] = session_state(s)
    s["sid8"] = sid[:8]
    files = rows(c.execute("SELECT * FROM session_files WHERE session_id=? ORDER BY ignored, first_ts", (sid,)))
    for f in files:
        ex, size = file_stat(f["path"])
        f["exists"] = ex
        f["size_bytes"] = size
        f["name"] = os.path.basename(f["path"])
    events = rows(c.execute("SELECT * FROM session_events WHERE session_id=? ORDER BY id", (sid,)))
    for e in events:
        try:
            e["payload"] = json.loads(e["payload"]) if e.get("payload") else None
        except Exception:
            pass
    items = rows(c.execute("SELECT id,title,kind,status,confidence,built_at,project FROM items WHERE session_id=? AND status!='deleted' ORDER BY id", (sid,)))
    commands = db.commands_for_session(c, sid, limit=200)
    return {"session": s, "files": files, "events": events, "items": items, "commands": commands}


def command_list(params: dict) -> dict:
    """Every Bash command the hooks (and backfill) saw, newest first — the "how did I run that
    last time" index. Substring match, because that is what a person means by searching shell text."""
    c = con()
    limit = clamp_int(params.get("limit"), 50, 1, 500)
    offset = clamp_int(params.get("offset"), 0, 0, 100000)
    rowsout = db.search_commands(
        c, (params.get("q") or "").strip(), session_id=(params.get("session") or None),
        project=(params.get("project") or None),
        wrote_only=(params.get("wrote") in ("1", "true", "yes")),
        limit=limit, offset=offset)
    try:
        total = c.execute("SELECT count(*) FROM session_commands").fetchone()[0]
    except Exception:
        total = 0
    return {"commands": rowsout, "count": len(rowsout), "total_stored": total,
            "limit": limit, "offset": offset}


def stats() -> dict:
    c = con()
    t = dict(c.execute(
        "SELECT count(*) AS items,"
        " sum(confidence='confirmed') AS confirmed, sum(confidence='inferred') AS inferred,"
        " sum(status='active') AS active, sum(status='wip') AS wip,"
        " sum(COALESCE(cost_usd,0)) AS cost_usd,"
        " min(COALESCE(built_at,created_at)) AS first_date, max(COALESCE(built_at,created_at)) AS last_date"
        " FROM items WHERE status!='deleted'").fetchone())
    s = dict(c.execute(
        "SELECT count(*) AS sessions, sum(substantive) AS substantive, sum(logged) AS logged,"
        " sum(gate_bypassed>0) AS bypassed, sum(waived_at IS NOT NULL) AS waived,"
        " sum(COALESCE(cost_usd,0)) AS cost_usd,"
        " min(COALESCE(started_at,created_at)) AS first_date, max(COALESCE(started_at,created_at)) AS last_date"
        " FROM sessions").fetchone())
    unlogged = c.execute(
        "SELECT count(*) FROM sessions WHERE (substantive=1 AND logged=0 AND waived_at IS NULL) OR gate_bypassed>0").fetchone()[0]
    projects = rows(c.execute(
        "SELECT project,"
        " (SELECT count(*) FROM items i WHERE i.project=x.project AND i.status!='deleted') AS items,"
        " (SELECT count(*) FROM sessions se WHERE se.project=x.project) AS sessions,"
        " (SELECT sum(COALESCE(cost_usd,0)) FROM sessions se WHERE se.project=x.project) AS cost_usd,"
        " (SELECT min(COALESCE(built_at,created_at)) FROM items i WHERE i.project=x.project AND i.status!='deleted') AS first_date,"
        " (SELECT max(COALESCE(built_at,created_at)) FROM items i WHERE i.project=x.project AND i.status!='deleted') AS last_date"
        " FROM (SELECT DISTINCT project FROM items WHERE status!='deleted' AND project IS NOT NULL"
        "       UNION SELECT DISTINCT project FROM sessions WHERE project IS NOT NULL) x"
        " ORDER BY items DESC, project"))
    kinds = rows(c.execute("SELECT kind, count(*) AS count FROM items WHERE status!='deleted' GROUP BY kind ORDER BY count DESC"))
    statuses = rows(c.execute("SELECT status, count(*) AS count FROM items WHERE status!='deleted' GROUP BY status ORDER BY count DESC"))
    per_day = rows(c.execute(
        "SELECT substr(COALESCE(built_at,created_at),1,10) AS day, count(*) AS items,"
        " sum(COALESCE(cost_usd,0)) AS cost_usd FROM items WHERE status!='deleted'"
        " GROUP BY day ORDER BY day"))
    files = c.execute("SELECT count(*) FROM item_files").fetchone()[0]
    cost = (t.get("cost_usd") or 0) or (s.get("cost_usd") or 0)
    return {
        "totals": {
            "items": t.get("items") or 0,
            "confirmed": t.get("confirmed") or 0,
            "inferred": t.get("inferred") or 0,
            "sessions": s.get("sessions") or 0,
            "substantive_sessions": s.get("substantive") or 0,
            "logged_sessions": s.get("logged") or 0,
            "unlogged": unlogged,
            "bypassed": s.get("bypassed") or 0,
            "waived": s.get("waived") or 0,
            "files": files,
            "cost_usd": round(float(s.get("cost_usd") or 0), 4),
            "item_cost_usd": round(float(cost or 0), 4),
        },
        "projects": projects,
        "kinds": kinds,
        "statuses": statuses,
        "per_day": per_day,
        "first_date": t.get("first_date") or s.get("first_date"),
        "last_date": t.get("last_date") or s.get("last_date"),
        "session_first_date": s.get("first_date"),
        "session_last_date": s.get("last_date"),
        "generated_at": db.now_iso(),
        "db": str(db.DB_PATH),
    }


def projects() -> dict:
    return {"projects": stats()["projects"]}


# ------------------------------------------------------------------------ writes
def attach_file(c, item_id: int, path: str, role: str = "modified", cwd: str | None = None):
    role = check_enum(role or "modified", ROLES, "role") or "modified"
    p = db.norm_path(str(path), cwd)
    ex, size = file_stat(p)
    c.execute("INSERT OR IGNORE INTO item_files(item_id,path,role,exists_at_log,size_bytes) VALUES(?,?,?,?,?)",
              (item_id, p, role, ex, size))
    c.execute("UPDATE item_files SET role=?, exists_at_log=?, size_bytes=? WHERE item_id=? AND path=?",
              (role, ex, size, item_id, p))
    return p


def create_item(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise HttpError(400, "body must be a JSON object")
    c = con()
    title = validate_title(payload.get("title"))
    summary = validate_summary(payload.get("summary"), title)
    usage = need_str(payload, "usage", required=False, hi=8000) if payload.get("usage") else None
    kind = check_enum(payload.get("kind") or "build", KINDS, "kind")
    status = check_enum(payload.get("status") or "active", STATUSES, "status")
    confidence = check_enum(payload.get("confidence") or "confirmed", CONFIDENCES, "confidence")
    sid = payload.get("session_id") or None
    if sid and not c.execute("SELECT 1 FROM sessions WHERE session_id=?", (sid,)).fetchone():
        raise HttpError(404, f"session {sid} not found")
    if not sid:
        sid = "manual-ui"
        db.upsert_session(c, sid, source="manual", title="Manual entries from the tracker app",
                          project="ai-command-center", cwd=str(ROOT), started_at=db.now_iso(), substantive=1, logged=1)
    sess = c.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    project = payload.get("project") or (sess["project"] if sess else None) or db.derive_project(sess["cwd"] if sess else None)
    tags = payload.get("tags") or []
    if isinstance(tags, str):
        tags = [t for t in re.split(r"[,\s]+", tags) if t]
    if not isinstance(tags, list):
        raise HttpError(400, "tags must be a list of strings")
    built_at = payload.get("built_at") or db.now_iso()
    supersedes = payload.get("supersedes_id")
    if supersedes is not None:
        try:
            supersedes = int(supersedes)
        except (TypeError, ValueError):
            raise HttpError(400, "supersedes_id must be an integer")
        if not c.execute("SELECT 1 FROM items WHERE id=?", (supersedes,)).fetchone():
            raise HttpError(404, f"item {supersedes} (supersedes_id) not found")
    cur = c.execute(
        "INSERT INTO items(session_id,built_at,kind,title,summary,usage,project,status,confidence,source,tags,supersedes_id,model,cost_usd)"
        " VALUES(?,?,?,?,?,?,?,?,?,'ui',?,?,?,?)",
        (sid, built_at, kind, title, summary, usage, project, status, confidence,
         json.dumps([str(t) for t in tags]), supersedes, (sess["model"] if sess else None), payload.get("cost_usd")))
    item_id = cur.lastrowid
    cwd = sess["cwd"] if sess else None
    for f in payload.get("files") or []:
        if isinstance(f, str):
            attach_file(c, item_id, f, "modified", cwd)
        elif isinstance(f, dict) and f.get("path"):
            attach_file(c, item_id, f["path"], f.get("role") or "modified", cwd)
    for l in payload.get("links") or []:
        if isinstance(l, str):
            c.execute("INSERT OR IGNORE INTO item_links(item_id,url,label) VALUES(?,?,NULL)", (item_id, l))
        elif isinstance(l, dict) and l.get("url"):
            c.execute("INSERT OR IGNORE INTO item_links(item_id,url,label) VALUES(?,?,?)", (item_id, l["url"], l.get("label")))
    if supersedes:
        c.execute("UPDATE items SET status='superseded' WHERE id=? AND status='active'", (supersedes,))
        db.reindex_item(c, supersedes)
    # claim the session's pending file touches that match
    for p in [r["path"] for r in c.execute("SELECT path FROM item_files WHERE item_id=?", (item_id,))]:
        c.execute("UPDATE session_files SET logged_item_id=? WHERE session_id=? AND path=? AND logged_item_id IS NULL",
                  (item_id, sid, p))
        db.release_unclaimed(c, [p], claiming_item_id=item_id)
    if confidence == "confirmed":
        c.execute("UPDATE sessions SET logged=1 WHERE session_id=?", (sid,))
    db.reindex_item(c, item_id)
    db.add_event(c, sid, "log", {"item_id": item_id, "via": "ui"})
    db.log_line("server", f"created item #{item_id} {title!r} via UI")
    return item_detail(item_id)


def patch_item(item_id: int, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise HttpError(400, "body must be a JSON object")
    c = con()
    it = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not it:
        raise HttpError(404, f"item {item_id} not found")
    sets, args = [], []
    if "title" in payload:
        sets.append("title=?"); args.append(validate_title(payload["title"]))
    if "summary" in payload:
        sets.append("summary=?"); args.append(validate_summary(payload["summary"], payload.get("title", it["title"])))
    if "usage" in payload:
        v = payload["usage"]
        if v is not None and not isinstance(v, str):
            raise HttpError(400, "usage must be a string")
        sets.append("usage=?"); args.append((v or "").strip() or None)
    for key, allowed in (("kind", KINDS), ("status", STATUSES), ("confidence", CONFIDENCES)):
        if key in payload:
            sets.append(f"{key}=?"); args.append(check_enum(payload[key], allowed, key))
    if "project" in payload:
        v = payload["project"]
        if v is not None and not isinstance(v, str):
            raise HttpError(400, "project must be a string")
        sets.append("project=?"); args.append((v or "").strip() or None)
    if "tags" in payload:
        tags = payload["tags"]
        if isinstance(tags, str):
            tags = [t for t in re.split(r"[,\s]+", tags) if t]
        if not isinstance(tags, list):
            raise HttpError(400, "tags must be a list of strings")
        sets.append("tags=?"); args.append(json.dumps([str(t).strip() for t in tags if str(t).strip()]))
    if "built_at" in payload and payload["built_at"]:
        sets.append("built_at=?"); args.append(str(payload["built_at"]))
    if "verified" in payload:
        sets.append("verified_at=?"); args.append(db.now_iso() if payload["verified"] else None)
    if sets:
        sets.append("updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')")
        c.execute(f"UPDATE items SET {', '.join(sets)} WHERE id=?", args + [item_id])

    sess = c.execute("SELECT cwd FROM sessions WHERE session_id=?", (it["session_id"],)).fetchone()
    cwd = sess["cwd"] if sess else None
    add_file = payload.get("add_file")
    for f in ([add_file] if isinstance(add_file, (str, dict)) else (add_file or [])):
        if isinstance(f, str):
            attach_file(c, item_id, f, "modified", cwd)
        elif isinstance(f, dict) and f.get("path"):
            attach_file(c, item_id, f["path"], f.get("role") or "modified", cwd)
        else:
            raise HttpError(400, "add_file must be a path or {path, role}")
    rm = payload.get("remove_file")
    for f in ([rm] if isinstance(rm, (str, int, dict)) else (rm or [])):
        if isinstance(f, dict):
            f = f.get("path") or f.get("id")
        if isinstance(f, int):
            c.execute("DELETE FROM item_files WHERE item_id=? AND id=?", (item_id, f))
        elif isinstance(f, str):
            c.execute("DELETE FROM item_files WHERE item_id=? AND path IN (?,?)", (item_id, f, db.norm_path(f, cwd)))
    add_link = payload.get("add_link")
    for l in ([add_link] if isinstance(add_link, (str, dict)) else (add_link or [])):
        url, label = (l, None) if isinstance(l, str) else (l.get("url"), l.get("label")) if isinstance(l, dict) else (None, None)
        if not url:
            raise HttpError(400, "add_link must be a url or {url, label}")
        if not re.match(r"^(https?|file)://", url):
            raise HttpError(400, "link url must start with http://, https:// or file://")
        c.execute("INSERT OR IGNORE INTO item_links(item_id,url,label) VALUES(?,?,?)", (item_id, url, label))
    rml = payload.get("remove_link")
    for l in ([rml] if isinstance(rml, (str, int)) else (rml or [])):
        if isinstance(l, int):
            c.execute("DELETE FROM item_links WHERE item_id=? AND id=?", (item_id, l))
        else:
            c.execute("DELETE FROM item_links WHERE item_id=? AND url=?", (item_id, l))
    if payload.get("confidence") == "confirmed" or payload.get("verified"):
        c.execute("UPDATE sessions SET logged=1 WHERE session_id=?", (it["session_id"],))
    db.reindex_item(c, item_id)
    db.log_line("server", f"patched item #{item_id}: {sorted(payload.keys())}")
    return item_detail(item_id)


def delete_item(item_id: int) -> dict:
    c = con()
    it = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not it:
        raise HttpError(404, f"item {item_id} not found")
    c.execute("UPDATE items SET status='deleted', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (item_id,))
    c.execute("UPDATE session_files SET logged_item_id=NULL WHERE logged_item_id=?", (item_id,))
    db.reindex_item(c, item_id)
    if it["session_id"] and not c.execute(
            "SELECT 1 FROM items WHERE session_id=? AND status!='deleted' AND confidence='confirmed'", (it["session_id"],)).fetchone():
        c.execute("UPDATE sessions SET logged=0 WHERE session_id=?", (it["session_id"],))
    db.log_line("server", f"soft-deleted item #{item_id}")
    return {"ok": True, "id": item_id, "status": "deleted"}


def waive_session(sid: str, payload: dict) -> dict:
    c = con()
    s = c.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    if not s:
        raise HttpError(404, f"session {sid} not found")
    min_chars = int(db.config().get("gate", {}).get("min_waive_reason_chars", 20))
    reason = need_str(payload if isinstance(payload, dict) else {}, "reason", lo=min_chars, hi=2000, label="reason")
    pending = db.pending_files(c, sid)
    if pending and not payload.get("force"):
        raise HttpError(409, f"this session has {len(pending)} unlogged file changes — log an item instead, or waive with force",
                        pending=[r["path"] for r in pending][:12], pending_count=len(pending))
    ts = db.now_iso()
    c.execute("UPDATE sessions SET waived_at=?, waived_reason=?, updated_at=? WHERE session_id=?", (ts, reason, ts, sid))
    db.add_event(c, sid, "waive", {"reason": reason, "via": "ui", "forced": bool(payload.get("force"))})
    db.log_line("server", f"waived session {sid[:8]}: {reason[:80]!r}")
    return session_detail(sid)


def clamp_int(v, default, lo, hi):
    if v is None or v == "":
        return default
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise HttpError(400, f"expected an integer, got {v!r}")
    return max(lo, min(hi, n))


# ------------------------------------------------------------------- module mount
# Every modules/<name>.py may expose ROUTES = [(method, regex_path, handler)];
# handler(query: dict, body: dict|None, match: re.Match) -> (status, json-serialisable).
# Routes are mounted under /api/<name>/. A module that fails to import is skipped and logged,
# so one broken module can never take the command centre down.
MODULES: dict[str, object] = {}
MOUNTED: list[tuple[str, str, "re.Pattern[str]", object, str]] = []  # (module, method, regex, handler, raw)
MODULE_ERRORS: dict[str, str] = {}
RESERVED_MODULE_NAMES = {"items", "sessions", "stats", "projects"}


SOURCE_MTIMES: dict[str, float] = {}
STARTED_AT = db.now_iso()


def stale_sources() -> list[str]:
    """Files whose on-disk mtime is newer than what this process imported — i.e. edits that are
    not live yet. Empty means the running server is the code on disk."""
    out = []
    for path, mt in SOURCE_MTIMES.items():
        try:
            if os.path.getmtime(path) > mt + 0.5:
                out.append(path)
        except OSError:
            out.append(path)
    return sorted(out)


def load_modules(verbose: bool = False) -> None:
    """Import modules/*.py and mount their ROUTES. Idempotent; safe to call at startup."""
    import importlib
    MODULES.clear(); MOUNTED.clear(); MODULE_ERRORS.clear(); SOURCE_MTIMES.clear()
    # the server holds imported modules, so an edit on disk is invisible until a
    # restart. Record what was on disk at import time; /health publishes it and `tracker doctor`
    # compares it with the files, so "I edited it and nothing changed" is a check, not a memory.
    for f in [ROOT / "server.py"] + sorted((ROOT / "modules").glob("*.py")):
        try:
            SOURCE_MTIMES[str(f)] = f.stat().st_mtime
        except OSError:
            pass
    mod_dir = ROOT / "modules"
    if not mod_dir.is_dir():
        return
    for f in sorted(mod_dir.glob("*.py")):
        name = f.stem
        if name.startswith("_") or name == "common":
            continue
        try:
            mod = importlib.import_module(f"modules.{name}")
            importlib.reload(mod)
        except Exception as e:
            MODULE_ERRORS[name] = f"{e.__class__.__name__}: {e}"
            db.log_line("server", f"module {name} failed to import: {e}\n{traceback.format_exc()}")
            continue
        mount = getattr(mod, "MODULE", None) or name
        if mount in RESERVED_MODULE_NAMES:
            MODULE_ERRORS[name] = f"mount name {mount!r} collides with a core route"
            continue
        MODULES[mount] = mod
        for route in getattr(mod, "ROUTES", []) or []:
            try:
                method, raw, handler = route[0], route[1], route[2]
            except Exception:
                MODULE_ERRORS[name] = "ROUTES entries must be (method, regex_path, handler)"
                continue
            pat = str(raw).strip()
            if pat.startswith("^"):
                pat = pat[1:]
            if pat.endswith("$") and not pat.endswith("\\$"):
                pat = pat[:-1]
            if not pat.startswith("/api/"):
                pat = f"/api/{mount}/" + pat.lstrip("/")
            pat = pat.rstrip("/") or pat
            try:
                rx = re.compile(pat)
            except re.error as e:
                MODULE_ERRORS[name] = f"bad route regex {raw!r}: {e}"
                continue
            MOUNTED.append((mount, str(method).upper(), rx, handler, pat))
        if verbose:
            print(f"  module {mount}: {sum(1 for m in MOUNTED if m[0] == mount)} routes", flush=True)
    db.log_line("server", f"modules loaded: {sorted(MODULES)} routes={len(MOUNTED)} errors={MODULE_ERRORS or 'none'}")


def audit_action(module: str, path: str, body, status: int) -> None:
    """Every mutating module call leaves a trail under session_id='cc'."""
    try:
        payload = {"module": module, "path": path, "status": status}
        if isinstance(body, dict):
            payload["body"] = {k: v for k, v in list(body.items())[:20] if k != "content"}
        db.add_event(con(), "cc", "action", payload)
    except Exception as e:
        db.log_line("server", f"audit_action failed: {e}")


def dispatch_module(method: str, path: str, params: dict, body_reader) -> tuple[int, object] | None:
    """Return (status, payload) if a module claims this path, else None. 405 if only the method mismatches."""
    method_mismatch = False
    for mount, mmethod, rx, handler, _raw in MOUNTED:
        m = rx.fullmatch(path)
        if not m:
            continue
        if mmethod != method and not (method == "GET" and mmethod == "HEAD"):
            method_mismatch = True
            continue
        body = body_reader() if method in ("POST", "PUT", "PATCH", "DELETE") else None
        try:
            result = handler(params, body, m)
        except ApiError as e:
            raise HttpError(getattr(e, "status", 400), getattr(e, "message", str(e)))
        except HttpError:
            raise
        except Exception as e:
            db.log_line("server", f"module {mount} {path} crashed: {e}\n{traceback.format_exc()}")
            raise HttpError(500, f"module {mount} failed: {e.__class__.__name__}: {e}")
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
            status, payload = result
        else:
            status, payload = 200, result
        if method != "GET":
            audit_action(mount, path, body, status)
        return status, payload
    if method_mismatch:
        raise HttpError(405, "method not allowed")
    return None


# ------------------------------------------------------------------------ handler
class Handler(BaseHTTPRequestHandler):
    server_version = "AICommandCenter/1.0"
    protocol_version = "HTTP/1.1"

    def finish(self):
        """Close this thread's sqlite handle when the TCP connection ends.

        ThreadingHTTPServer starts a thread per connection and `con()` caches a
        connection in thread-local storage, so without this every connection
        leaked a handle (sqlite holds the db, its -wal and its -shm). Under the
        LaunchAgent's 256-descriptor limit that exhausted the table and sqlite
        reported it as the very misleading "unable to open database file".
        """
        try:
            super().finish()
        finally:
            _local.con = None
            try:
                db.close_thread_connections()   # covers module handlers' connections too
            except Exception:
                pass

    def log_message(self, fmt, *args):  # quiet stderr; we log ourselves
        pass

    # ---- plumbing
    def _send(self, status: int, body: bytes, ctype: str, extra_headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, obj, status=200):
        self._send(status, json.dumps(obj, default=str).encode(), "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def error_json(self, status, message, **extra):
        payload = {"error": message, "status": status}
        payload.update(extra)
        self.json(payload, status)

    def body_json(self) -> dict:
        n = self.headers.get("Content-Length")
        try:
            n = int(n or 0)
        except ValueError:
            raise HttpError(400, "bad Content-Length")
        if n > MAX_BODY:
            raise HttpError(413, "request body too large")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            v = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise HttpError(400, f"invalid JSON body: {e}")
        if not isinstance(v, dict):
            raise HttpError(400, "body must be a JSON object")
        return v

    # ---- verbs
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain", {"Allow": "GET, HEAD, POST, DELETE, OPTIONS"})

    def _guard(self, method):
        """Only this computer's own browser tab may talk to the server.
        Host check: defeats DNS rebinding (a remote page re-pointed at 127.0.0.1).
        Writes must be application/json from our own origin: a cross-site form or no-cors fetch
        cannot set that content type without a CORS preflight, which this server never grants."""
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        host = (self.headers.get("Host") or "").lower()
        if host not in allowed:
            raise HttpError(403, "forbidden host")
        if method == "GET":
            return
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in {f"http://{h}" for h in allowed}:
            raise HttpError(403, "cross-origin write refused")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise HttpError(415, "writes must be Content-Type: application/json")

    def _dispatch(self, method):
        t0 = time.time()
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path).rstrip("/") or "/"
        params = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
        try:
            self._guard(method)
            self.route(method, path, params)
        except HttpError as e:
            self.error_json(e.status, e.message, **e.extra)
        except BrokenPipeError:
            return
        except Exception as e:
            db.log_line("server", f"ERROR {method} {self.path}: {e}\n{traceback.format_exc()}")
            try:
                self.error_json(500, f"internal error: {e.__class__.__name__}: {e}")
            except Exception:
                pass
        finally:
            db.log_line("server", f"{method} {self.path} {int((time.time()-t0)*1000)}ms")

    def route(self, method, path, params):
        # ---------- static
        if path == "/health":
            return self.json({"ok": True, "db": str(db.DB_PATH), "db_exists": db.DB_PATH.exists(),
                              "port": self.server.server_address[1], "time": db.now_iso(),
                              "modules": sorted(MODULES), "module_errors": MODULE_ERRORS,
                              "pid": os.getpid(), "started_at": STARTED_AT,
                              "source_mtimes": SOURCE_MTIMES,
                              "stale_sources": stale_sources()})
        if path in ("/", "/index.html", "/ui/index.html"):
            if method != "GET":
                raise HttpError(405, "method not allowed")
            if not UI_FILE.exists():
                raise HttpError(404, "ui/index.html is missing — the app has not been built yet")
            return self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8",
                              {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache", "Expires": "0"})
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if path.startswith("/mascot/"):
            if method != "GET":
                raise HttpError(405, "method not allowed")
            rel = path[len("/mascot/"):]
            if not re.fullmatch(r"[A-Za-z0-9._-]+", rel or ""):
                raise HttpError(404, "no such mascot file")
            f = (ROOT / "mascot" / rel).resolve()
            if not str(f).startswith(str((ROOT / "mascot").resolve())) or not f.is_file():
                if rel.endswith(".js"):
                    # The shell includes /mascot/mascot.js unconditionally. Until the creature
                    # lands, answer with an executable no-op so the browser has nothing to
                    # complain about and the page is simply mascot-less.
                    stub = (b"/* mascot/" + rel.encode() + b" is not here yet;"
                            b" the command centre runs without it. */\n"
                            b"console.info('[command centre] mascot not installed');\n")
                    return self._send(200, stub, "text/javascript; charset=utf-8", {"Cache-Control": "no-store"})
                raise HttpError(404, f"no mascot file called {rel}")
            ctype = {"js": "text/javascript; charset=utf-8", "mjs": "text/javascript; charset=utf-8",
                     "json": "application/json; charset=utf-8", "css": "text/css; charset=utf-8",
                     "png": "image/png", "gif": "image/gif", "svg": "image/svg+xml",
                     "webp": "image/webp", "map": "application/json; charset=utf-8",
                     "txt": "text/plain; charset=utf-8", "md": "text/plain; charset=utf-8"
                     }.get(f.suffix.lstrip(".").lower(), "application/octet-stream")
            return self._send(200, f.read_bytes(), ctype, {"Cache-Control": "no-cache"})

        # ---------- api
        if not path.startswith("/api/"):
            raise HttpError(404, f"no route for {path}")
        m = re.fullmatch(r"/api/items/(\d+)", path)
        if m:
            item_id = int(m.group(1))
            if method == "GET":
                return self.json(item_detail(item_id))
            if method == "POST":
                return self.json(patch_item(item_id, self.body_json()))
            if method == "DELETE":
                return self.json(delete_item(item_id))
            raise HttpError(405, "method not allowed")
        if re.fullmatch(r"/api/items/\S+", path):
            raise HttpError(400, "item id must be an integer")
        if path == "/api/items":
            if method == "GET":
                return self.json(item_list(params))
            if method == "POST":
                return self.json(create_item(self.body_json()), 201)
            raise HttpError(405, "method not allowed")
        m = re.fullmatch(r"/api/sessions/([^/]+)/waive", path)
        if m:
            if method != "POST":
                raise HttpError(405, "method not allowed")
            return self.json(waive_session(m.group(1), self.body_json()))
        m = re.fullmatch(r"/api/sessions/([^/]+)", path)
        if m:
            if method != "GET":
                raise HttpError(405, "method not allowed")
            return self.json(session_detail(m.group(1)))
        if path == "/api/sessions":
            if method != "GET":
                raise HttpError(405, "method not allowed")
            return self.json(session_list(params))
        if path == "/api/stats":
            return self.json(stats())
        if path == "/api/projects":
            return self.json(projects())
        if path == "/api/commands":
            if method != "GET":
                raise HttpError(405, "method not allowed")
            return self.json(command_list(params))
        if path == "/api/modules":
            return self.json({
                "modules": [{"name": n, "routes": sorted({r[4] for r in MOUNTED if r[0] == n})} for n in sorted(MODULES)],
                "errors": MODULE_ERRORS,
            })
        hit = dispatch_module(method, path, params, self.body_json)
        if hit is not None:
            return self.json(hit[1], hit[0])
        raise HttpError(404, f"no route for {path}")


# --------------------------------------------------------------------------- main
def serve(port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def main(argv=None):
    ap = argparse.ArgumentParser(description="AI Command Center local API + UI server (stdlib only).")
    ap.add_argument("--port", type=int, default=None, help="TCP port (default: config.json port, 8790)")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1; do not expose)")
    ap.add_argument("--db", default=None, help="path to tracker.db (default: config/env CLAUDE_TRACKER_DB)")
    args = ap.parse_args(argv)

    if args.db:
        db.DB_PATH = Path(args.db).expanduser().resolve()
    port = args.port or int(db.config().get("port", 8790))

    db.POOL = True   # one connection per request thread, closed in Handler.finish()
    load_modules(verbose=True)
    try:
        con()  # fail fast on a broken DB, and migrate before the first request
    except sqlite3.Error as e:
        print(f"tracker: cannot open {db.DB_PATH}: {e}", file=sys.stderr)
        return 1
    try:
        httpd = serve(port, args.host)
    except OSError as e:
        print(f"tracker: cannot bind {args.host}:{port}: {e}", file=sys.stderr)
        return 1

    stopping = threading.Event()

    def shutdown(signum, _frame):
        if stopping.is_set():
            return
        stopping.set()
        db.log_line("server", f"signal {signum} — shutting down")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    db.log_line("server", f"listening on http://{args.host}:{port} db={db.DB_PATH}")
    print(f"AI Command Center on http://{args.host}:{port}  (db: {db.DB_PATH})", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        httpd.server_close()
        db.log_line("server", "stopped")
        print("stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
