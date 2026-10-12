#!/usr/bin/env python3
"""Shared foundation for AI Command Center: DB connection, migrations, project derivation,
ignore-glob logic, FTS maintenance and the Bash file-write parser. Stdlib only.
Every other module (tracker.py, backfill.py, server.py, hooks/*) imports from here."""
from __future__ import annotations
import json, os, re, sqlite3, fnmatch, datetime, hashlib, shlex, threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("CLAUDE_TRACKER_DB") or ROOT / "tracker.db")
SCHEMA_PATH = ROOT / "schema.sql"
CONFIG_PATH = ROOT / "config.json"
LOG_DIR = ROOT / "logs"
HOME = Path.home()
DESKTOP = HOME / "Desktop"
import oscompat  # noqa: E402
_HOME_S = oscompat.posix(HOME)

def _desktop_s() -> str:
    """config.json `desktop` wins (Windows OneDrive Desktops live elsewhere)."""
    d = config().get("desktop")
    return oscompat.posix(os.path.normpath(os.path.expanduser(d))) if d else oscompat.posix(DESKTOP)

_config_cache = None
def config() -> dict:
    global _config_cache
    if _config_cache is None:
        try:
            src = CONFIG_PATH if CONFIG_PATH.exists() else ROOT / "config.default.json"
            _config_cache = json.loads(src.read_text(encoding="utf-8"))
        except Exception:
            _config_cache = {"port": 8790, "ignore_globs": [], "gate": {}, "substantive": {}}
    return _config_cache

def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def db_path() -> Path:
    """Active DB path. $CLAUDE_TRACKER_DB overrides the default (used by tests)."""
    env = os.environ.get("CLAUDE_TRACKER_DB")
    return Path(env).expanduser() if env else DB_PATH

_pool = threading.local()

# Pooling is OFF unless a long-running process turns it on. The server sets POOL = True at
# startup because it is the only process where per-request connections accumulate; the CLI,
# hooks, tests and backfill are short-lived and expect an independent connection they may close
# without affecting anyone else's handle.
POOL = False


def _open(p: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(p), timeout=5, isolation_level=None)  # autocommit; use explicit BEGIN when needed
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    migrate(con)
    return con


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Return this thread's connection for `path`, opening one the first time.

    Every module handler used to call this per request and never close the result, so a long-
    running server accumulated one db + one -wal handle per request until it passed the 256
    descriptor limit and sqlite started reporting "unable to open database file" on every route.
    Pooling per thread fixes all ~20 call sites at once without touching any of them, but only
    where it is wanted: see POOL above. A caller that closes a pooled connection is still safe --
    the liveness probe below notices and reopens. Connections are never shared across threads,
    which sqlite forbids.
    """
    p = Path(path) if path else db_path()
    if not POOL:
        return _open(p)          # default: an independent connection, as every caller expects
    key = str(p)
    cache = getattr(_pool, "conns", None)
    if cache is None:
        cache = {}
        _pool.conns = cache
    con = cache.get(key)
    if con is not None:
        try:
            con.execute("SELECT 1")
            return con
        except sqlite3.Error:
            cache.pop(key, None)
    con = _open(p)
    cache[key] = con
    return con


def close_thread_connections() -> int:
    """Close every connection this thread pooled. The server calls this when a TCP connection
    ends, so a finished request thread never leaves handles behind."""
    cache = getattr(_pool, "conns", None) or {}
    n = 0
    for con in list(cache.values()):
        try:
            con.close()
            n += 1
        except Exception:
            pass
    cache.clear()
    return n

# contentless_delete arrived in SQLite 3.43 (2023). Older builds get an ordinary FTS5 table,
# which stores its own copy of the text but supports the same DELETE-then-INSERT re-index.
FTS_CONTENTLESS = sqlite3.sqlite_version_info >= (3, 43, 0)
FTS_OPTS = "content='', contentless_delete=1, tokenize='porter unicode61'" if FTS_CONTENTLESS \
    else "tokenize='porter unicode61'"


def migrate(con: sqlite3.Connection) -> None:
    # Columns added after the first release. CREATE TABLE IF NOT EXISTS never alters an existing
    # table, so older databases get them here, before the schema's indexes refer to them.
    for table in ("sessions", "items"):
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if cols and "agent" not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN agent TEXT NOT NULL DEFAULT 'claude'")
    con.executescript(SCHEMA_PATH.read_text().replace(
        "content='', contentless_delete=1, tokenize='porter unicode61'", FTS_OPTS))
    con.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version','1')")
    ensure_fts_deletable(con)  #: old DBs get a deletable FTS index

# ---------- project + path helpers ----------
def norm_path(p: str, cwd: str | None = None) -> str:
    p = os.path.expanduser(p.strip().strip('"').strip("'"))
    if oscompat.IS_WIN and re.match(r"^/[a-zA-Z]/", p):   # Git Bash: /c/Users/x -> C:/Users/x
        p = p[1].upper() + ":" + p[2:]
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    p = oscompat.posix(os.path.normpath(p))
    return p

def derive_project(path_or_cwd: str | None) -> str:
    if not path_or_cwd:
        return "unknown"
    p = oscompat.posix(os.path.normpath(os.path.expanduser(path_or_cwd)))
    d = _desktop_s()
    if p == d:
        return "Desktop"
    if p.startswith(d + "/"):
        rest = p[len(d) + 1:].split("/")
        # a file directly on the Desktop belongs to 'Desktop'; a dir under Desktop is the project
        if len(rest) == 1 and (os.path.isfile(p) or ("." in rest[0] and not os.path.isdir(p))):
            return "Desktop"
        return rest[0]
    if p.startswith(_HOME_S + "/.claude"):
        return "claude-config"
    if p == _HOME_S:
        return "home"
    if p.startswith(_HOME_S + "/"):
        rest = p[len(_HOME_S) + 1:].split("/")
        return rest[0] if len(rest) == 1 else "/".join(rest[:2])
    parts = [x for x in p.split("/") if x]
    return "/".join(parts[-2:]) if len(parts) >= 2 else (parts[0] if parts else "unknown")

def is_ignored(path: str) -> bool:
    # On Windows a "/tmp/x" written by a shell becomes "C:/tmp/x": test the drive-less form too.
    forms = [path, path[2:]] if oscompat.IS_WIN and path[1:2] == ":" else [path]
    for g in config().get("ignore_globs", []):
        g2 = os.path.expanduser(g)
        if any(fnmatch.fnmatch(p, g2) for p in forms):
            return True
    return False

# ---------- Bash command → written file paths ----------
_REDIR = re.compile(r"(?<![<>&\d=\-!])>{1,2}\s*(?!&)(?:'([^']+)'|\"([^\"]+)\"|((?:\\.|[^\s;&|)])+))")
_TEE = re.compile(r"\btee\s+(?:-a\s+|-ai\s+|-ia\s+|-i\s+)?(?:'([^']+)'|\"([^\"]+)\"|((?:\\.|[^\s;&|)])+))")
_OPEN = re.compile(r"open\(\s*(?:r?['\"]([^'\"]+)['\"]|os\.path\.expanduser\(['\"]([^'\"]+)['\"]\)),\s*['\"][wa]")
_PATHWRITE = re.compile(r"Path\(\s*['\"]([^'\"]+)['\"]\s*\)(?:\.expanduser\(\))?\.write_(?:text|bytes)")
_CPMV = re.compile(r"\b(?:cp|mv|install)\s+(?:-\w+\s+)*(?:\S+\s+)+?(?:'([^']+)'|\"([^\"]+)\"|((?:\\.|\S)+))\s*(?:;|&&|\||$)", re.M)
_TOUCH = re.compile(r"\btouch\s+((?:(?:'[^']+'|\"[^\"]+\"|(?:\\.|[^\s;&|])+)\s*)+)")
_LN = re.compile(r"\bln\s+-s\w*\s+\S+\s+(?:'([^']+)'|\"([^\"]+)\"|((?:\\.|\S)+))")
_SED_I = re.compile(r"\bsed\s+-i(?:\s*''|\s*\"\")?\s+(?:-e\s+)?(?:'[^']*'|\"[^\"]*\"|\S+)\s+((?:(?:'[^']+'|\"[^\"]+\"|(?:\\.|[^\s;&|])+)\s*)+)")
_DEVNULL = {"/dev/null", "/dev/stderr", "/dev/stdout"}

_CD = re.compile(r"(?:^|[;&|]\s*|\n\s*)\s*(?:cd|pushd)\s+(?:'([^']+)'|\"([^\"]+)\"|((?:\\.|[^\s;&|])+))")

def _unesc(raw: str | None) -> str | None:
    """Resolve backslash escapes in an UNQUOTED shell token: `My\\ Apps` -> `My Apps`.
    Without it `cd ~/Desktop/My\\ Apps/Demo && cat > Sources/Main.swift` harvested the phantom
    `/Users/you/Desktop/My\\/Main.swift` and the gate blocked a handback for a file that was
    already logged (2026-09-14). Only the unquoted alternative (group 3) needs it - a quoted
    capture is already literal."""
    return re.sub(r"\\(.)", r"\1", raw) if raw else raw

def _cd_chain(command: str, cwd: str | None) -> list[tuple[int, str]]:
    """[(char_offset, dir_in_effect_after_this_cd)] so a relative path can be resolved against the
    directory the command had actually cd'd into by that point (`cd ~/x && cat > y.md`)."""
    chain: list[tuple[int, str]] = []
    cur = cwd or os.getcwd()
    for m in _CD.finditer(command):
        raw = (m.group(1) or m.group(2) or _unesc(m.group(3)) or "").strip()
        if not raw or raw.startswith("-") or raw.startswith("$") or "`" in raw:
            continue
        cur = norm_path(raw, cur)
        chain.append((m.end(), cur))
    return chain

def bash_written_paths(command: str, cwd: str | None = None) -> list[tuple[str, str]]:
    """Return [(abs_path, op)] for files a shell command probably writes. Best effort, conservative.
    Relative paths resolve against the directory in effect at that point in the command, i.e. a
    leading `cd DIR &&` is honoured."""
    out: dict[str, str] = {}
    chain = _cd_chain(command, cwd)

    def base_at(pos: int) -> str:
        base = cwd or os.getcwd()
        for off, d in chain:
            if off <= pos:
                base = d
            else:
                break
        return base

    def add(raw: str | None, op: str, pos: int = 0):
        if not raw:
            return
        raw = raw.strip()
        if raw in _DEVNULL or raw.startswith("/dev/") or raw.startswith("$") or raw.startswith("-"):
            return
        if raw.endswith("/") or "*" in raw or "`" in raw or "$(" in raw:
            return
        p = norm_path(raw, base_at(pos))
        out.setdefault(p, op)
    for m in _REDIR.finditer(command):
        add(m.group(1) or m.group(2) or _unesc(m.group(3)), "create", m.start())
    for m in _TEE.finditer(command):
        add(m.group(1) or m.group(2) or _unesc(m.group(3)), "create", m.start())
    for m in _OPEN.finditer(command):
        add(m.group(1) or m.group(2), "create", m.start())
    for m in _PATHWRITE.finditer(command):
        add(m.group(1), "create", m.start())
    for m in _CPMV.finditer(command):
        dest = m.group(1) or m.group(2) or _unesc(m.group(3))
        argv0 = command[m.start():].split()[0] if command[m.start():].split() else ""
        if dest and dest != argv0:  # `/usr/libexec/PlistBuddy -c … x.plist` is not a cp destination
            add(dest, "create", m.start())
    for m in _TOUCH.finditer(command):
        for tok in shlex.split(m.group(1), posix=True) if _safe_shlex(m.group(1)) else m.group(1).split():
            add(tok, "create", m.start())
    for m in _LN.finditer(command):
        add(m.group(1) or m.group(2) or _unesc(m.group(3)), "create", m.start())
    for m in _SED_I.finditer(command):
        for tok in (shlex.split(m.group(1)) if _safe_shlex(m.group(1)) else m.group(1).split()):
            if not tok.startswith("-"):
                add(tok, "modify", m.start())
    return [(p, op) for p, op in out.items() if not is_ignored(p) and plausible_path(p)]

def _safe_shlex(s: str) -> bool:
    try:
        shlex.split(s); return True
    except ValueError:
        return False

# ---------- sessions / files / items helpers ----------
def upsert_session(con, session_id: str, **fields) -> None:
    fields = {k: v for k, v in fields.items() if v is not None}
    if "cwd" in fields and "project" not in fields:
        fields["project"] = derive_project(fields["cwd"])
    cols = ", ".join(fields.keys())
    qs = ", ".join("?" for _ in fields)
    sets = ", ".join(f"{k}=COALESCE(excluded.{k}, sessions.{k})" if k in ("title","first_prompt","model","cwd","project","started_at","transcript_path","git_branch","cc_version") else f"{k}=excluded.{k}" for k in fields)
    con.execute(
        f"INSERT INTO sessions(session_id{',' if cols else ''}{cols}) VALUES(?{',' if qs else ''}{qs}) "
        f"ON CONFLICT(session_id) DO UPDATE SET {sets + ', ' if sets else ''}updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
        [session_id, *fields.values()],
    )

def touch_file(con, session_id: str, path: str, op: str = "modify", tool: str | None = None, ts: str | None = None) -> None:
    ts = ts or now_iso()
    ign = 1 if is_ignored(path) else 0
    con.execute(
        "INSERT INTO session_files(session_id,path,op,tool,first_ts,last_ts,count,ignored) VALUES(?,?,?,?,?,?,1,?) "
        "ON CONFLICT(session_id,path) DO UPDATE SET last_ts=excluded.last_ts, count=count+1, "
        "op=CASE WHEN session_files.op='create' THEN 'create' ELSE excluded.op END, tool=COALESCE(excluded.tool, session_files.tool)",
        (session_id, path, op, tool, ts, ts, ign),
    )

def pending_files(con, session_id: str) -> list[sqlite3.Row]:
    """Non-ignored file touches in this session not yet claimed by an item and touched after the last waive."""
    s = con.execute("SELECT waived_at FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    waived_at = s["waived_at"] if s else None
    q = "SELECT * FROM session_files WHERE session_id=? AND ignored=0 AND logged_item_id IS NULL"
    args = [session_id]
    if waived_at:
        q += " AND last_ts > ?"; args.append(waived_at)
    return con.execute(q + " ORDER BY first_ts", args).fetchall()

def add_event(con, session_id: str, event: str, payload=None) -> None:
    con.execute("INSERT INTO session_events(session_id,event,payload) VALUES(?,?,?)",
                (session_id, event, json.dumps(payload) if payload is not None else None))

def reindex_item(con, item_id: int) -> None:
    it = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    con.execute("DELETE FROM items_fts WHERE rowid=?", (item_id,))
    if not it or it["status"] == "deleted":
        return
    paths = " ".join(r["path"] for r in con.execute("SELECT path FROM item_files WHERE item_id=?", (item_id,)))
    try:
        tags = " ".join(json.loads(it["tags"] or "[]"))
    except Exception:
        tags = it["tags"] or ""
    con.execute("INSERT INTO items_fts(rowid,title,summary,usage,tags,paths,project) VALUES(?,?,?,?,?,?,?)",
                (item_id, it["title"], it["summary"], it["usage"] or "", tags, paths, it["project"] or ""))

def rebuild_fts(con) -> int:
    con.execute("DELETE FROM items_fts")
    n = 0
    for r in con.execute("SELECT id FROM items WHERE status!='deleted'").fetchall():
        reindex_item(con, r["id"]); n += 1
    return n

def log_line(name: str, msg: str) -> None:
    """Append a line to logs/<name>.log, rotating at 5 MB. Never raises."""
    try:
        LOG_DIR.mkdir(exist_ok=True)
        p = LOG_DIR / f"{name}.log"
        if p.exists() and p.stat().st_size > 5_000_000:
            p.rename(LOG_DIR / f"{name}.log.1")
        with open(p, "a") as fh:
            fh.write(f"{now_iso()} {msg}\n")
    except Exception:
        pass

# ---------- durability heuristic + gate state ----------
DURABLE_EXTS = {
    ".py", ".sh", ".bash", ".zsh", ".html", ".htm", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".css", ".md", ".plist", ".sql", ".bat", ".cmd", ".ps1", ".yaml", ".yml", ".toml",
}
_STATEY = re.compile(r"/(state|data|cache|tmp|out|output|results?)/", re.I)

def is_durable(path: str) -> bool:
    """True when a touched file looks like something worth a ledger entry (code, pages, docs,
    configs) rather than routine data/state churn. Used by the Stop gate and by `tracker waive`:
    a pending durable file means waive is refused and the session must log instead."""
    p = str(path)
    ext = os.path.splitext(p)[1].lower()
    if ext in DURABLE_EXTS:
        return True
    if ext == ".json":
        # config/manifests are durable; anything under a */state/* dir is churn
        return not re.search(r"/state/", p, re.I)
    if ext in (".csv", ".db", ".sqlite", ".sqlite3", ".txt", ".jsonl", ".tsv", ".parquet", ".png", ".jpg", ".jpeg"):
        return False
    if ext == "":
        # extensionless: durable unless it lives in a data/state-ish directory
        return not _STATEY.search(p)
    return False

def counts_for_gate(path: str, tool: str | None) -> bool:
    """May this pending row block a handback? Paths from Write/Edit are trusted - they existed when
    the tool wrote them, so one deleted later this session still gates. A path scraped out of a
    shell command by regex (tool='Bash') is best effort: if it is not on disk it is parser debris,
    never an unlogged file, and must not block (`tracker waive` already reasons this way)."""
    if tool != "Bash":
        return True
    try:
        return os.path.exists(path)
    except OSError:
        return False

def gate_state(con, session_id: str) -> dict:
    """The single source of truth for the Stop gate (hooks/stop_gate.py and `tracker gate-check`).
    Returns pending file rows, whether the gate would block, and why."""
    s = con.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    rows = pending_files(con, session_id) if s else []
    pend = [r for r in rows if counts_for_gate(r["path"], r["tool"])]
    phantom = [r["path"] for r in rows if not counts_for_gate(r["path"], r["tool"])]
    max_blocks = int(config().get("gate", {}).get("max_blocks_per_session", 3))
    blocks = int(s["gate_blocks"]) if s else 0
    durable = [r["path"] for r in pend if is_durable(r["path"])]
    would = bool(pend) and blocks < max_blocks
    if not s:
        why = "session not registered"
    elif not pend:
        why = (f"no pending file touches ({len(phantom)} harvested path(s) not on disk, ignored)"
               if phantom else "no pending file touches")
    elif blocks >= max_blocks:
        why = f"block cap reached ({blocks}/{max_blocks})"
    else:
        why = f"{len(pend)} unlogged file touch(es)"
    return {
        "session_id": session_id,
        "exists": bool(s),
        "pending": [dict(r) for r in pend],
        "pending_count": len(pend),
        "phantom_pending": phantom,
        "durable_pending": durable,
        "durable_count": len(durable),
        "blocks": blocks,
        "max_blocks": max_blocks,
        "would_block": would,
        "why": why,
        "logged": int(s["logged"]) if s else 0,
        "waived_at": s["waived_at"] if s else None,
    }

def gate_reason(pending_paths: list[str], project: str | None = None,
                authors: dict | None = None) -> str:
    """The text the Stop gate shows Claude (kept under 1,200 chars).

    `authors` is db.file_authors(): {path: {agent_id, agent_type}} for paths a SUBAGENT wrote.
    The parent is still the one gated (a subagent's files count as yours), but the
    message names the real author so the parent can describe what it is logging instead of
    guessing at a path it never touched."""
    n = len(pending_paths)
    shown = pending_paths[:12]
    more = f" (+{n - len(shown)} more)" if n > len(shown) else ""
    authors = authors or {}

    def _label(p: str) -> str:
        a = authors.get(p)
        if not a:
            return p
        who = a.get("agent_type") or "subagent"
        aid = (a.get("agent_id") or "")[:8]
        return f"{p}   [written by subagent {who}{' ' + aid if aid else ''}]"

    files = "\n  ".join(_label(p) for p in shown)
    by_agent = len([p for p in pending_paths if p in authors])
    agent_note = (
        f"{by_agent} of these were written by your subagents — they count as yours; "
        "log them with explicit --file paths.\n" if by_agent else "")
    return (
        f"TRACKER GATE: you changed {n} file(s) that are not logged:\n  {files}{more}\n" + agent_note +
        "Log what you built - one item per distinct thing; an enhancement to something that already "
        "existed uses --kind enhancement (add --supersedes ID if it replaces an item):\n"
        '  tracker log --title "Name of the thing" --summary "what it is + why it exists, >=60 chars" '
        '--usage "how to run it / where to look" --file PATH[:role] [--link URL]\n'
        "If these edits are routine state/data churn with nothing new to know about, run:\n"
        '  tracker waive --reason "why there is nothing durable here"   (refused if the files look durable)\n'
        "`tracker status` lists every pending file grouped by directory; paths that are regex debris or "
        'not yours: `tracker dismiss <path|glob> --missing --reason "..."`.\n'
        "Then stop. See skill log-build. Do not edit hooks or settings to get past this."
    )

def item_files_of(con, item_id: int) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM item_files WHERE item_id=? ORDER BY id", (item_id,)).fetchall()

# ---------- backfill helpers ----------
def ensure_fts_deletable(con: sqlite3.Connection) -> bool:
    """items_fts is a contentless FTS5 table; without `contentless_delete=1` SQLite refuses
    `DELETE FROM items_fts WHERE rowid=?`, which reindex_item() does on every re-index of an
    existing item (edit, re-backfill, status change). Schema v1 shipped without the option, so
    upgrade the table in place (drop + recreate + rebuild) the first time we see an old DB.
    Returns True if the table was rebuilt. Safe to call on every connect."""
    row = con.execute("SELECT sql FROM sqlite_master WHERE name='items_fts'").fetchone()
    sql = (row[0] or "") if row else ""
    if row and (("contentless_delete" in sql) if FTS_CONTENTLESS else ("content=''" not in sql)):
        return False
    con.execute("DROP TABLE IF EXISTS items_fts")
    con.execute("CREATE VIRTUAL TABLE items_fts USING fts5("
                "title, summary, usage, tags, paths, project, "
                + FTS_OPTS + ")")
    rebuild_fts(con)
    return True


_BAD_PATH_CHARS = set('=%{}[]()<>|$"\'`*?,;:!&\\\n\t ')
# real file extensions we accept from regex-parsed shell commands (anything else must exist on disk)
FILE_EXTS = {
    "py","sh","bash","zsh","fish","js","mjs","cjs","ts","tsx","jsx","html","htm","css","scss",
    "json","jsonl","ndjson","csv","tsv","md","markdown","txt","rst","yml","yaml","toml","ini",
    "cfg","conf","env","properties","sql","db","sqlite","sqlite3","parquet","png","jpg","jpeg",
    "gif","svg","webp","ico","icns","pdf","ics","xlsx","xls","docx","pptx","zip","tar","gz","tgz",
    "bz2","xz","log","lock","plist","service","timer","applescript","scpt","osa","rb","go","rs",
    "java","c","h","cpp","hpp","swift","kt","php","pl","lua","r","ipynb","xml","xsl","tmpl","j2",
    "template","desktop","pem","crt","key","cer","bak","patch","diff","tsv","tex","srt","vtt",
    "wav","mp3","mp4","mov","webm","ttf","otf","woff","woff2","gitignore","dockerfile","makefile",
}

def path_ext(p: str) -> str:
    base = os.path.basename(p)
    if "." not in base[1:]:
        return ""
    return base.rsplit(".", 1)[-1].lower()

def plausible_path(p: str) -> bool:
    """Does this look like a real file a command wrote, rather than shell/JS/awk debris?
    `bash_written_paths` is regex-based, so `if x > 0`, an arrow function `(a,b)=>a.length-b.length`
    and `awk '{print > f}'` all leak through as fake paths (`/Users/.../0`, `/Users/.../o.value`).
    Accept only paths with a known file extension, or ones that actually exist on disk (checked
    first, so a real file under a directory with a space in its name - `.../My Apps/Demo/x.swift` -
    is not thrown away by _BAD_PATH_CHARS)."""
    if not p or len(p) > 400 or not oscompat.is_abs(p):
        return False
    base = os.path.basename(p)
    if not base or base.startswith("-") or base in (".", ".."):
        return False
    try:
        if os.path.isfile(p):
            return True
    except OSError:
        pass
    body = p[2:] if oscompat.IS_WIN and p[1:2] == ":" else p   # the drive colon is fine
    if any(c in _BAD_PATH_CHARS for c in body):
        return False
    return path_ext(p) in FILE_EXTS


_UUIDISH = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

def session_id_for_transcript(path: str) -> str:
    """The session a transcript belongs to. `<proj>/<sid>.jsonl` is that session; anything under
    `<proj>/<sid>/...` (subagent transcripts, e.g. `<sid>/subagents/agent-*.jsonl`) belongs to the
    parent <sid>, so its usage and text are attributed to the session that paid for it."""
    pp = Path(path)
    for anc in pp.parents:
        if _UUIDISH.match(anc.name):
            return anc.name
    return pp.stem

if __name__ == "__main__":
    con = connect()
    print("db ok:", DB_PATH, "items:", con.execute("SELECT count(*) FROM items").fetchone()[0])


# ---------- continuity helpers ----------
def session_recap(con, session_id: str, limit_chars: int = 350) -> str | None:
    """The best available "what I ended up doing" text for a session: the last assistant chunk
    from the transcript index, else a recap stashed in an item's extra blob. Both are optional -
    the tables may not exist yet on an old DB - so every lookup is guarded."""
    try:
        r = con.execute(
            "SELECT text FROM transcript_chunks WHERE session_id=? AND role='assistant' "
            "AND length(text)>=120 ORDER BY seq DESC LIMIT 1", (session_id,)).fetchone()
        if r and r["text"]:
            t = " ".join(str(r["text"]).split())
            return t[:limit_chars] + ("..." if len(t) > limit_chars else "")
    except sqlite3.OperationalError:
        pass
    try:
        r = con.execute("SELECT payload AS extra FROM session_events WHERE session_id=? AND event='recap' "
                        "ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
        if r and r["extra"]:
            t = " ".join(str(json.loads(r["extra"]).get("text") or "").split())
            if t:
                return t[:limit_chars] + ("..." if len(t) > limit_chars else "")
    except (sqlite3.OperationalError, ValueError, TypeError):
        pass
    try:
        r = con.execute("SELECT extra FROM items WHERE session_id=? AND extra IS NOT NULL "
                        "ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
        if r and r["extra"]:
            blob = json.loads(r["extra"])
            t = " ".join(str(blob.get("recap") or "").split())
            if t:
                return t[:limit_chars] + ("..." if len(t) > limit_chars else "")
    except (sqlite3.OperationalError, ValueError, TypeError):
        pass
    return None


import oscompat  # noqa: E402
IST = oscompat.local_tz()  # name kept for history: this is the LOCAL display timezone
TZ_LABEL = oscompat.tz_label()


def rate_limit_state(con, max_age_hours: float = 6.0) -> dict | None:
    """The newest rate-limit sample, if the table exists and the sample is fresh enough."""
    try:
        r = con.execute("SELECT * FROM rate_limit_samples ORDER BY ts DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    if not r:
        return None
    try:
        ts = datetime.datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00"))
        age_h = (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds() / 3600
    except ValueError:
        age_h = 0.0
    if age_h > max_age_hours:
        return None
    d = dict(r)
    d["age_hours"] = round(age_h, 2)
    return d


def _binding_window(con):
    """modules.usage.binding_window() when that module is available - it knows about the
    per-model `limits[]` windows, where the real constraint lives (the Fable-scoped weekly runs
    far ahead of the all-models seven_day column)."""
    try:
        from modules.usage import binding_window
        return binding_window(con)
    except Exception:
        return None


def _window_label(w: dict) -> str:
    group = str(w.get("group") or w.get("kind") or "window").replace("_", " ")
    group = {"five hour": "5-hour", "5h": "5-hour", "fivehour": "5-hour"}.get(group, group)
    scope = w.get("scope_model") or (w.get("model") or "").title() or None
    return f"{scope} {group}".strip() if scope else group


def _in_words(minutes) -> str:
    try:
        m = float(minutes)
    except (TypeError, ValueError):
        return ""
    if m < 90:
        return f"in {int(round(m))}m"
    if m < 48 * 60:
        return f"in {int(round(m / 60))}h"
    return f"in {m / 1440:.1f}d"


def usage_warning_line(con) -> str | None:
    """One line for the SessionStart briefing and `tracker status` when the account is close to the
    window that will actually stop work."""
    w = _binding_window(con)
    if w and (float(w.get("used_pct") or 0) >= 85 or w.get("severity") in ("warning", "critical")):
        sev = str(w.get("severity") or "").upper()
        when = w.get("resets_at_ist") or w.get("resets_at") or ""
        left = _in_words(w.get("resets_in_min"))
        resets = f" (resets {when}{', ' + left if left else ''})" if when else ""
        return (f"USAGE: {_window_label(w)} {float(w['used_pct']):.0f}%"
                + (f" {sev}" if sev else "") + resets +
                '. Keep the scoped model for judgment work; run mechanical work on smaller-model subagents '
                '(model:"opus"|"sonnet").')
    s = rate_limit_state(con)
    if not s:
        return None
    seven = s.get("seven_day_pct") or 0
    five = s.get("five_hour_pct") or 0
    if seven < 90 and five < 85:
        return None
    which, pct, resets = (("7-day", seven, s.get("seven_day_resets_at")) if seven >= 90
                          else ("5-hour", five, s.get("five_hour_resets_at")))
    when = ""
    if resets:
        try:
            when = datetime.datetime.fromisoformat(str(resets).replace("Z", "+00:00")).astimezone(
                IST).strftime("%a %H:%M ") + TZ_LABEL
        except ValueError:
            when = str(resets)
    model = (s.get("model") or "Fable").split("-")[1].title() if (s.get("model") or "").count("-") else (s.get("model") or "Fable")
    return (f"USAGE: {model} {which} {pct:.0f}%" + (f" (resets {when})" if when else "") +
            '. Keep the scoped model for judgment work; run mechanical work on smaller-model subagents '
            '(model:"opus"|"sonnet").')


def similar_items(con, title: str, limit: int = 3) -> list[dict]:
    """Items whose text matches the words of `title` - used to warn before logging a duplicate."""
    words = [w for w in re.split(r"[^\w]+", title or "") if len(w) > 3]
    if not words:
        return []
    q = " OR ".join(f'"{w}"' for w in words[:8])
    try:
        rows = con.execute(
            "SELECT i.id, i.title, i.project, i.kind, i.status, bm25(items_fts) rank "
            "FROM items_fts JOIN items i ON i.id=items_fts.rowid "
            "WHERE items_fts MATCH ? AND i.status NOT IN ('deleted','abandoned') "
            "ORDER BY rank LIMIT ?", (q, limit * 3)).fetchall()
    except sqlite3.OperationalError:
        return []
    tw = {w.lower() for w in words}
    out = []
    for r in rows:
        shared = tw & {w.lower() for w in re.split(r"[^\w]+", r["title"] or "") if len(w) > 3}
        if len(shared) >= 2:
            out.append({**dict(r), "shared_terms": sorted(shared)})
    return out[:limit]


# ---------- Bash command history ----------
CMD_MAX = 8000

def record_command(con, session_id: str, command: str, *, cwd: str | None = None,
                   description: str | None = None, wrote: int = 0, ts: str | None = None,
                   event_key: str | None = None, source: str = "hook") -> bool:
    """Keep the Bash command the PostToolUse hook already parses for file paths.

    Returns True when a row was inserted. `event_key` (the tool_use id when the payload has one)
    makes the write idempotent, so the hook and a later backfill of the same transcript cannot
    store the same command twice. Never raises: the table may be missing on a pre-migration DB
    and no hook may fail because of a history nicety.
    """
    cmd = (command or "").strip()
    if not cmd:
        return False
    if len(cmd) > CMD_MAX:
        cmd = cmd[:CMD_MAX] + "\n… (truncated)"
    ts = ts or now_iso()
    key = event_key or f"{session_id}:{ts}:{hashlib.sha1(cmd.encode('utf-8', 'replace')).hexdigest()[:16]}"
    try:
        cur = con.execute(
            "INSERT OR IGNORE INTO session_commands"
            "(event_key,session_id,ts,cwd,project,command,description,wrote,source) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (key, session_id, ts, cwd, derive_project(cwd) if cwd else None,
             cmd, (description or None), int(wrote or 0), source))
        return bool(cur.rowcount)
    except sqlite3.Error:
        return False


def commands_for_session(con, session_id: str, limit: int = 200) -> list[dict]:
    try:
        rows = con.execute(
            "SELECT id, ts, cwd, project, command, description, wrote, source "
            "FROM session_commands WHERE session_id=? ORDER BY ts DESC, id DESC LIMIT ?",
            (session_id, limit)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def search_commands(con, q: str = "", *, session_id: str | None = None, project: str | None = None,
                    wrote_only: bool = False, limit: int = 50, offset: int = 0) -> list[dict]:
    """Substring search over stored commands, newest first. No FTS: commands are shell text,
    where `grep -n`-style substring matching is what a person actually means."""
    where, args = ["1=1"], []
    if q:
        where.append("command LIKE ?"); args.append(f"%{q}%")
    if session_id:
        where.append("session_id=?"); args.append(session_id)
    if project:
        where.append("project=?"); args.append(project)
    if wrote_only:
        where.append("wrote>0")
    try:
        rows = con.execute(
            "SELECT c.id, c.ts, c.session_id, c.cwd, c.project, c.command, c.description, c.wrote, "
            "c.source, s.title session_title FROM session_commands c "
            "LEFT JOIN sessions s ON s.session_id=c.session_id "
            f"WHERE {' AND '.join(where)} ORDER BY c.ts DESC, c.id DESC LIMIT ? OFFSET ?",
            (*args, int(limit), int(offset))).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def release_unclaimed(con, paths, claiming_item_id: int | None = None) -> int:
    """Drop `paths` from every auto-item's `extra.unclaimed_paths`.

    A gate bypass records the pending paths on an inferred item without claiming them, so the
    real author can still log them. When that author does, the path stops being "unclaimed" and
    must leave the auto item's list - otherwise the ledger shows the same file as both orphaned
    and logged. An auto item left with an empty list and no other content is closed as deleted.
    Never raises: it is bookkeeping, not the point of the write.
    """
    want = {str(p) for p in (paths or [])}
    if not want:
        return 0
    touched = 0
    try:
        rows = con.execute(
            "SELECT id, extra FROM items WHERE source='auto' AND extra LIKE '%unclaimed_paths%'"
        ).fetchall()
    except sqlite3.Error:
        return 0
    for r in rows:
        if claiming_item_id is not None and r["id"] == claiming_item_id:
            continue
        try:
            extra = json.loads(r["extra"] or "{}")
        except Exception:
            continue
        left = [p for p in (extra.get("unclaimed_paths") or []) if p not in want]
        if len(left) == len(extra.get("unclaimed_paths") or []):
            continue
        if left:
            extra["unclaimed_paths"] = left
        else:
            extra.pop("unclaimed_paths", None)
            extra["unclaimed_cleared_at"] = now_iso()
        try:
            con.execute("UPDATE items SET extra=?, updated_at=? WHERE id=?",
                        (json.dumps(extra), now_iso(), r["id"]))
            if not left:
                # every path it was holding now belongs to a real item
                con.execute("UPDATE items SET status='superseded' WHERE id=? AND status='wip'", (r["id"],))
            touched += 1
        except sqlite3.Error:
            pass
    return touched


# ---------- subagent attribution ----------
# A tool call made inside a subagent arrives at PostToolUse carrying the PARENT's session_id
# plus its own `agent_id` (confirmed against the Claude Code 2.1.269 hook schema — see
# hooks/HOOK_CONTRACT.md §1). Without these helpers the write lands in session_files under
# the parent and the Stop gate blocks the parent for work it cannot describe. We keep the
# parent's row (a subagent's files count as the parent's) and record the author
# alongside it, so the block message can name who wrote each path.

def upsert_agent(con, parent_session_id: str, agent_id: str, **fields) -> None:
    """Create or update the row for one subagent run. Never raises on a missing table."""
    fields = {k: v for k, v in fields.items() if v is not None}
    if "cwd" in fields and "project" not in fields:
        fields["project"] = derive_project(fields["cwd"])
    cols = list(fields.keys())
    try:
        con.execute(
            "INSERT INTO session_agents(parent_session_id,agent_id"
            + ("," + ",".join(cols) if cols else "") + ") VALUES(?,?"
            + ("," + ",".join("?" for _ in cols) if cols else "") + ") "
            "ON CONFLICT(parent_session_id,agent_id) DO UPDATE SET "
            + "".join(f"{k}=COALESCE(excluded.{k}, session_agents.{k}), " for k in cols)
            + "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            [parent_session_id, agent_id, *fields.values()])
    except sqlite3.Error as exc:
        log_line("hooks", f"upsert_agent({agent_id[:8] if agent_id else '?'}): {exc}")


def record_agent_file(con, parent_session_id: str, agent_id: str, path: str, *,
                      agent_type: str | None = None, op: str = "modify",
                      tool: str | None = None, ts: str | None = None) -> None:
    """Attribute one file touch to the subagent that actually made it."""
    ts = ts or now_iso()
    try:
        con.execute(
            "INSERT INTO agent_files(parent_session_id,agent_id,agent_type,path,op,tool,"
            "first_ts,last_ts,count) VALUES(?,?,?,?,?,?,?,?,1) "
            "ON CONFLICT(agent_id,path) DO UPDATE SET last_ts=excluded.last_ts, count=count+1, "
            "op=CASE WHEN agent_files.op='create' THEN 'create' ELSE excluded.op END, "
            "tool=COALESCE(excluded.tool, agent_files.tool)",
            (parent_session_id, agent_id, agent_type, path, op, tool, ts, ts))
    except sqlite3.Error as exc:
        log_line("hooks", f"record_agent_file: {exc}")


def file_authors(con, session_id: str) -> dict:
    """{path: {"agent_id", "agent_type", "n"}} for paths in this session written by a subagent.

    Paths absent from the map were written by the main thread. Degrades to {} on an old DB."""
    out: dict = {}
    try:
        rows = con.execute(
            "SELECT path, agent_id, agent_type, count FROM agent_files WHERE parent_session_id=? "
            "ORDER BY last_ts", (session_id,)).fetchall()
    except sqlite3.Error:
        return out
    for r in rows:
        out[r["path"]] = {"agent_id": r["agent_id"], "agent_type": r["agent_type"],
                          "n": r["count"]}
    return out


def agents_of_session(con, session_id: str) -> list[dict]:
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM session_agents WHERE parent_session_id=? ORDER BY id", (session_id,))]
    except sqlite3.Error:
        return []
