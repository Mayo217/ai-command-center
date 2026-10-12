#!/usr/bin/env python3
"""tracker - the AI Command Center CLI.

A local ledger of everything Claude Code sessions build on this machine.
Stdlib only. No network except the localhost health check used by `open`.

Every read command supports --json. Exit codes: 0 ok, 1 validation error, 2 not found.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import db  # noqa: E402
import oscompat  # noqa: E402

KINDS = ["build", "enhancement", "fix", "research", "doc", "config", "automation", "analysis", "other"]
STATUSES = ["active", "wip", "superseded", "abandoned", "deleted"]
ROLES = ["created", "modified", "entry", "doc", "output"]
CONFIDENCES = ["confirmed", "inferred"]

C_RESET, C_DIM, C_BOLD = "\033[0m", "\033[2m", "\033[1m"
C_GREEN, C_RED, C_YELLOW, C_CYAN = "\033[32m", "\033[31m", "\033[33m", "\033[36m"


def _tty() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(text: str, colour: str) -> str:
    return f"{colour}{text}{C_RESET}" if _tty() else text


def die(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def out_json(obj, pretty: bool = True):
    print(json.dumps(obj, indent=2 if pretty else None, default=str))


# --------------------------------------------------------------------------- sessions
def resolve_session(con, sid: str | None = None, create: bool = True, cwd: str | None = None,
                    agent: str = "claude") -> str | None:
    """--session > $CLAUDE_SESSION_ID > newest live session (preferring this cwd) > new manual session.
    Another AI (--agent codex) never borrows a Claude session: it gets its own session for the day."""
    cwd = cwd or os.getcwd()
    if agent != "claude" and not sid:
        sid = f"{agent}-{time.strftime('%Y%m%d')}"
        if create and not con.execute("SELECT 1 FROM sessions WHERE session_id=?", (sid,)).fetchone():
            db.upsert_session(con, sid, source="manual", cwd=cwd, started_at=db.now_iso(),
                              last_activity=db.now_iso(), title=f"{agent} entries")
            con.execute("UPDATE sessions SET agent=? WHERE session_id=?", (agent, sid))
        return sid
    sid = sid or os.environ.get("CLAUDE_SESSION_ID") or None
    if sid:
        row = con.execute("SELECT session_id FROM sessions WHERE session_id=?", (sid,)).fetchone()
        if not row and create:
            db.upsert_session(con, sid, source="live", cwd=cwd, started_at=db.now_iso(), last_activity=db.now_iso())
        return sid
    row = con.execute(
        "SELECT session_id FROM sessions WHERE source='live' AND cwd=? AND ended_at IS NULL "
        "ORDER BY COALESCE(last_activity,started_at,created_at) DESC LIMIT 1", (cwd,)).fetchone()
    if not row:
        row = con.execute(
            "SELECT session_id FROM sessions WHERE source='live' "
            "ORDER BY COALESCE(last_activity,started_at,created_at) DESC LIMIT 1").fetchone()
    if row:
        return row["session_id"]
    if not create:
        return None
    sid = "manual-" + time.strftime("%Y%m%d%H%M%S")
    db.upsert_session(con, sid, source="manual", cwd=cwd, started_at=db.now_iso(), last_activity=db.now_iso(),
                      title="manual CLI entries")
    return sid


def session_cwd(con, sid: str) -> str:
    row = con.execute("SELECT cwd FROM sessions WHERE session_id=?", (sid,)).fetchone()
    return (row["cwd"] if row and row["cwd"] else os.getcwd())


# --------------------------------------------------------------------------- item creation
def agent_name(raw: str | None) -> str:
    """--agent > $TRACKER_AGENT > claude. Lower-case, letters/digits/dash only."""
    v = (raw or os.environ.get("TRACKER_AGENT") or "claude").strip().lower()
    return "".join(ch for ch in v if ch.isalnum() or ch == "-") or "claude"


def parse_file_arg(raw: str) -> tuple[str, str]:
    """PATH[:role] -> (path, role). Only splits when the suffix is a known role."""
    if ":" in raw:
        head, tail = raw.rsplit(":", 1)
        if tail.strip().lower() in ROLES and head.strip():
            return head.strip(), tail.strip().lower()
    return raw.strip(), ""


def parse_link_arg(raw: str) -> tuple[str, str | None]:
    if "|" in raw:
        url, label = raw.split("|", 1)
        return url.strip(), label.strip() or None
    return raw.strip(), None


def validate_item(d: dict) -> tuple[str, str, str, str, str]:
    cfg = db.config().get("gate", {})
    min_sum = int(cfg.get("min_summary_chars", 60))
    title = (d.get("title") or "").strip()
    summary = (d.get("summary") or "").strip()
    usage = (d.get("usage") or "").strip()
    kind = (d.get("kind") or "build").strip().lower()
    status = (d.get("status") or "active").strip().lower()
    if not (8 <= len(title) <= 140):
        die(f"error: --title must be 8..140 chars (got {len(title)}): {title!r}")
    if len(summary) < min_sum:
        die(f"error: --summary must be >= {min_sum} chars (got {len(summary)}). "
            "Say what the thing is AND why it exists.")
    if summary.strip().lower() == title.strip().lower():
        die("error: --summary must add information, not repeat --title.")
    if kind not in KINDS:
        die(f"error: --kind must be one of {'|'.join(KINDS)} (got {kind!r})")
    if status not in STATUSES:
        die(f"error: --status must be one of {'|'.join(STATUSES)} (got {status!r})")
    return title, summary, usage, kind, status


def create_item(con, sid: str, d: dict, cwd: str, source: str = "cli", confidence: str = "confirmed",
                quiet: bool = False) -> dict:
    title, summary, usage, kind, status = validate_item(d)

    files: list[tuple[str, str]] = []
    for f in d.get("files") or []:
        if isinstance(f, dict):
            p, role = f.get("path", ""), (f.get("role") or "").lower()
        else:
            p, role = parse_file_arg(str(f))
        if not p:
            continue
        p = db.norm_path(p, cwd)
        if role not in ROLES:
            role = "created" if not os.path.exists(p) else "modified"
            role = "created" if role == "created" else role
        files.append((p, role))

    attached = 0
    if not files:
        for r in db.pending_files(con, sid):
            role = "created" if r["op"] == "create" else "modified"
            files.append((r["path"], role))
            attached += 1

    # first file is the entry point unless a role was given explicitly
    if files and all(r in ("created", "modified") for _, r in files):
        files[0] = (files[0][0], "entry")

    tags = [str(t).strip() for t in (d.get("tags") or []) if str(t).strip()]
    project = (d.get("project") or "").strip() or db.derive_project(files[0][0] if files else cwd)
    if project not in tags:
        tags.append(project)

    supersedes = d.get("supersedes")
    if supersedes:
        try:
            supersedes = int(supersedes)
        except (TypeError, ValueError):
            die(f"error: --supersedes must be an item id (got {supersedes!r})")
        if not con.execute("SELECT 1 FROM items WHERE id=?", (supersedes,)).fetchone():
            die(f"error: --supersedes {supersedes}: no such item", 2)

    if not quiet:
        for sim in db.similar_items(con, title):
            print(c(f"similar item exists: #{sim['id']} {sim['title']} ({sim['project'] or '-'}, "
                    f"{sim['kind']})", C_YELLOW), file=sys.stderr)

    sess = con.execute("SELECT model, cost_usd, agent FROM sessions WHERE session_id=?", (sid,)).fetchone()
    agent = agent_name(d.get("agent") or (sess["agent"] if sess else None))
    if agent != "claude" and agent not in tags:
        tags.append(agent)
    cur = con.execute(
        "INSERT INTO items(session_id,built_at,kind,title,summary,usage,project,status,confidence,source,"
        "supersedes_id,tags,model,agent) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, db.now_iso(), kind, title, summary, usage or None, project, status, confidence, source,
         supersedes, json.dumps(tags), sess["model"] if sess else None, agent))
    item_id = cur.lastrowid

    missing = []
    for p, role in files:
        ex = os.path.exists(p)
        if not ex:
            missing.append(p)
        size = os.path.getsize(p) if ex and os.path.isfile(p) else None
        con.execute("INSERT OR IGNORE INTO item_files(item_id,path,role,exists_at_log,size_bytes) VALUES(?,?,?,?,?)",
                    (item_id, p, role, 1 if ex else 0, size))
        con.execute("UPDATE session_files SET logged_item_id=? WHERE session_id=? AND path=? AND logged_item_id IS NULL",
                    (item_id, sid, p))
    # a gate-bypass auto item may be holding these paths as "unclaimed"; they now have an owner
    db.release_unclaimed(con, [p for p, _ in files], claiming_item_id=item_id)

    for l in d.get("links") or []:
        if isinstance(l, dict):
            url, label = l.get("url", ""), l.get("label")
        else:
            url, label = parse_link_arg(str(l))
        if url:
            con.execute("INSERT OR IGNORE INTO item_links(item_id,url,label) VALUES(?,?,?)", (item_id, url, label))

    if supersedes:
        con.execute("UPDATE items SET status='superseded', updated_at=? WHERE id=? AND status='active'",
                    (db.now_iso(), supersedes))
        db.reindex_item(con, supersedes)

    if confidence == "confirmed":
        con.execute("UPDATE sessions SET logged=1, updated_at=? WHERE session_id=?", (db.now_iso(), sid))
    db.reindex_item(con, item_id)
    db.add_event(con, sid, "log", {"item_id": item_id, "title": title, "files": len(files), "attached": attached,
                                   "source": source})
    if missing:
        for p in missing:
            print(f"warning: file does not exist: {p}", file=sys.stderr)
    return {"id": item_id, "title": title, "kind": kind, "project": project,
            "files": [{"path": p, "role": r} for p, r in files], "attached": attached, "missing": missing}


# --------------------------------------------------------------------------- rendering
def row_line(r) -> str:
    when = (r["built_at"] or r["created_at"] or "")[:10]
    conf = "" if r["confidence"] == "confirmed" else c(" ~inferred", C_DIM)
    st = "" if r["status"] == "active" else c(f" [{r['status']}]", C_YELLOW)
    return f"{c('#' + str(r['id']), C_CYAN):>10} {c(when, C_DIM)}  {r['kind']:<11} {c(r['project'] or '-', C_DIM):<16} {r['title']}{st}{conf}"


def item_dict(con, r, with_children: bool = True) -> dict:
    d = dict(r)
    try:
        d["tags"] = json.loads(d.get("tags") or "[]")
    except Exception:
        d["tags"] = []
    if with_children:
        d["files"] = [{**dict(f), "exists": os.path.exists(f["path"])} for f in db.item_files_of(con, r["id"])]
        d["links"] = [dict(x) for x in con.execute("SELECT url,label FROM item_links WHERE item_id=?", (r["id"],))]
    return d


def print_item(con, r):
    d = item_dict(con, r)
    print(f"{c('#' + str(d['id']), C_CYAN)} {c(d['title'], C_BOLD)}")
    print(f"  {d['kind']} · {d['project'] or '-'} · {d['status']} · {d['confidence']} · "
          f"built {(d['built_at'] or d['created_at'] or '')[:19].replace('T', ' ')}")
    if d.get("supersedes_id"):
        print(f"  supersedes #{d['supersedes_id']}")
    print(f"\n  {c('what/why:', C_DIM)} {d['summary']}")
    if d.get("usage"):
        print(f"\n  {c('usage:', C_DIM)}    {d['usage']}")
    if d["files"]:
        print(f"\n  {c('files:', C_DIM)}")
        for f in d["files"]:
            badge = c("ok     ", C_GREEN) if f["exists"] else c("MISSING", C_RED)
            print(f"    {badge} {f['role']:<9} {f['path']}")
    if d["links"]:
        print(f"\n  {c('links:', C_DIM)}")
        for l in d["links"]:
            print(f"    {l['url']}" + (f"  ({l['label']})" if l["label"] else ""))
    if d["tags"]:
        print(f"\n  {c('tags:', C_DIM)}     {', '.join(d['tags'])}")
    if d.get("session_id"):
        s = con.execute("SELECT * FROM sessions WHERE session_id=?", (d["session_id"],)).fetchone()
        if s:
            print(f"\n  {c('session:', C_DIM)}  {s['session_id'][:8]} · {s['project'] or '-'} · "
                  f"{(s['started_at'] or '')[:16].replace('T', ' ')} · {s['model'] or 'model?'}"
                  + (f" · ${s['cost_usd']:.2f}" if s["cost_usd"] else ""))
            if s["transcript_path"]:
                print(f"  {c('transcript:', C_DIM)} {s['transcript_path']}")


# --------------------------------------------------------------------------- commands
def cmd_log(a):
    con = db.connect()
    payload = None
    if a.json_in:
        raw = sys.stdin.read() if a.json_in == "-" else Path(a.json_in).read_text()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            die(f"error: --json-in is not valid JSON: {e}")
    agent = agent_name(getattr(a, "agent", None))
    sid = resolve_session(con, a.session, agent=agent)
    cwd = os.getcwd() if agent != "claude" else session_cwd(con, sid)
    results = []
    if payload:
        items = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(items, list) or not items:
            die('error: --json-in must be {"items":[{...}]} with at least one item')
        for it in items:
            it.setdefault("agent", agent)
            results.append(create_item(con, sid, it, cwd, quiet=a.json))
    else:
        d = {"title": a.title, "summary": a.summary, "usage": a.usage, "kind": a.kind, "status": a.status,
             "project": a.project, "tags": a.tag or [], "supersedes": a.supersedes,
             "files": a.file or [], "links": a.link or [], "agent": agent}
        if not a.title or not a.summary:
            die("error: --title and --summary are required (or use --json-in)")
        results.append(create_item(con, sid, d, cwd, quiet=a.json))
    gs = db.gate_state(con, sid)
    if a.json:
        out_json({"ok": True, "session": sid, "items": results, "gate": {"pending": gs["pending_count"],
                                                                        "would_block": gs["would_block"]}})
    else:
        for r in results:
            print(f"{c('logged item #' + str(r['id']), C_GREEN)}: {r['title']}")
            if r["attached"]:
                print(f"  auto-attached {r['attached']} file(s) touched in this session:")
                for f in r["files"]:
                    print(f"    {f['role']:<9} {f['path']}")
            elif r["files"]:
                for f in r["files"]:
                    print(f"    {f['role']:<9} {f['path']}")
        if gs["pending_count"]:
            print(c(f"note: {gs['pending_count']} file touch(es) still unlogged "
                    f"(the Stop gate will ask again): " + ", ".join(p["path"] for p in gs["pending"][:5]), C_YELLOW))
        else:
            print(c("gate clear.", C_DIM))
    return 0


def cmd_waive(a):
    con = db.connect()
    min_r = int(db.config().get("gate", {}).get("min_waive_reason_chars", 20))
    reason = (a.reason or "").strip()
    if len(reason) < min_r:
        die(f"error: --reason must be >= {min_r} chars (got {len(reason)}). Say why nothing durable was built.")
    sid = resolve_session(con, a.session)
    pend = db.pending_files(con, sid)
    durable = [r["path"] for r in pend if db.is_durable(r["path"])]
    if durable:
        msg = ("error: waive refused - this session changed files that look durable:\n  "
               + "\n  ".join(durable[:12])
               + "\nLog them instead: tracker log --title ... --summary ... --usage ... --file ...")
        if a.json:
            out_json({"ok": False, "error": "durable_files_pending", "files": durable})
            sys.exit(1)
        die(msg)
    ts = db.now_iso()
    con.execute("UPDATE sessions SET waived_at=?, waived_reason=?, updated_at=? WHERE session_id=?",
                (ts, reason, ts, sid))
    con.execute("UPDATE session_files SET ignored=1 WHERE session_id=? AND logged_item_id IS NULL", (sid,))
    db.add_event(con, sid, "waive", {"reason": reason, "files": [r["path"] for r in pend]})
    if a.json:
        out_json({"ok": True, "session": sid, "waived_at": ts, "reason": reason,
                  "files_waived": [r["path"] for r in pend]})
    else:
        print(c(f"waived session {sid[:8]}", C_GREEN) + f": {reason}")
        if pend:
            print(f"  {len(pend)} non-durable file touch(es) cleared from the gate.")
    return 0


def cmd_status(a):
    t0 = time.time()
    con = db.connect()
    sid = resolve_session(con, a.session, create=False)
    if not sid:
        if a.json:
            out_json({"ok": True, "session": None, "note": "no sessions yet"})
        else:
            print("no sessions recorded yet.")
        return 0
    s = con.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    gs = db.gate_state(con, sid)
    items = [dict(r) for r in con.execute(
        "SELECT id,title,kind,created_at FROM items WHERE session_id=? AND status!='deleted' ORDER BY id", (sid,))]
    totals = con.execute("SELECT count(*) n FROM items WHERE status!='deleted'").fetchone()["n"]
    payload = {"ok": True, "session": sid, "project": s["project"], "cwd": s["cwd"],
               "started_at": s["started_at"], "user_turns": s["user_turns"], "tool_calls": s["tool_calls"],
               "writes": s["writes"], "edits": s["edits"], "bash_calls": s["bash_calls"],
               "items": items, "items_total_db": totals,
               "pending_files": [{"path": p["path"], "op": p["op"], "durable": db.is_durable(p["path"])}
                                 for p in gs["pending"]],
               "gate": {"would_block": gs["would_block"], "why": gs["why"], "blocks": gs["blocks"],
                        "max_blocks": gs["max_blocks"]},
               "waived_at": s["waived_at"], "waived_reason": s["waived_reason"],
               "elapsed_ms": round((time.time() - t0) * 1000, 1)}
    payload["usage_warning"] = db.usage_warning_line(con)
    if a.json:
        out_json(payload)
        return 0
    print(f"{c('session', C_DIM)} {sid[:8]}  {s['project'] or '-'}  {s['cwd'] or ''}")
    print(f"{c('turns', C_DIM)}   {s['user_turns']} user · {s['tool_calls']} tools "
          f"({s['writes']} writes, {s['edits']} edits, {s['bash_calls']} bash)")
    if items:
        print(f"{c('logged', C_DIM)}  {len(items)} item(s): " + ", ".join(f"#{i['id']} {i['title']}" for i in items))
    else:
        print(f"{c('logged', C_DIM)}  nothing yet")
    if s["waived_at"]:
        print(f"{c('waived', C_DIM)}  {s['waived_at'][:19]} - {s['waived_reason']}")
    if gs["pending"]:
        print(f"{c('pending', C_DIM)} {gs['pending_count']} unlogged file touch(es):")
        if gs["pending_count"] <= 15:
            for p in gs["pending"]:
                print(f"         {'durable' if db.is_durable(p['path']) else 'data   '} {p['op']:<7} {p['path']}")
        else:
            groups: dict[str, list] = {}
            for p in gs["pending"]:
                groups.setdefault(os.path.dirname(p["path"]), []).append(p)
            for d in sorted(groups, key=lambda k: -len(groups[k])):
                names = [os.path.basename(p["path"]) for p in groups[d]]
                dur = sum(1 for p in groups[d] if db.is_durable(p["path"]))
                print(f"         {c(d + '/', C_CYAN)}  ({len(names)} file(s), {dur} durable)")
                print(f"           {', '.join(names[:14])}" + (f" +{len(names) - 14} more" if len(names) > 14 else ""))
            print(c("         missing/phantom paths? `tracker dismiss <glob> --missing --reason \"...\"`", C_DIM))
    else:
        print(f"{c('pending', C_DIM)} none")
    verdict = c("WOULD BLOCK", C_RED) if gs["would_block"] else c("clear", C_GREEN)
    print(f"{c('gate', C_DIM)}    {verdict} ({gs['why']}; blocks {gs['blocks']}/{gs['max_blocks']})")
    if payload.get("usage_warning"):
        print(c(payload["usage_warning"], C_YELLOW))
    print(c(f"({payload['elapsed_ms']} ms · {totals} items in ledger)", C_DIM))
    return 0


def _items_query(con, where: list[str], args: list, limit: int):
    sql = ("SELECT * FROM items WHERE " + " AND ".join(where) +
           " ORDER BY COALESCE(built_at, created_at) DESC, id DESC LIMIT ?")
    return con.execute(sql, [*args, limit]).fetchall()


def cmd_recent(a):
    con = db.connect()
    where = ["status!='deleted'"]
    args: list = []
    if a.project:
        where.append("project=?")
        args.append(a.project)
        label = a.project
    else:
        cwd = db.norm_path(a.cwd or os.getcwd())
        proj = db.derive_project(cwd)
        where.append("(project=? OR id IN (SELECT item_id FROM item_files WHERE path=? OR path LIKE ?))")
        args += [proj, cwd, cwd.rstrip("/") + "/%"]
        label = f"{proj} ({cwd})"
    rows = _items_query(con, where, args, a.limit)
    if a.json:
        out_json({"ok": True, "scope": label, "count": len(rows), "items": [item_dict(con, r) for r in rows]})
        return 0
    if not rows:
        print(f"nothing logged yet for {label}.")
        return 0
    print(c(f"recently built in {label}:", C_DIM))
    for r in rows:
        print(row_line(r))
    return 0


def cmd_list(a):
    con = db.connect()
    where, args = ["status!='deleted'" if a.status != "deleted" else "1=1"], []
    if a.project:
        where.append("project=?"); args.append(a.project)
    if a.kind:
        where.append("kind=?"); args.append(a.kind)
    if a.status:
        where.append("status=?"); args.append(a.status)
    if a.confidence:
        where.append("confidence=?"); args.append(a.confidence)
    if a.since:
        where.append("COALESCE(built_at, created_at) >= ?"); args.append(a.since)
    rows = _items_query(con, where, args, a.limit)
    if a.json:
        out_json({"ok": True, "count": len(rows), "items": [item_dict(con, r) for r in rows]})
        return 0
    if not rows:
        print("no items match.")
        return 0
    for r in rows:
        print(row_line(r))
    print(c(f"{len(rows)} item(s)", C_DIM))
    return 0


def _fts_query(q: str) -> str:
    """Make an arbitrary user string safe for FTS5 MATCH."""
    q = q.strip()
    if not q:
        return '""'
    if any(ch in q for ch in '"()*:^-') or " OR " in q or " AND " in q or " NOT " in q:
        return '"' + q.replace('"', '""') + '"'
    return " ".join(f'"{t}"' for t in q.split())


def _snippet(text: str, terms: list[str], width: int = 180) -> str:
    t = " ".join((text or "").split())
    low = t.lower()
    pos = -1
    for term in terms:
        pos = low.find(term.lower())
        if pos >= 0:
            break
    if pos < 0:
        return t[:width] + ("..." if len(t) > width else "")
    start = max(0, pos - width // 3)
    end = min(len(t), start + width)
    return ("..." if start else "") + t[start:end] + ("..." if end < len(t) else "")


def cmd_search(a):
    con = db.connect()
    terms = [w for w in a.query.split() if len(w) > 1]
    try:
        rows = con.execute(
            "SELECT i.*, bm25(items_fts) AS rank FROM items_fts JOIN items i ON i.id=items_fts.rowid "
            "WHERE items_fts MATCH ? AND i.status!='deleted' ORDER BY rank LIMIT ?",
            (_fts_query(a.query), a.limit)).fetchall()
    except Exception:
        like = f"%{a.query}%"
        rows = con.execute(
            "SELECT *, 0 AS rank FROM items WHERE status!='deleted' AND (title LIKE ? OR summary LIKE ? OR usage LIKE ?) "
            "ORDER BY COALESCE(built_at,created_at) DESC LIMIT ?", (like, like, like, a.limit)).fetchall()
    if a.json:
        out = []
        for r in rows:
            d = item_dict(con, r)
            d["snippet"] = _snippet(f"{r['summary']} {r['usage'] or ''}", terms)
            out.append(d)
        out_json({"ok": True, "query": a.query, "count": len(out), "items": out})
        return 0
    if not rows:
        print(f"no items match {a.query!r}. (try `tracker list` or a shorter query)")
        return 0
    for r in rows:
        print(row_line(r))
        print(f"           {c(_snippet(f'{r['summary']} {r['usage'] or ''}', terms), C_DIM)}")
    print(c(f"{len(rows)} hit(s) for {a.query!r}", C_DIM))
    return 0


def cmd_show(a):
    con = db.connect()
    r = con.execute("SELECT * FROM items WHERE id=?", (a.id,)).fetchone()
    if not r:
        if a.json:
            out_json({"ok": False, "error": "not_found", "id": a.id})
        else:
            print(f"no item #{a.id}", file=sys.stderr)
        return 2
    if a.json:
        d = item_dict(con, r)
        if r["session_id"]:
            s = con.execute("SELECT * FROM sessions WHERE session_id=?", (r["session_id"],)).fetchone()
            d["session"] = dict(s) if s else None
        out_json({"ok": True, "item": d})
    else:
        print_item(con, r)
    return 0


def cmd_edit(a):
    con = db.connect()
    r = con.execute("SELECT * FROM items WHERE id=?", (a.id,)).fetchone()
    if not r:
        print(f"no item #{a.id}", file=sys.stderr)
        return 2
    sets, args, changed = [], [], []
    for field in ("title", "summary", "usage", "status", "kind"):
        v = getattr(a, field, None)
        if v is not None:
            if field == "kind" and v not in KINDS:
                die(f"error: --kind must be one of {'|'.join(KINDS)}")
            if field == "status" and v not in STATUSES:
                die(f"error: --status must be one of {'|'.join(STATUSES)}")
            if field == "title" and not (8 <= len(v) <= 140):
                die("error: --title must be 8..140 chars")
            if field == "summary" and len(v) < int(db.config().get("gate", {}).get("min_summary_chars", 60)):
                die("error: --summary too short")
            sets.append(f"{field}=?"); args.append(v); changed.append(field)
    if a.confidence:
        if a.confidence not in CONFIDENCES:
            die(f"error: --confidence must be one of {'|'.join(CONFIDENCES)}")
        sets.append("confidence=?"); args.append(a.confidence); changed.append("confidence")
        if a.confidence == "confirmed":
            sets.append("verified_at=?"); args.append(db.now_iso())
    tags = json.loads(r["tags"] or "[]")
    if a.add_tag or a.rm_tag:
        for t in a.add_tag or []:
            if t not in tags:
                tags.append(t)
        for t in a.rm_tag or []:
            if t in tags:
                tags.remove(t)
        sets.append("tags=?"); args.append(json.dumps(tags)); changed.append("tags")
    if sets:
        sets.append("updated_at=?"); args.append(db.now_iso())
        con.execute(f"UPDATE items SET {', '.join(sets)} WHERE id=?", [*args, a.id])
    cwd = session_cwd(con, r["session_id"]) if r["session_id"] else os.getcwd()
    for raw in a.add_file or []:
        p, role = parse_file_arg(raw)
        p = db.norm_path(p, cwd)
        ex = os.path.exists(p)
        con.execute("INSERT OR IGNORE INTO item_files(item_id,path,role,exists_at_log,size_bytes) VALUES(?,?,?,?,?)",
                    (a.id, p, role or ("modified" if ex else "created"), 1 if ex else 0,
                     os.path.getsize(p) if ex and os.path.isfile(p) else None))
        if r["session_id"]:
            con.execute("UPDATE session_files SET logged_item_id=? WHERE session_id=? AND path=? AND logged_item_id IS NULL",
                        (a.id, r["session_id"], p))
        db.release_unclaimed(con, [p], claiming_item_id=a.id)
        changed.append(f"+file {p}")
    for raw in a.rm_file or []:
        p = db.norm_path(parse_file_arg(raw)[0], cwd)
        con.execute("DELETE FROM item_files WHERE item_id=? AND path=?", (a.id, p))
        changed.append(f"-file {p}")
    for raw in a.add_link or []:
        url, label = parse_link_arg(raw)
        con.execute("INSERT OR IGNORE INTO item_links(item_id,url,label) VALUES(?,?,?)", (a.id, url, label))
        changed.append(f"+link {url}")
    db.reindex_item(con, a.id)
    if not changed:
        die("error: nothing to change - pass at least one field")
    if a.json:
        r2 = con.execute("SELECT * FROM items WHERE id=?", (a.id,)).fetchone()
        out_json({"ok": True, "id": a.id, "changed": changed, "item": item_dict(con, r2)})
    else:
        print(c(f"updated item #{a.id}", C_GREEN) + ": " + ", ".join(changed))
    return 0


def cmd_dismiss(a):
    """Clear pending file touches that are not real work (regex debris, phantom paths from a
    heredoc fixture, someone else's file) without inventing a fake item."""
    import fnmatch as _fn
    con = db.connect()
    min_r = int(db.config().get("gate", {}).get("min_waive_reason_chars", 20))
    reason = (a.reason or "").strip()
    if len(reason) < min_r:
        die(f"error: --reason must be >= {min_r} chars (got {len(reason)}). Say why these are not real work.")
    sid = resolve_session(con, a.session, create=False)
    if not sid:
        die("error: no session to dismiss files from", 2)
    pend = db.pending_files(con, sid)
    cwd = session_cwd(con, sid)
    hit = []
    for r in pend:
        path = r["path"]
        if a.missing and not os.path.exists(path):
            hit.append(r); continue
        for pat in a.paths or []:
            norm = db.norm_path(pat, cwd) if not any(ch in pat for ch in "*?[") else pat
            if path == norm or _fn.fnmatch(path, pat) or _fn.fnmatch(path, norm):
                hit.append(r); break
    if not hit:
        die("error: nothing matched - pass paths (globs ok) and/or --missing. `tracker status` lists what is pending.")
    durable_real = [r["path"] for r in hit if db.is_durable(r["path"]) and os.path.exists(r["path"])]
    if durable_real and not a.force:
        die("error: refusing to dismiss files that exist and look durable:\n  "
            + "\n  ".join(durable_real[:12]) + "\nLog them, or pass --force if they really are not yours.")
    for r in hit:
        con.execute("UPDATE session_files SET ignored=1 WHERE id=?", (r["id"],))
    db.add_event(con, sid, "dismiss", {"reason": reason, "files": [r["path"] for r in hit]})
    if a.json:
        out_json({"ok": True, "session": sid, "dismissed": [r["path"] for r in hit], "reason": reason})
    else:
        print(c(f"dismissed {len(hit)} pending file(s)", C_GREEN) + f": {reason}")
        for r in hit[:20]:
            print(f"    {r['path']}")
    return 0


def cmd_sessions(a):
    con = db.connect()
    min_turns = int(db.config().get("substantive", {}).get("min_user_turns", 3))
    where = [
        "(s.substantive=1 OR s.user_turns>=? OR EXISTS(SELECT 1 FROM session_files f "
        "WHERE f.session_id=s.session_id AND f.ignored=0) "
        "OR EXISTS(SELECT 1 FROM items i2 WHERE i2.session_id=s.session_id AND i2.status!='deleted'))"
    ]
    args: list = [min_turns]
    if a.unlogged:
        where.append("s.logged=0 AND s.waived_at IS NULL "
                     "AND NOT EXISTS(SELECT 1 FROM items i WHERE i.session_id=s.session_id AND i.status!='deleted')")
    if a.since:
        where.append("COALESCE(s.started_at, s.created_at) >= ?"); args.append(a.since)
    if a.project:
        where.append("s.project=?"); args.append(a.project)
    rows = con.execute(
        "SELECT s.*, (SELECT count(*) FROM items i WHERE i.session_id=s.session_id AND i.status!='deleted') n_items, "
        "(SELECT count(*) FROM session_files f WHERE f.session_id=s.session_id AND f.ignored=0) n_files "
        "FROM sessions s WHERE " + " AND ".join(where) +
        " ORDER BY COALESCE(s.started_at, s.created_at) DESC LIMIT ?", [*args, a.limit]).fetchall()
    if a.json:
        out_json({"ok": True, "count": len(rows), "sessions": [dict(r) for r in rows]})
        return 0
    if not rows:
        print("no sessions match.")
        return 0
    for s in rows:
        flag = c("UNLOGGED", C_RED) if (not s["n_items"] and not s["waived_at"]) else (
            c("waived", C_YELLOW) if s["waived_at"] and not s["n_items"] else c("logged", C_GREEN))
        bypass = c(" bypassed", C_RED) if s["gate_bypassed"] else ""
        print(f"{c(s['session_id'][:8], C_CYAN)} {(s['started_at'] or '')[:16].replace('T', ' ')} "
              f"{(s['project'] or '-'):<16} {flag}{bypass} {s['n_items']} item(s) {s['n_files']} file(s)")
        title = s["title"] or (s["first_prompt"] or "")[:90]
        if title:
            print(f"         {c(title.replace(chr(10), ' ')[:110], C_DIM)}")
    print(c(f"{len(rows)} session(s)", C_DIM))
    return 0


def cmd_backfill(a, extra: list[str]):
    import subprocess
    script = ROOT / "backfill.py"
    if not script.exists():
        die(f"error: {script} not found", 1)
    cmd = [sys.executable, str(script)]
    if a.since: cmd += ["--since", a.since]
    if a.limit: cmd += ["--limit", str(a.limit)]
    if a.dry_run: cmd += ["--dry-run"]
    if a.force: cmd += ["--force"]
    if a.summarize: cmd += ["--summarize"]
    if a.json: cmd += ["--json"]
    cmd += extra
    return subprocess.call(cmd)


def _health_json(port: int, timeout: float = 2.0) -> dict | None:
    """The server's /health payload, or None if it is not answering."""
    import http.client
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", "/health")
        r = conn.getresponse()
        body = r.read(200_000)
        conn.close()
        return json.loads(body) if r.status == 200 else None
    except Exception:
        return None


def _stop_server_on(port: int) -> None:
    """Stop the tracker server that answers on `port` (its pid comes from /health)."""
    import signal
    pid = (_health_json(port, timeout=1.0) or {}).get("pid")
    if not pid:
        return
    try:
        os.kill(int(pid), signal.SIGTERM)  # TerminateProcess on Windows
    except OSError:
        return
    for _ in range(20):
        if not _health(port, 0.3):
            return
        time.sleep(0.2)


def _health(port: int, timeout: float = 1.0) -> bool:
    import http.client
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", "/health")
        r = conn.getresponse()
        ok = r.status == 200 and b'"ok"' in r.read(200)
        conn.close()
        return ok
    except Exception:
        return False


def cmd_serve(a):
    script = ROOT / "server.py"
    if not script.exists():
        die(f"error: {script} not found", 1)
    os.execv(sys.executable, [sys.executable, str(script), "--port", str(a.port)])


def cmd_open(a):
    import subprocess
    port = a.port or int(db.config().get("port", 8790))
    url = f"http://127.0.0.1:{port}"
    if not _health(port):
        script = ROOT / "server.py"
        if not script.exists():
            die(f"error: server.py not found; cannot start the app.", 1)
        plist = Path.home() / "Library/LaunchAgents/com.aicommandcenter.server.plist"
        started = False
        if plist.exists() and oscompat.has_launchd():
            uid = oscompat.uid()
            rc = subprocess.call(["launchctl", "kickstart", "-k", f"gui/{uid}/com.aicommandcenter.server"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            started = rc == 0
        if not started:
            db.LOG_DIR.mkdir(exist_ok=True)
            logf = open(db.LOG_DIR / "server.log", "a")
            subprocess.Popen([sys.executable, str(script), "--port", str(port)],
                             stdout=logf, stderr=logf, **oscompat.detached_kwargs())
        for _ in range(25):
            if _health(port, 0.4):
                break
            time.sleep(0.2)
    ok = _health(port)
    if ok:
        oscompat.open_path(url)
    if a.json:
        out_json({"ok": ok, "url": url, "port": port})
    else:
        print(c(f"AI Command Center: {url}", C_GREEN) if ok else c(f"server not reachable on {port}", C_RED))
    return 0 if ok else 1


def cmd_export(a):
    con = db.connect()
    where, args = ["status!='deleted'"], []
    if a.project:
        where.append("project=?"); args.append(a.project)
    if a.since:
        where.append("COALESCE(built_at, created_at) >= ?"); args.append(a.since)
    rows = _items_query(con, where, args, a.limit)
    if a.json_out or a.json:
        out_json({"ok": True, "count": len(rows), "items": [item_dict(con, r) for r in rows]})
        return 0
    scope = " · ".join(x for x in [a.project or "all projects", f"since {a.since}" if a.since else ""] if x)
    print(f"# AI Command Center digest ({scope})")
    print(f"_{len(rows)} item(s), generated {time.strftime('%Y-%m-%d %H:%M')}_\n")
    by_project: dict[str, list] = {}
    for r in rows:
        by_project.setdefault(r["project"] or "unknown", []).append(r)
    for proj in sorted(by_project):
        print(f"\n## {proj}\n")
        for r in by_project[proj]:
            d = item_dict(con, r)
            when = (d["built_at"] or d["created_at"] or "")[:10]
            mark = "" if d["confidence"] == "confirmed" else " _(inferred)_"
            print(f"### #{d['id']} {d['title']}{mark}")
            print(f"`{d['kind']}` · {when} · {d['status']}\n")
            print(f"{d['summary']}\n")
            if d["usage"]:
                print(f"**Use:** {d['usage']}\n")
            if d["files"]:
                print("**Files:**")
                for f in d["files"][:12]:
                    print(f"- `{f['path']}` ({f['role']}{'' if f['exists'] else ', missing'})")
                print()
            if d["links"]:
                for l in d["links"]:
                    print(f"- [{l['label'] or l['url']}]({l['url']})")
                print()
    return 0


def doctor_checks(fix: bool = False) -> list[dict]:
    """The installation health checks, as data. `tracker doctor` renders these; the Command Center
    config module (modules/config.py) imports this function so the app shows the same truth."""
    a = argparse.Namespace(fix=fix, json=True)
    return _doctor(a)


def cmd_doctor(a):
    # --full hands the whole job to tests/run_all.sh, which runs every suite AND
    # `tracker doctor` itself, so there is exactly one definition of "green".
    if getattr(a, "full", False):
        import subprocess
        runner = ROOT / "tests" / "selftest.py"
        if not runner.exists():
            die(f"error: {runner} is missing", 1)
        return subprocess.call([sys.executable, str(runner)], cwd=str(ROOT))
    checks = _doctor(a)
    if a.json:
        out_json({"ok": all(x["ok"] for x in checks), "checks": checks})
    else:
        for x in checks:
            mark = c(" ok ", C_GREEN) if x["ok"] else c("FAIL", C_RED)
            print(f"[{mark}] {x['check']:<42} {c(x['detail'], C_DIM)}")
            if not x["ok"] and x.get("fix"):
                import textwrap as _tw
                for ln in _tw.wrap(x["fix"], 86):
                    print("         " + c("fix: " + ln if ln == _tw.wrap(x["fix"], 86)[0]
                                           else "     " + ln, C_YELLOW))
        bad = [x for x in checks if not x["ok"]]
        print(("\n" + c(f"{len(bad)} check(s) need attention.", C_YELLOW)) if bad else ("\n" + c("all green.", C_GREEN)))
    return 0


def _doctor(a) -> list[dict]:
    con = db.connect()
    checks = []

    def chk(name, ok, detail="", fix=""):
        """`fix` is what to DO about a red check — a check that cannot be acted on is noise."""
        checks.append({"check": name, "ok": bool(ok), "detail": detail, "fix": fix})

    n_items = con.execute("SELECT count(*) n FROM items WHERE status!='deleted'").fetchone()["n"]
    n_sess = con.execute("SELECT count(*) n FROM sessions").fetchone()["n"]
    chk("database", True, f"{db.db_path()} · {n_items} items · {n_sess} sessions")
    n_fts = con.execute("SELECT count(*) n FROM items_fts").fetchone()["n"]
    chk("fts index", n_fts == n_items, f"items_fts={n_fts} items={n_items}"
        + ("" if n_fts == n_items else " · run: tracker doctor --fix"))
    if a.fix and n_fts != n_items:
        db.rebuild_fts(con)
        chk("fts rebuilt", True, str(db.rebuild_fts(con)) + " rows")

    # hooks wired
    sp = Path.home() / ".claude/settings.json"
    wired, missing = False, []
    try:
        cfg = json.loads(sp.read_text())
        hooks = cfg.get("hooks", {})
        want = {"SessionStart": "session_start.py", "UserPromptSubmit": "user_prompt_submit.py",
                "PostToolUse": "post_tool_use.py", "Stop": "stop_gate.py",
                "SessionEnd": "session_end.py",
                # wired 2026-09-13 — see hooks/HOOK_CONTRACT.md §1a
                "SubagentStop": "subagent_stop.py", "PreCompact": "pre_compact.py",
                "Notification": "notification.py", "PermissionRequest": "permission_request.py"}
        for ev, script in want.items():
            blob = json.dumps(hooks.get(ev, []))
            if script not in blob:
                missing.append(ev)
        wired = not missing
    except Exception as e:
        missing = [f"settings.json unreadable: {e}"]
    chk("hooks wired in ~/.claude/settings.json", wired,
        f"all {len(want)} events" if wired else f"missing: {', '.join(missing)}")

    for h in ["hook_common.py", "session_start.py", "user_prompt_submit.py", "post_tool_use.py", "stop_gate.py",
              "session_end.py", "subagent_stop.py", "pre_compact.py", "notification.py",
              "permission_request.py"]:
        p = ROOT / "hooks" / h
        chk(f"hook {h}", p.exists(), str(p) if p.exists() else "missing")

    shim = (Path.home() / ".local/bin/tracker.cmd") if oscompat.IS_WIN else (Path.home() / ".local/bin/tracker")
    chk("shim " + str(shim), shim.exists() and (oscompat.IS_WIN or os.access(shim, os.X_OK)),
        str(shim) if shim.exists() else "missing")

    skill = Path.home() / ".claude/skills/log-build/SKILL.md"
    chk("skill log-build", skill.exists(), str(skill) if skill.exists() else "missing")
    try:
        chk("CLAUDE.md build-ledger rule", "Build ledger (mandatory)" in (Path.home() / ".claude/CLAUDE.md").read_text())
    except Exception:
        chk("CLAUDE.md build-ledger rule", False, "CLAUDE.md unreadable")

    plist = Path.home() / "Library/LaunchAgents/com.aicommandcenter.server.plist"
    loaded = False
    if plist.exists() and oscompat.has_launchd():
        import subprocess
        loaded = subprocess.call(["launchctl", "print", f"gui/{oscompat.uid()}/com.aicommandcenter.server"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
    if not oscompat.has_launchd():
        chk("server autostart", True, "no launchd here: START.bat, or python install.py --autostart")
    else:
      chk("launchd job", plist.exists() and loaded,
        ("loaded" if loaded else ("plist present, not loaded" if plist.exists() else "no plist")))

    port = int(db.config().get("port", 8790))
    health = _health_json(port)
    chk("server /health", bool(health), f"http://127.0.0.1:{port}" if health else f"not listening on {port}")
    # server.py imports modules at startup, so an edit on disk is not live until a
    # restart. Compare what the running process imported with what is on disk.
    if health is not None:
        stale = health.get("stale_sources")
        if stale is None:                       # server predates the check
            chk("server modules fresh", True, "this server does not report source mtimes yet")
        else:
            names = ", ".join(Path(x).name for x in stale[:6])
            chk("server modules fresh", not stale,
                "running the code on disk" if not stale
                else f"{len(stale)} file(s) edited since the server started ({names}) - run: tracker restart")
    chk("server.py present", (ROOT / "server.py").exists(), "missing" if not (ROOT / "server.py").exists() else "")
    chk("backfill.py present", (ROOT / "backfill.py").exists(), "missing" if not (ROOT / "backfill.py").exists() else "")
    chk("ui/index.html present", (ROOT / "ui/index.html").exists(), "missing" if not (ROOT / "ui/index.html").exists() else "")

    unlogged = con.execute(
        "SELECT count(*) n FROM sessions s WHERE s.source='live' AND s.logged=0 AND s.waived_at IS NULL "
        "AND s.ended_at IS NOT NULL AND NOT EXISTS(SELECT 1 FROM items i WHERE i.session_id=s.session_id) AND "
        "EXISTS(SELECT 1 FROM session_files f WHERE f.session_id=s.session_id AND f.ignored=0 AND f.logged_item_id IS NULL)"
    ).fetchone()["n"]
    chk("no unlogged live sessions", unlogged == 0,
        f"{unlogged} finished live session(s) never logged or waived - `tracker sessions --unlogged`"
        if unlogged else "")
    backlog = con.execute("SELECT count(*) n FROM items WHERE confidence='inferred' AND status='wip'").fetchone()["n"]
    chk("curation backlog (informational)", True, f"{backlog} auto/backfilled item(s) awaiting a human rewrite")

    # --- the daily reindex job: a silent stop means history starts expiring (idea 4) -------
    try:
        row = con.execute("SELECT value FROM meta WHERE key='reindex_last_run'").fetchone()
        last = json.loads(row["value"]) if row and row["value"] else None
    except Exception:
        last = None
    if not last:
        chk("reindex job has run", False, "no run on record",
            ("install it: python install.py --autostart · run once: python jobs/reindex.py" if oscompat.IS_WIN else "install and start it: python3 install.py --autostart · run once: python3 jobs/reindex.py"))
    else:
        import datetime as _dt
        try:
            t = _dt.datetime.strptime(last["finished_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=_dt.timezone.utc)
            age_h = (_dt.datetime.now(_dt.timezone.utc) - t).total_seconds() / 3600.0
        except Exception:
            age_h = None
        ok = age_h is not None and age_h < 36
        chk("reindex job has run", ok,
            f"last finished {last.get('finished_at', '?')[:19]}Z"
            + (f" ({age_h:.1f} h ago)" if age_h is not None else "")
            + f", {last.get('transcripts_on_disk', '?')} transcripts on disk",
            "" if ok else "Claude deletes transcripts after ~30 days; a stopped reindex job means "
                          "history is expiring unrecorded. Check: launchctl print "
                          "gui/$(id -u)/com.aicommandcenter.reindex · "
                          "reinstall: python3 install.py --autostart")

    # --- self-lint of our own Claude surface -------------------
    # SKILL.md frontmatter, CLAUDE.md size, hook commands that resolve, a tamper hash of the
    # hooks block, and the launchd KeepAlive shape that once left the server dead.
    try:
        from modules import config as _cfgmod
        for row in _cfgmod.lint_claude_surface(fix=bool(getattr(a, "fix", False))):
            checks.append(row)
    except Exception as exc:
        chk("self-lint of the Claude surface", False,
            f"lint failed: {type(exc).__name__}: {exc}",
            "see modules/config.py::lint_claude_surface")

    return checks


def cmd_gate_check(a):
    con = db.connect()
    sid = resolve_session(con, a.session, create=False)
    if not sid:
        out_json({"ok": True, "would_block": False, "why": "no session"})
        return 0
    gs = db.gate_state(con, sid)
    if a.json or True:
        gs["reason"] = db.gate_reason([p["path"] for p in gs["pending"]]) if gs["would_block"] else None
        out_json({"ok": True, **gs})
    return 0


# --------------------------------------------------------------------------- Command Center wrappers
def _mod(name):
    """Lazy-import a Command Center module so the core CLI stays fast."""
    try:
        return __import__(f"modules.{name}", fromlist=["ROUTES"])
    except Exception as e:
        die(f"error: modules/{name}.py is not available ({type(e).__name__}: {e})", 1)


def cmd_live(a):
    m = _mod("live")
    if a.recent:
        res = m.get_recent({"hours": str(a.recent)})[1]
        if a.json:
            out_json(res); return 0
        if not res["sessions"]:
            print(f"no sessions in the last {a.recent}h."); return 0
        print(c(f"sessions active in the last {res['hours']}h:", C_DIM))
        for s in res["sessions"]:
            tag = c(" LIVE", C_GREEN) if s["live"] else ""
            flag = "" if s["logged"] else (c(" unlogged", C_YELLOW) if not s["waived"] else "")
            print(f"{c(s['session_id'][:8], C_CYAN)} {s['minutes_ago']:>7.0f}m ago  {(s['project'] or '-'):<16} "
                  f"{(s['title'] or '')[:52]}{tag}{flag}")
            print(f"         {c(s['resume_cmd'], C_DIM)}")
        return 0
    res = m.get_sessions()[1]
    if a.json:
        out_json(res); return 0
    if not res["sessions"]:
        print("no Claude Code sessions running."); return 0
    print(c(f"{res['count']} Claude Code session(s) running:", C_DIM))
    for s in res["sessions"]:
        dot = {"active": c("*", C_GREEN), "thinking": c("+", C_YELLOW)}.get(s["pulse"], c(".", C_DIM))
        idle = f"{s['idle_s']:.0f}s idle" if s["idle_s"] is not None else "?"
        print(f" {dot} pid {s['pid']:<7} {(s['project'] or '-'):<14} {s['elapsed']:>12}  {idle:<12} "
              f"{(s['title'] or s['session_id'] or '?')[:44]}")
        if s["pending_files"]:
            print(c(f"      {s['pending_files']} unlogged file touch(es), {s['logged_items']} item(s) logged", C_YELLOW))
    return 0


def cmd_jobs(a):
    m = _mod("jobs")
    import re as _re
    if a.label and a.action:
        res = m.post_action(None, {"action": a.action}, _re.match(r"(?P<label>.+)", a.label))[1]
        (out_json(res) if a.json else print(c(f"{a.action} {a.label}", C_GREEN) + f": {res['state']}"))
        return 0
    if a.label:
        res = m.get_job({"lines": str(a.lines)}, None, _re.match(r"(?P<label>.+)", a.label))[1]
        j = res["job"]
        if a.json:
            out_json(res); return 0
        print(f"{c(j['label'], C_BOLD)}  {j['schedule']}")
        print(f"  {j['program_str']}")
        print(f"  loaded={j.get('loaded')} pid={j.get('pid')} runs={j.get('runs')} last_exit={j.get('last_exit')}")
        if j.get("next_fire"):
            print(f"  next fire {j['next_fire']}")
        for stream in ("stdout_tail", "stderr_tail"):
            if j.get(stream):
                print(c(f"  --- {stream} ---", C_DIM))
                for line in j[stream][-a.lines:]:
                    print(f"    {line[:200]}")
        return 0
    res = m.get_jobs({"logs": "0"})[1]
    if a.json:
        out_json(res); return 0
    st = res.get("by_status", {})
    print(c(f"{res['count']} launchd agent(s): "
            + ", ".join(f"{n} {k}" for k, n in sorted(st.items(), key=lambda kv: -kv[1])), C_DIM))
    colour = {"healthy": C_GREEN, "stale": C_YELLOW, "failing": C_RED}
    for j in res["jobs"]:
        status = j.get("status", "unknown")
        mark = c(f"{status:<10}", colour.get(status, C_DIM))
        age = j.get("last_activity_age_s")
        when = (f"{age // 60} min ago" if age is not None and age < 86400 else
                f"{age // 3600} h ago" if age is not None else "no evidence")
        print(f" {mark} {j['label']:<36} {j['schedule'][:34]:<34} {when:<14} {str(j.get('evidence') or '')[:26]}")
    if res.get("failing") or res.get("stale"):
        print(c(f"stale: {', '.join(res.get('stale') or []) or 'none'}", C_YELLOW))
        print(c(f"failing: {', '.join(res.get('failing') or []) or 'none'}", C_RED))
    print(c(res.get("note", ""), C_DIM))
    return 0


def cmd_usage(a):
    m = _mod("usage")
    res = m.get_summary({"days": str(a.days)}, None, None)[1]
    if a.json:
        out_json(res); return 0
    t = res["totals"]
    print(c(f"last {res['days']} days", C_DIM) + f"  {t['api_calls']:,} api calls · "
          f"{(t['input_tokens'] + t['output_tokens']):,} in+out tokens · "
          f"{t['cache_read']:,} cache read · est ${t['cost_usd_est']:,.2f}"
          + (f" (recorded ${t['cost_usd_recorded']:,.2f} over {t['sessions_with_recorded_cost']} session(s))"
             if t.get("cost_usd_recorded") else ""))
    for label, w in (res.get("windows") or {}).items():
        fam = " · ".join(f"{f['family']} {f['tokens']:,}tok ${f['cost_usd_est']:,.2f}"
                         for f in (w.get("by_family") or []))
        print(f"{c(label.replace('_', ' '), C_DIM):<14} {w.get('tokens', 0):>15,} tokens  "
              f"est ${w.get('cost_usd_est', 0):>8,.2f}  {w.get('calls', 0):>5} calls   {fam}")
    days: dict[str, float] = {}
    for r in res.get("by_day", []):
        days[r["day"]] = days.get(r["day"], 0) + (r.get("cost_usd_est") or 0)
    if days:
        top = max(days.values()) or 1
        blocks = " ▁▂▃▄▅▆▇█"
        print(c("daily est cost", C_DIM) + "         "
              + "".join(blocks[min(8, int(round(v / top * 8)))] for _d, v in sorted(days.items()))
              + f"   ${min(days.values()):,.0f}-${max(days.values()):,.0f}/day")
    print(c("by project", C_DIM))
    for r in (res.get("by_project") or [])[:8]:
        print(f"  {r['project'][:22]:<22} {r['sessions']:>3} session(s)  {r['tokens']:>15,} tokens  "
              f"est ${r['cost_usd_est']:,.2f}")
    print(c("most expensive sessions", C_DIM))
    for r in (res.get("top_sessions") or [])[:5]:
        print(f"  {r['session_id'][:8]} {r['date']}  est ${r['cost_usd_est']:>8,.2f}  {(r['title'] or '')[:52]}")
    cal = res.get("calibration") or {}
    if cal.get("est_over_recorded"):
        print(c(f"(estimate runs {cal['est_over_recorded']}x recorded cost over "
                f"{cal.get('sessions_compared')} compared session(s))", C_DIM))
    return 0


def cmd_projects(a):
    m = _mod("launch")
    if a.path:
        res = m.get_project({"path": a.path})[1]
        out_json(res) if a.json else print(json.dumps(res["project"], indent=2, default=str))
        return 0
    res = m.get_projects()[1]
    if a.json:
        out_json(res); return 0
    print(c(f"{res['count']} project(s) under ~/Desktop:", C_DIM))
    for p in res["projects"]:
        g = p["git"] or {}
        git = f" git:{g['branch']}" + (f"+{g['dirty']}" if g.get("dirty") else "") if g.get("branch") else ""
        last = (p["last_item"] or {}).get("ts", "")[:10]
        print(f"  {p['name'][:24]:<24} {p['items']:>3} item(s)  last {last or '-':<10} "
              f"{len(p['jobs'])} job(s) {len(p['commands'])} cmd(s){git}")
    return 0


def cmd_handoff(a):
    m = _mod("handoff")
    con = db.connect()
    project = a.project or db.derive_project(db.norm_path(a.cwd or os.getcwd()))
    md = m.render_handoff(con, project)
    written = None
    if a.write:
        d = m.project_dir(con, project)
        if not d:
            die(f"error: cannot find a directory for project {project} "
                "(no session cwd, no item files, no ~/Desktop/<project>)")
        target = d / "HANDOFF_AUTO.md"
        target.write_text(md)
        written = str(target)
        sid = resolve_session(con, a.session)
        create_item(con, sid, {
            "title": f"Auto handoff for {project}",
            "summary": (f"Generated state-of-project page for {project}: active items by kind with their entry "
                        f"paths and usage lines, work in progress, the last five sessions with recaps and resume "
                        f"commands, and the ledger gaps. Regenerated by `tracker handoff --write`."),
            "usage": f"tracker handoff --project {project} --write  (writes {written}; never touches a human HANDOFF.md)",
            "kind": "doc", "project": project,
            "files": [{"path": written, "role": "created"}],
            "tags": ["handoff", "generated"],
        }, str(d), quiet=True)
    if a.json:
        out_json({"ok": True, "project": project, "chars": len(md), "written": written, "markdown": md})
    else:
        print(md)
        if written:
            print(c(f"\nwritten to {written} (logged as a doc item)", C_GREEN), file=sys.stderr)
    return 0


def cmd_digest(a):
    m = _mod("handoff")
    con = db.connect()
    md = m.render_digest(con, a.days)
    written = None
    if a.write:
        d = ROOT / "digests"
        d.mkdir(exist_ok=True)
        target = d / f"{m.iso_week()}.md"
        target.write_text(md)
        written = str(target)
        if a.write:
            sid = resolve_session(con, a.session)
            create_item(con, sid, {
                "title": f"Weekly build digest {m.iso_week()}",
                "summary": (f"Digest for {m.iso_week()}: everything built this week grouped by project, estimated "
                            f"cost per project, ledger hygiene (sessions that changed files without logging) and "
                            f"the plan-limit samples recorded during the week."),
                "usage": f"tracker digest --write (writes {written}); schedule it weekly with cron or Task Scheduler",
                "kind": "doc", "project": "ai-command-center",
                "files": [{"path": written, "role": "created"}],
                "tags": ["digest", "generated"],
            }, str(ROOT), quiet=True)
    if a.json:
        out_json({"ok": True, "week": m.iso_week(), "days": a.days, "written": written, "markdown": md})
    else:
        print(md)
        if written:
            print(c(f"\nwritten to {written}", C_GREEN), file=sys.stderr)
    return 0


def cmd_restart(a):
    """server.py imports modules/*.py at startup, so an edit is not live until the
    process restarts. This is the one command that makes it live."""
    import subprocess
    port = int(db.config().get("port", 8790))
    label = "com.aicommandcenter.server"
    target = f"gui/{oscompat.uid()}/{label}"
    loaded = oscompat.has_launchd() and subprocess.call(["launchctl", "print", target],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
    how, rc = "launchctl kickstart -k", 0
    if loaded:
        rc = subprocess.call(["launchctl", "kickstart", "-k", target],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        how = "detached python server.py"
        if oscompat.IS_WIN:  # the old server holds the port; stop it first
            _stop_server_on(port)
        log = ROOT / "logs" / "server.out"
        log.parent.mkdir(exist_ok=True)
        with open(log, "ab") as fh:
            subprocess.Popen([sys.executable, str(ROOT / "server.py")],
                             stdout=fh, stderr=fh, cwd=str(ROOT), **oscompat.detached_kwargs())
    health, waited = None, 0.0
    while waited < 8.0:
        health = _health_json(port, timeout=1.0)
        if health:
            break
        time.sleep(0.25)
        waited += 0.25
    stale = (health or {}).get("stale_sources") or []
    ok = bool(health) and not stale
    if a.json:
        out_json({"ok": ok, "how": how, "launchd_loaded": loaded, "rc": rc,
                  "waited_s": round(waited, 2), "port": port,
                  "modules": (health or {}).get("modules"), "stale_sources": stale})
    else:
        if not health:
            print(c(f"server did not come back on {port} within {waited:.0f}s ({how})", C_RED), file=sys.stderr)
            return 1
        print(c(f"server restarted via {how}", C_GREEN)
              + f" · pid {health.get('pid')} · {len(health.get('modules') or [])} modules · {waited:.1f}s")
        if stale:
            print(c(f"still stale: {', '.join(Path(x).name for x in stale)}", C_YELLOW), file=sys.stderr)
    return 0 if ok else 1


def cmd_backup(a):
    """sqlite online backup - safe while the server and hooks are writing."""
    import sqlite3
    src = db.db_path()
    if not src.exists():
        die(f"error: no database at {src}", 2)
    outdir = ROOT / "backups"
    outdir.mkdir(exist_ok=True)
    target = outdir / f"tracker-{time.strftime('%Y%m%d-%H%M%S')}.db"
    con = db.connect()
    dest = sqlite3.connect(str(target))
    try:
        con.backup(dest)
    finally:
        dest.close()
    keep = max(1, a.keep)
    existing = sorted(outdir.glob("tracker-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for old in existing[keep:]:
        try:
            old.unlink()
            removed.append(old.name)
        except OSError:
            pass
    size = target.stat().st_size
    items = db.connect(target).execute("SELECT count(*) n FROM items").fetchone()["n"]
    if a.json:
        out_json({"ok": True, "backup": str(target), "bytes": size, "items": items,
                  "kept": keep, "removed": removed})
    else:
        print(c(f"backed up {items} items to {target}", C_GREEN) + f" ({size / 1e6:.1f} MB)")
        if removed:
            print(c(f"pruned {len(removed)} old backup(s), keeping {keep}", C_DIM))
    return 0


# --------------------------------------------------------------------------- argparse
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tracker",
        description="AI Command Center - the ledger of everything built on this machine by Claude Code sessions.",
        epilog="Protocol: log one item per distinct thing you built, as you go. See skill log-build.")
    p.add_argument("--version", action="version", version="ai-command-center 1.0")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    def jsonflag(sp):
        sp.add_argument("--json", action="store_true", help="machine-readable JSON output")
        return sp

    sp = sub.add_parser("log", help="record something you built or changed")
    sp.add_argument("--title", help="the NAME of the thing (8-140 chars), not the task")
    sp.add_argument("--summary", help="what it is AND why it exists (>=60 chars)")
    sp.add_argument("--usage", help="how to run/open it and where it lives")
    sp.add_argument("--kind", default="build", choices=KINDS)
    sp.add_argument("--project")
    sp.add_argument("--file", action="append", metavar="PATH[:role]",
                    help=f"file that constitutes the thing; role in {'|'.join(ROLES)}")
    sp.add_argument("--link", action="append", metavar="URL[|label]")
    sp.add_argument("--tag", action="append")
    sp.add_argument("--supersedes", help="id of the item this replaces")
    sp.add_argument("--status", default="active", choices=STATUSES)
    sp.add_argument("--session")
    sp.add_argument("--agent", help="which AI built it: claude (default), codex, gemini, cursor, ... ($TRACKER_AGENT)")
    sp.add_argument("--json-in", dest="json_in", metavar="FILE|-",
                    help='{"items":[{title,summary,usage,kind,files:[{path,role}],links:[],tags:[]}]}')
    jsonflag(sp).set_defaults(fn=cmd_log)

    sp = sub.add_parser("waive", help="declare that this session built nothing durable")
    sp.add_argument("--reason", required=True, help=">=20 chars, why there is nothing to log")
    sp.add_argument("--session")
    jsonflag(sp).set_defaults(fn=cmd_waive)

    sp = sub.add_parser("status", help="this session: counters, pending files, gate verdict")
    sp.add_argument("--session")
    jsonflag(sp).set_defaults(fn=cmd_status)

    sp = sub.add_parser("recent", help="what was built in this directory/project before")
    sp.add_argument("--cwd")
    sp.add_argument("--project")
    sp.add_argument("--limit", type=int, default=15)
    jsonflag(sp).set_defaults(fn=cmd_recent)

    sp = sub.add_parser("list", help="browse the ledger")
    sp.add_argument("--project"); sp.add_argument("--kind", choices=KINDS)
    sp.add_argument("--since", metavar="YYYY-MM-DD"); sp.add_argument("--status", choices=STATUSES)
    sp.add_argument("--confidence", choices=CONFIDENCES); sp.add_argument("--limit", type=int, default=30)
    jsonflag(sp).set_defaults(fn=cmd_list)

    sp = sub.add_parser("search", help="full-text search the ledger")
    sp.add_argument("query"); sp.add_argument("--limit", type=int, default=20)
    jsonflag(sp).set_defaults(fn=cmd_search)

    sp = sub.add_parser("show", help="everything about one item")
    sp.add_argument("id", type=int)
    jsonflag(sp).set_defaults(fn=cmd_show)

    sp = sub.add_parser("edit", help="curate an item")
    sp.add_argument("id", type=int)
    sp.add_argument("--title"); sp.add_argument("--summary"); sp.add_argument("--usage")
    sp.add_argument("--status", choices=STATUSES); sp.add_argument("--kind", choices=KINDS)
    sp.add_argument("--confidence", choices=CONFIDENCES)
    sp.add_argument("--add-file", action="append", metavar="PATH[:role]")
    sp.add_argument("--rm-file", action="append", metavar="PATH")
    sp.add_argument("--add-link", action="append", metavar="URL[|label]")
    sp.add_argument("--add-tag", action="append"); sp.add_argument("--rm-tag", action="append")
    jsonflag(sp).set_defaults(fn=cmd_edit)

    sp = sub.add_parser("dismiss", help="drop phantom/irrelevant pending files from the gate (no item created)")
    sp.add_argument("paths", nargs="*", metavar="PATH", help="paths or globs to dismiss")
    sp.add_argument("--missing", action="store_true", help="dismiss every pending path that does not exist on disk")
    sp.add_argument("--reason", required=True, help=">=20 chars, why these are not real work")
    sp.add_argument("--force", action="store_true", help="dismiss even files that exist and look durable")
    sp.add_argument("--session")
    jsonflag(sp).set_defaults(fn=cmd_dismiss)

    sp = sub.add_parser("sessions", help="audit sessions (which ones never logged anything)")
    sp.add_argument("--unlogged", action="store_true"); sp.add_argument("--since", metavar="YYYY-MM-DD")
    sp.add_argument("--project"); sp.add_argument("--limit", type=int, default=40)
    jsonflag(sp).set_defaults(fn=cmd_sessions)

    sp = sub.add_parser("backfill", help="mine ~/.claude/projects transcripts into the ledger")
    sp.add_argument("--since", metavar="YYYY-MM-DD"); sp.add_argument("--limit", type=int)
    sp.add_argument("--dry-run", action="store_true"); sp.add_argument("--force", action="store_true")
    sp.add_argument("--summarize", action="store_true", help="use claude -p haiku for summaries (costs money)")
    jsonflag(sp).set_defaults(fn=None)

    sp = sub.add_parser("serve", help="run the local web app in the foreground")
    sp.add_argument("--port", type=int, default=int(db.config().get("port", 8790)))
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser("open", help="ensure the server is up and open the app")
    sp.add_argument("--port", type=int)
    jsonflag(sp).set_defaults(fn=cmd_open)

    sp = sub.add_parser("export", help="markdown (or JSON) digest of the ledger")
    sp.add_argument("--md", action="store_true", default=True, help="markdown (default)")
    sp.add_argument("--json-out", "--json-export", dest="json_out", action="store_true")
    sp.add_argument("--since", metavar="YYYY-MM-DD"); sp.add_argument("--project")
    sp.add_argument("--limit", type=int, default=500)
    jsonflag(sp).set_defaults(fn=cmd_export)

    sp = sub.add_parser("live", help="Claude Code sessions running on this Mac right now")
    sp.add_argument("--recent", type=int, metavar="HOURS", help="recently active sessions + resume commands")
    jsonflag(sp).set_defaults(fn=cmd_live)

    sp = sub.add_parser("jobs", help="launchd agents: state, cadence, logs, actions")
    sp.add_argument("--label", help="one job by label")
    sp.add_argument("--action", choices=["start", "stop", "kickstart", "load", "unload"],
                    help="act on --label")
    sp.add_argument("--lines", type=int, default=20, help="log tail length for --label")
    jsonflag(sp).set_defaults(fn=cmd_jobs)

    sp = sub.add_parser("usage", help="token and cost summary from the transcripts")
    sp.add_argument("--days", type=int, default=30)
    jsonflag(sp).set_defaults(fn=cmd_usage)

    sp = sub.add_parser("projects", help="projects under ~/Desktop with docs, git, jobs, ledger activity")
    sp.add_argument("--path", help="one project directory")
    jsonflag(sp).set_defaults(fn=cmd_projects)

    sp = sub.add_parser("handoff", help='render (and optionally write) the "State of <project>" page')
    sp.add_argument("--project"); sp.add_argument("--cwd")
    sp.add_argument("--write", action="store_true", help="save to <project>/HANDOFF_AUTO.md and log it")
    sp.add_argument("--session")
    jsonflag(sp).set_defaults(fn=cmd_handoff)

    sp = sub.add_parser("digest", help="weekly digest of everything built, cost and ledger hygiene")
    sp.add_argument("--days", type=int, default=7)
    sp.add_argument("--write", action="store_true", help="save to digests/<ISO-week>.md and log it")
    sp.add_argument("--session")
    jsonflag(sp).set_defaults(fn=cmd_digest)

    sp = sub.add_parser("restart", help="restart the local server so edited modules/server.py go live")
    jsonflag(sp).set_defaults(fn=cmd_restart)

    sp = sub.add_parser("backup", help="online sqlite backup of tracker.db into backups/")
    sp.add_argument("--keep", type=int, default=10)
    jsonflag(sp).set_defaults(fn=cmd_backup)

    sp = sub.add_parser("agents", help="AI tools (Claude, Codex, Gemini, ...): status, connect, scan, new",
                        add_help=False)
    sp.add_argument("rest", nargs=argparse.REMAINDER)
    sp = sub.add_parser("doctor", help="check the whole installation")
    sp.add_argument("--fix", action="store_true", help="rebuild the FTS index if it drifted")
    sp.add_argument("--full", action="store_true",
                    help="also run tests/run_all.sh — every suite, the doctor checks and an "
                         "HTTP smoke of every route (about a minute)")
    jsonflag(sp).set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("gate-check", help="what the Stop gate would do for a session (JSON)")
    sp.add_argument("--session")
    jsonflag(sp).set_defaults(fn=cmd_gate_check)
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        parser.print_help()
        return 0
    if argv[0] == "backfill":
        a, extra = parser.parse_known_args(argv)
        return cmd_backfill(a, extra)
    if argv[0] == "agents":
        import agents
        return agents.cli(argv[1:])
    a = parser.parse_args(argv)
    if not getattr(a, "fn", None):
        parser.print_help()
        return 0
    try:
        return a.fn(a) or 0
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130


# --------------------------------------------------------------------------------------
# `tracker limits` and `tracker usage --full`.
# Deliberately a wrapper around main() rather than an edit to build_parser(), so this cannot
# collide with the rest of tracker.py. Both delegate to modules/usage.py:cli_main().
# --------------------------------------------------------------------------------------
_main_without_limits = main


def main(argv=None) -> int:                                      # noqa: F811
    argv = list(sys.argv[1:] if argv is None else argv)
    deep = {"--full", "--analysis", "--deep"}
    if argv and argv[0] == "limits":
        from modules import usage as _usage
        return _usage.cli_main(argv)
    if argv and argv[0] == "usage" and deep & set(argv):
        from modules import usage as _usage
        return _usage.cli_main([a for a in argv if a not in deep])
    return _main_without_limits(argv)


if __name__ == "__main__":
    sys.exit(main())
