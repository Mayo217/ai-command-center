"""Scratch and pins — module 10 of the Command Center.

Pin anything in the centre (an item, a session, a job, a project, a memory file,
a URL) to the Today board, and capture quick notes without leaving the page.
Everything lives in the tracker DB next to the ledger; nothing leaves the Mac.

Routes
  GET    /api/notes/board?limit=       pins + notes + what happened today
  GET    /api/notes/pins               pins, newest first
  POST   /api/notes/pins               {kind, ref, label?, note?}  -> create (idempotent per kind+ref)
  DELETE /api/notes/pins               {id} or {kind, ref}         -> remove
  GET    /api/notes/notes?limit=&q=    notes, newest first
  POST   /api/notes/notes              {text}                      -> create
  DELETE /api/notes/notes              {id}                        -> remove
"""
from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import db  # noqa: E402
from modules.common import ApiError  # noqa: E402

MODULE = "notes"

PIN_KINDS = ("item", "session", "job", "project", "memory", "skill", "url", "view", "file")
SCHEMA = """
CREATE TABLE IF NOT EXISTS pins (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT NOT NULL,
  ref        TEXT NOT NULL,
  label      TEXT,
  note       TEXT,
  sort       INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE(kind, ref)
);
CREATE TABLE IF NOT EXISTS notes (
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  ts   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  text TEXT NOT NULL,
  ref  TEXT
);
CREATE INDEX IF NOT EXISTS idx_notes_ts ON notes(ts);
"""

_ready = False


def con():
    """Connection for this call, with the notes tables created on first use."""
    global _ready
    c = db.connect()
    if not _ready:
        c.executescript(SCHEMA)
        _ready = True
    return c


def _int(v, label):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ApiError(400, f"{label} must be an integer")


def _body(body) -> dict:
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise ApiError(400, "body must be a JSON object")
    return body


def _clamp(v, default, lo, hi):
    if v in (None, ""):
        return default
    return max(lo, min(hi, _int(v, "limit")))


# --------------------------------------------------------------------- resolve
def _resolve(c, kind: str, ref: str) -> dict:
    """Give a pin enough detail to render without a second round trip."""
    out = {"exists": True, "detail": None, "href": None}
    try:
        if kind == "item":
            r = c.execute("SELECT id,title,kind,project,status,built_at,created_at FROM items WHERE id=?", (_int(ref, "ref"),)).fetchone()
            if not r:
                return {"exists": False, "detail": None, "href": None}
            out["detail"] = f"{r['kind']} in {r['project'] or 'no project'}"
            out["href"] = f"#/ledger/{r['id']}"
            out["title"] = r["title"]
            out["ts"] = r["built_at"] or r["created_at"]
        elif kind == "session":
            r = c.execute("SELECT session_id,title,project,started_at,cost_usd FROM sessions WHERE session_id=?", (ref,)).fetchone()
            if not r:
                return {"exists": False, "detail": None, "href": None}
            out["detail"] = f"session {ref[:8]} in {r['project'] or 'no project'}"
            out["href"] = f"#/audit/{ref}"
            out["title"] = r["title"] or f"session {ref[:8]}"
            out["ts"] = r["started_at"]
        elif kind == "project":
            n = c.execute("SELECT count(*) FROM items WHERE project=? AND status!='deleted'", (ref,)).fetchone()[0]
            out["detail"] = f"{n} ledger {'entry' if n == 1 else 'entries'}"
            out["href"] = "#/launch"
            out["title"] = ref
        elif kind in ("memory", "file"):
            p = Path(ref).expanduser()
            out["exists"] = p.exists()
            out["detail"] = str(p.parent).replace(str(Path.home()), "~")
            out["href"] = "#/memory" if kind == "memory" else None
            out["title"] = p.name
        elif kind == "job":
            out["detail"] = "launchd job"
            out["href"] = "#/jobs"
            out["title"] = ref
        elif kind == "skill":
            out["detail"] = "skill"
            out["href"] = "#/config"
            out["title"] = ref
        elif kind == "url":
            out["detail"] = re.sub(r"^https?://", "", ref).split("/")[0]
            out["href"] = ref
            out["title"] = ref
        elif kind == "view":
            out["detail"] = "a view in the centre"
            out["href"] = "#/" + ref.lstrip("#/")
            out["title"] = ref
    except ApiError:
        raise
    except Exception:
        pass
    return out


def _pin_rows(c, limit=200) -> list[dict]:
    rows = [dict(r) for r in c.execute("SELECT * FROM pins ORDER BY sort DESC, id DESC LIMIT ?", (limit,))]
    for r in rows:
        info = _resolve(c, r["kind"], r["ref"])
        r["exists"] = info.get("exists", True)
        r["detail"] = info.get("detail")
        r["href"] = info.get("href")
        if not r.get("label"):
            r["label"] = info.get("title") or r["ref"]
        r["resolved_ts"] = info.get("ts")
    return rows


# ---------------------------------------------------------------- handlers
def get_pins(query, body, m):
    c = con()
    return 200, {"ok": True, "pins": _pin_rows(c, _clamp(query.get("limit"), 200, 1, 1000))}


def post_pin(query, body, m):
    b = _body(body)
    kind = str(b.get("kind") or "").strip()
    ref = str(b.get("ref") or "").strip()
    if kind not in PIN_KINDS:
        raise ApiError(400, "kind must be one of: " + ", ".join(PIN_KINDS))
    if not ref:
        raise ApiError(400, "ref is required (the id, path or url being pinned)")
    if len(ref) > 1000:
        raise ApiError(400, "ref is too long")
    label = (b.get("label") or "").strip()[:200] or None
    note = (b.get("note") or "").strip()[:1000] or None
    c = con()
    top = c.execute("SELECT COALESCE(max(sort),0) FROM pins").fetchone()[0]
    c.execute("INSERT INTO pins(kind,ref,label,note,sort) VALUES(?,?,?,?,?) "
              "ON CONFLICT(kind,ref) DO UPDATE SET label=COALESCE(excluded.label,pins.label), "
              "note=COALESCE(excluded.note,pins.note), sort=excluded.sort",
              (kind, ref, label, note, top + 1))
    db.log_line("server", f"pinned {kind}:{ref}")
    return 200, {"ok": True, "pins": _pin_rows(c)}


def delete_pin(query, body, m):
    b = _body(body)
    c = con()
    if b.get("id") is not None:
        c.execute("DELETE FROM pins WHERE id=?", (_int(b["id"], "id"),))
    elif b.get("kind") and b.get("ref"):
        c.execute("DELETE FROM pins WHERE kind=? AND ref=?", (b["kind"], str(b["ref"])))
    elif query.get("id"):
        c.execute("DELETE FROM pins WHERE id=?", (_int(query["id"], "id"),))
    else:
        raise ApiError(400, "pass an id, or a kind and ref, to unpin")
    return 200, {"ok": True, "pins": _pin_rows(c)}


def get_notes(query, body, m):
    c = con()
    limit = _clamp(query.get("limit"), 50, 1, 500)
    q = (query.get("q") or "").strip()
    if q:
        rows = c.execute("SELECT * FROM notes WHERE text LIKE ? ORDER BY id DESC LIMIT ?", ("%" + q + "%", limit))
    else:
        rows = c.execute("SELECT * FROM notes ORDER BY id DESC LIMIT ?", (limit,))
    return 200, {"ok": True, "notes": [dict(r) for r in rows]}


def post_note(query, body, m):
    b = _body(body)
    text = str(b.get("text") or "").strip()
    if not text:
        raise ApiError(400, "a note needs some text")
    if len(text) > 8000:
        raise ApiError(400, "that note is over 8,000 characters — it belongs in a file")
    c = con()
    cur = c.execute("INSERT INTO notes(text,ref) VALUES(?,?)", (text, (b.get("ref") or None)))
    row = dict(c.execute("SELECT * FROM notes WHERE id=?", (cur.lastrowid,)).fetchone())
    return 201, {"ok": True, "note": row}


def delete_note(query, body, m):
    b = _body(body)
    nid = b.get("id") or query.get("id")
    if nid is None:
        raise ApiError(400, "pass the note id to delete")
    c = con()
    c.execute("DELETE FROM notes WHERE id=?", (_int(nid, "id"),))
    return 200, {"ok": True}


def get_board(query, body, m):
    c = con()
    now = datetime.datetime.now(datetime.timezone.utc)
    local_midnight = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    since = local_midnight.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    today = dict(c.execute(
        "SELECT count(*) AS sessions, sum(COALESCE(cost_usd,0)) AS cost_usd, sum(substantive) AS substantive,"
        " sum(logged) AS logged FROM sessions WHERE COALESCE(started_at, created_at) >= ?", (since,)).fetchone())
    items_today = c.execute(
        "SELECT count(*) FROM items WHERE status!='deleted' AND COALESCE(built_at,created_at) >= ?", (since,)).fetchone()[0]
    unlogged = c.execute(
        "SELECT count(*) FROM sessions WHERE (substantive=1 AND logged=0 AND waived_at IS NULL) OR gate_bypassed>0").fetchone()[0]
    recent_items = [dict(r) for r in c.execute(
        "SELECT id,title,kind,project,confidence,COALESCE(built_at,created_at) AS ts FROM items"
        " WHERE status!='deleted' ORDER BY ts DESC LIMIT 8")]
    recent_sessions = [dict(r) for r in c.execute(
        "SELECT session_id,title,project,started_at,cost_usd,substantive,logged,waived_at,gate_bypassed,"
        " (SELECT count(*) FROM session_files sf WHERE sf.session_id=sessions.session_id AND sf.ignored=0) AS files_count"
        " FROM sessions WHERE COALESCE(started_at,created_at) >= ? ORDER BY COALESCE(started_at,created_at) DESC LIMIT 12", (since,))]
    for s in recent_sessions:
        s["sid8"] = (s["session_id"] or "")[:8]
        s["state"] = ("bypassed" if s["gate_bypassed"] else "waived" if s["waived_at"]
                      else "logged" if s["logged"] else "unlogged" if s["substantive"] else "quiet")
    return 200, {
        "ok": True,
        "day_started": since,
        "today": {
            "sessions": today.get("sessions") or 0,
            "cost_usd": round(float(today.get("cost_usd") or 0), 4),
            "substantive": today.get("substantive") or 0,
            "logged": today.get("logged") or 0,
            "items": items_today,
        },
        "unlogged_total": unlogged,
        "pins": _pin_rows(c),
        "notes": [dict(r) for r in c.execute("SELECT * FROM notes ORDER BY id DESC LIMIT ?",
                                             (_clamp(query.get("limit"), 30, 1, 200),))],
        "recent_items": recent_items,
        "today_sessions": recent_sessions,
        "generated_at": db.now_iso(),
    }


ROUTES = [
    ("GET", r"/api/notes/board/?$", get_board),
    ("GET", r"/api/notes/pins/?$", get_pins),
    ("POST", r"/api/notes/pins/?$", post_pin),
    ("DELETE", r"/api/notes/pins/?$", delete_pin),
    ("GET", r"/api/notes/notes/?$", get_notes),
    ("POST", r"/api/notes/notes/?$", post_note),
    ("DELETE", r"/api/notes/notes/?$", delete_note),
]
