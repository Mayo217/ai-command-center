#!/usr/bin/env python3
"""Command Center module 7 — Transcript search & artifacts.

GET /api/search?q=&role=&project=&since=&limit=   bm25-ranked hits across every transcript,
                                                  with a snippet and jump data (sid, uuid, ts).
GET /api/search/artifacts?q=&kind=&limit=         every claude.ai artifact link and other URL
                                                  seen in assistant text, newest first.
GET /api/search/session?sid=&uuid=&around=        the stored chunks around one hit, for the
                                                  transcript excerpt panel.
GET /api/search/stats                             index size and coverage.

The index is built by `backfill.py --transcripts` over the whole corpus and kept current by
`index_session(transcript_path)` from the SessionEnd hook. Indexed: every non-meta user prompt
and every assistant text block >= 80 chars (thinking blocks and tool payloads are skipped),
chunked to <= 2,000 chars, at most 400 chunks per session, idempotent per (session_id, uuid, seq).
Stdlib only.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from modules.common import ApiError  # noqa: E402

MODULE = "search"

CHUNK_CHARS = 2000
MAX_CHUNKS_PER_SESSION = 400
MIN_ASSISTANT_CHARS = 80
MAX_SNIPPET_TOKENS = 16
URL_RE = re.compile(r"https?://[^\s<>\"'\)\]\}]+")
FTS_COLS = "text, session_id, role, project, ts"


# ------------------------------------------------------------------ chunking
def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Split on paragraph/sentence boundaries where possible so snippets stay readable."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    out, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window = text[start:end]
            cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(". "))
            if cut > size * 0.5:
                end = start + cut + 1
        out.append(text[start:end].strip())
        start = end
    return [c for c in out if c]


def extract_chunks(transcript_path: str) -> dict:
    """Stream a transcript → searchable chunks + artifact URLs. Pure: no DB, worker-safe.
    Returns {'session_id', 'project', 'chunks': [(uuid, seq, ts, role, text)], 'urls': [...],
             'truncated': bool}."""
    sid_from_name = db.session_id_for_transcript(transcript_path)
    res = {"session_id": sid_from_name, "project": None, "chunks": [], "urls": [],
           "truncated": False, "path": transcript_path}
    chunks: list[tuple] = []
    urls: dict[str, tuple] = {}
    cwd = None
    try:
        fh = open(transcript_path, "r", errors="replace")
    except OSError:
        return res
    with fh:
        for line in fh:
            if len(line) < 3 or line.startswith('{"type":"') or '"attachment":{' in line[:220]:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            t = rec.get("type")
            if t not in ("user", "assistant"):
                continue
            if rec.get("cwd"):
                cwd = rec["cwd"]
            ts = rec.get("timestamp")
            uuid = rec.get("uuid")
            msg = rec.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            pieces: list[str] = []
            if t == "user":
                if not isinstance(content, str) or rec.get("isMeta"):
                    continue
                c = content.strip()
                if not c or c.startswith("<"):      # system-ish injections are not prompts
                    continue
                pieces.append(c)
            else:
                if not isinstance(content, list):
                    continue
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        txt = b.get("text")
                        if isinstance(txt, str):
                            for m in URL_RE.finditer(txt):
                                u = m.group(0).rstrip(".,;:!?`*")
                                if 10 < len(u) < 300:
                                    urls.setdefault(u, (ts, uuid))
                            if len(txt.strip()) >= MIN_ASSISTANT_CHARS:
                                pieces.append(txt.strip())
            if len(chunks) >= MAX_CHUNKS_PER_SESSION:
                res["truncated"] = True
                continue
            for piece in pieces:
                for seq, ch in enumerate(chunk_text(piece)):
                    if len(chunks) >= MAX_CHUNKS_PER_SESSION:
                        res["truncated"] = True
                        break
                    chunks.append((uuid, seq, ts, t, ch))
    res["project"] = db.derive_project(cwd)
    res["chunks"] = chunks
    res["urls"] = [(u, ts, uu) for u, (ts, uu) in urls.items()]
    return res


# ------------------------------------------------------------------ writing
def _fts_forget(con, session_id: str) -> int:
    """Remove a session's rows from the external-content FTS index (needs the old values)."""
    rows = con.execute("SELECT id, text, session_id, role, project, ts FROM transcript_chunks "
                       "WHERE session_id=?", (session_id,)).fetchall()
    for r in rows:
        con.execute(
            f"INSERT INTO transcript_fts(transcript_fts, rowid, {FTS_COLS}) "
            "VALUES('delete',?,?,?,?,?,?)",
            (r["id"], r["text"], r["session_id"], r["role"], r["project"], r["ts"]))
    con.execute("DELETE FROM transcript_chunks WHERE session_id=?", (session_id,))
    return len(rows)


def store_chunks(con, res: dict, replace: bool = False) -> dict:
    """Write one extract_chunks() result. Idempotent through UNIQUE(session_id,uuid,seq):
    re-indexing a grown transcript adds only its new records. `replace=True` drops the session's
    existing chunks first — use it for a single-file re-index, never while walking a corpus where
    several files (a session plus its subagent transcripts) share one session_id."""
    sid = res["session_id"]
    stat = {"session_id": sid, "chunks": 0, "urls": 0, "replaced": 0}
    if replace:
        stat["replaced"] = _fts_forget(con, sid)
    if True:
        for uuid, seq, ts, role, text in res["chunks"]:
            cur = con.execute(
                "INSERT OR IGNORE INTO transcript_chunks(session_id,uuid,seq,ts,role,project,text) "
                "VALUES(?,?,?,?,?,?,?)", (sid, uuid, seq, ts, role, res["project"], text))
            if cur.lastrowid and cur.rowcount:
                con.execute(
                    f"INSERT INTO transcript_fts(rowid, {FTS_COLS}) VALUES(?,?,?,?,?,?)",
                    (cur.lastrowid, text, sid, role, res["project"], ts))
                stat["chunks"] += 1
    for url, ts, _uuid in res["urls"]:
        kind = "artifact" if "claude.ai" in url else "link"
        label = None
        if kind == "artifact":
            label = "artifact"
        con.execute(
            "INSERT INTO artifacts(url,label,kind,session_id,ts,project) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(session_id,url) DO UPDATE SET count=count+1, "
            "ts=COALESCE(artifacts.ts, excluded.ts)",
            (url, label, kind, sid, ts, res["project"]))
        stat["urls"] += 1
    return stat


def index_session(transcript_path: str, con=None, replace: bool = False) -> dict:
    """Index one transcript for search. Called by the SessionEnd hook and by backfill."""
    own = con is None
    con = con or db.connect()
    try:
        res = extract_chunks(transcript_path)
        if not res["chunks"] and not res["urls"]:
            return {"session_id": res["session_id"], "chunks": 0, "urls": 0, "replaced": 0}
        stat = store_chunks(con, res, replace=replace)
        stat["truncated"] = res["truncated"]
        return stat
    finally:
        if own:
            con.close()


# ------------------------------------------------------------------ query
_SAFE_TERM = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_'./-]*")


def fts_query(q: str) -> str:
    """Turn arbitrary user input into a valid FTS5 query: quoted AND-ed terms, `*` kept as a
    prefix marker, `-term` as NOT, bare OR honoured. Never raises on punctuation."""
    q = (q or "").strip()
    if not q:
        raise ApiError(400, "q is required")
    if q.startswith('"') and q.endswith('"') and q.count('"') == 2:
        return q
    parts: list[str] = []
    for raw in q.split():
        if raw.upper() in ("OR", "AND", "NOT"):
            parts.append(raw.upper())
            continue
        neg = raw.startswith("-")
        star = raw.endswith("*")
        m = _SAFE_TERM.search(raw)
        if not m:
            continue
        term = m.group(0).replace('"', "")
        if not term:
            continue
        tok = f'"{term}"' + ("*" if star else "")
        parts.append(("NOT " if neg and parts else "") + tok if not neg or parts else tok)
    if not parts:
        raise ApiError(400, "q has no searchable terms")
    if parts[-1] in ("OR", "AND", "NOT"):
        parts.pop()
    return " ".join(parts)


def _int(query: dict, name: str, default: int, lo: int, hi: int) -> int:
    raw = query.get(name)
    if raw in (None, ""):
        return default
    try:
        return max(lo, min(hi, int(float(raw))))
    except (TypeError, ValueError):
        raise ApiError(400, f"{name} must be a number")


def get_search(query: dict, body, match) -> tuple[int, object]:
    q = fts_query(query.get("q", ""))
    limit = _int(query, "limit", 40, 1, 200)
    offset = _int(query, "offset", 0, 0, 10_000)
    role = (query.get("role") or "").strip().lower()
    project = (query.get("project") or "").strip()
    since = (query.get("since") or "").strip()
    if role and role not in ("user", "assistant"):
        raise ApiError(400, "role must be user or assistant")
    where, args = ["transcript_fts MATCH ?"], [q]
    if role:
        where.append("c.role=?"); args.append(role)
    if project:
        where.append("c.project=?"); args.append(project)
    if since:
        where.append("c.ts>=?"); args.append(since)
    sql = f"""SELECT c.session_id, c.uuid, c.seq, c.ts, c.role, c.project,
                     snippet(transcript_fts, 0, '«', '»', ' … ', {MAX_SNIPPET_TOKENS}) snippet,
                     bm25(transcript_fts) rank, s.title session_title,
                     substr(coalesce(s.started_at,c.ts),1,10) session_date, s.cwd
              FROM transcript_fts JOIN transcript_chunks c ON c.id = transcript_fts.rowid
              LEFT JOIN sessions s ON s.session_id = c.session_id
              WHERE {' AND '.join(where)} ORDER BY rank LIMIT ? OFFSET ?"""
    con = db.connect()
    try:
        try:
            rows = [dict(r) for r in con.execute(sql, (*args, limit, offset))]
        except Exception as exc:
            raise ApiError(400, f"bad search query: {exc}")
        total = con.execute(
            f"SELECT count(*) n FROM transcript_fts JOIN transcript_chunks c "
            f"ON c.id=transcript_fts.rowid WHERE {' AND '.join(where)}", args).fetchone()["n"]
        for r in rows:
            r["rank"] = round(r["rank"], 3)
            r["resume"] = f"claude --resume {r['session_id']}"
        return 200, {"q": query.get("q"), "fts_query": q, "count": len(rows), "total": total,
                     "limit": limit, "offset": offset, "results": rows}
    finally:
        con.close()


def get_artifacts(query: dict, body, match) -> tuple[int, object]:
    limit = _int(query, "limit", 100, 1, 500)
    kind = (query.get("kind") or "").strip().lower()
    q = (query.get("q") or "").strip()
    where, args = ["1=1"], []
    if kind in ("artifact", "link"):
        where.append("a.kind=?"); args.append(kind)
    if q:
        where.append("(a.url LIKE ? OR coalesce(s.title,'') LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    con = db.connect()
    try:
        rows = [dict(r) for r in con.execute(
            f"""SELECT a.url, a.label, a.kind, a.session_id, a.ts, a.project, a.count,
                       s.title session_title, substr(coalesce(s.started_at,a.ts),1,10) session_date
                FROM artifacts a LEFT JOIN sessions s ON s.session_id=a.session_id
                WHERE {' AND '.join(where)} ORDER BY coalesce(a.ts,'') DESC LIMIT ?""",
            (*args, limit))]
        counts = {r["kind"]: r["n"] for r in con.execute(
            "SELECT kind, count(*) n FROM artifacts GROUP BY kind")}
        return 200, {"count": len(rows), "by_kind": counts, "results": rows}
    finally:
        con.close()


def get_session_chunks(query: dict, body, match) -> tuple[int, object]:
    sid = (query.get("sid") or "").strip()
    if not sid:
        raise ApiError(400, "sid is required")
    uuid = (query.get("uuid") or "").strip()
    around = _int(query, "around", 3, 0, 20)
    limit = _int(query, "limit", 40, 1, 400)
    con = db.connect()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT id, uuid, seq, ts, role, text FROM transcript_chunks WHERE session_id=? "
            "ORDER BY id", (sid,))]
        if not rows:
            raise ApiError(404, f"no indexed transcript for session {sid}")
        if uuid:
            idx = next((i for i, r in enumerate(rows) if r["uuid"] == uuid), None)
            if idx is None:
                raise ApiError(404, f"uuid {uuid} not indexed for session {sid}")
            rows = rows[max(0, idx - around): idx + around + 1]
        sess = con.execute("SELECT title, project, started_at, cwd FROM sessions WHERE session_id=?",
                           (sid,)).fetchone()
        return 200, {"session_id": sid, "session": dict(sess) if sess else None,
                     "count": len(rows), "chunks": rows[:limit],
                     "resume": f"claude --resume {sid}"}
    finally:
        con.close()


def get_stats(query: dict, body, match) -> tuple[int, object]:
    con = db.connect()
    try:
        c = con.execute("SELECT count(*) n, count(DISTINCT session_id) s, sum(length(text)) b "
                        "FROM transcript_chunks").fetchone()
        span = con.execute("SELECT min(ts) a, max(ts) b FROM transcript_chunks").fetchone()
        roles = {r["role"]: r["n"] for r in con.execute(
            "SELECT role, count(*) n FROM transcript_chunks GROUP BY role")}
        arts = con.execute("SELECT count(*) n, count(DISTINCT url) u FROM artifacts").fetchone()
        return 200, {"chunks": c["n"], "sessions_indexed": c["s"],
                     "text_bytes": c["b"] or 0, "by_role": roles,
                     "artifacts": {"rows": arts["n"], "distinct_urls": arts["u"]},
                     "first_ts": span["a"], "last_ts": span["b"],
                     "chunk_chars": CHUNK_CHARS, "max_chunks_per_session": MAX_CHUNKS_PER_SESSION}
    finally:
        con.close()


ROUTES = [
    ("GET", r"/api/search/?$", get_search),
    ("GET", r"/api/search/artifacts/?$", get_artifacts),
    ("GET", r"/api/search/session/?$", get_session_chunks),
    ("GET", r"/api/search/stats/?$", get_stats),
]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Search module — index a transcript or run a query.")
    ap.add_argument("--index", metavar="TRANSCRIPT", help="index one transcript")
    ap.add_argument("--q", help="run a search")
    ap.add_argument("--limit", type=int, default=10)
    a = ap.parse_args()
    if a.index:
        print(json.dumps(index_session(a.index, replace=True), indent=2))
    elif a.q:
        print(json.dumps(get_search({"q": a.q, "limit": str(a.limit)}, None, None)[1], indent=2))
    else:
        print(json.dumps(get_stats({}, None, None)[1], indent=2))
