#!/usr/bin/env python3
"""Claude Code statusLine command — prints the terminal status line AND records the plan limits.

Claude Code pipes a JSON payload to this script on every status-line refresh. Besides the model
and workspace it carries `rate_limits` for Claude.ai subscribers — the same numbers `/usage`
shows — which is the only place the real 5-hour and weekly limits are visible locally. We print
one compact line and append a sample to `rate_limit_samples` (at most once per 60s per session,
and never two identical samples in a row) so the Command Center can chart the burn.

    fable-5.1 · ctx 41% · 5h 62% ↻14:30 · wk 95% ↻Mon 09:00 · $12.40

Standalone on purpose: no tracker imports beyond oscompat, no schema migration, no config read — this runs on
every refresh and must stay a couple of milliseconds. Never raises; on any failure it still
prints something and exits 0, because a broken status line must never wedge the terminal.
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("CLAUDE_TRACKER_DB") or ROOT / "tracker.db")
MIN_SAMPLE_SECONDS = 60
sys.path.insert(0, str(ROOT))
import oscompat  # noqa: E402
IST = oscompat.local_tz()  # name kept for history: this is the LOCAL display timezone
TZ_LABEL = oscompat.tz_label()
DDL = """CREATE TABLE IF NOT EXISTS rate_limit_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, session_id TEXT, model TEXT,
  five_hour_pct REAL, five_hour_resets_at INTEGER, seven_day_pct REAL, seven_day_resets_at INTEGER,
  spend_pct REAL, spend_resets_at INTEGER, context_pct REAL, cost_usd REAL,
  source TEXT NOT NULL DEFAULT 'statusline', raw TEXT)"""


# ------------------------------------------------------------------ payload parsing
def _num(x):
    if isinstance(x, bool) or x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x.strip().rstrip("%"))
        except ValueError:
            return None
    return None


def _dig(obj, *names):
    """First present value among several key spellings, at this level."""
    if not isinstance(obj, dict):
        return None
    for n in names:
        if n in obj and obj[n] is not None:
            return obj[n]
    return None


def context_tokens(cw) -> float | None:
    """Tokens in the context now: the current request's input, cache writes and cache reads
    (context_window.current_usage). Verified against a stored payload on 2026-09-25: the sum
    equals total_input_tokens and over context_window_size gives used_percentage."""
    if not isinstance(cw, dict):
        return None
    cu = _dig(cw, "current_usage", "currentUsage")
    if isinstance(cu, dict):
        parts = [_num(_dig(cu, *k)) for k in (("input_tokens", "inputTokens"),
                                             ("cache_creation_input_tokens", "cacheCreationInputTokens"),
                                             ("cache_read_input_tokens", "cacheReadInputTokens"))]
        if any(v is not None for v in parts):
            return sum(v for v in parts if v is not None)
    return None


def _ktok(v) -> str:
    if v is None:
        return ""
    return f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v / 1e3:.0f}k"


def short_model(model: str | None) -> str:
    m = (model or "").strip()
    if not m:
        return ""
    low = m.lower().replace("claude-", "")
    for fam in ("fable", "opus", "sonnet", "haiku"):
        if fam in low:
            digits = [p for p in low.split("-") if p.isdigit()]
            return f"{fam}-{'.'.join(digits[:2])}" if digits else fam
    return m[:18]


def parse_payload(data: dict) -> dict:
    """Pull everything we care about out of the statusLine JSON, tolerating shape drift."""
    out: dict = {"session_id": None, "model": None, "context_pct": None, "cost_usd": None,
                 "cwd": None, "limits": {}, "context_tokens": None, "context_size": None}
    if not isinstance(data, dict):
        return out
    out["session_id"] = _dig(data, "session_id", "sessionId")
    model = _dig(data, "model")
    if isinstance(model, dict):
        model = _dig(model, "id", "display_name", "name")
    out["model"] = model if isinstance(model, str) else None
    cw = _dig(data, "context_window", "contextWindow") or {}
    if not isinstance(cw, dict):
        cw = {}
    out["context_pct"] = _num(_dig(cw, "used_percentage", "usedPercentage", "percentage"))
    out["context_size"] = _num(_dig(cw, "context_window_size", "contextWindowSize"))
    out["context_tokens"] = context_tokens(cw)
    cost = _dig(data, "cost", "session")
    if isinstance(cost, dict):
        out["cost_usd"] = _num(_dig(cost, "total_cost_usd", "totalCostUSD", "total_cost", "cost_usd"))
    elif cost is not None:
        out["cost_usd"] = _num(cost)
    ws = _dig(data, "workspace") or {}
    out["cwd"] = _dig(ws, "current_dir", "currentDir") or _dig(data, "cwd")
    rl = _dig(data, "rate_limits", "rateLimits") or {}
    for key, alias in (("five_hour", "fiveHour"), ("seven_day", "sevenDay"),
                       ("spend_limit", "spendLimit")):
        blk = _dig(rl, key, alias)
        if isinstance(blk, dict):
            out["limits"][key] = {
                "pct": _num(_dig(blk, "used_percentage", "usedPercentage", "percentage", "used_pct")),
                "resets_at": _num(_dig(blk, "resets_at", "resetsAt", "reset_at")),
            }
    return out


# ------------------------------------------------------------------ rendering
def fmt_reset(epoch: float | None, now: float | None = None) -> str:
    """'↻14:30' for today, '↻Mon 09:00' further out. Local time (config.json timezone)."""
    if not epoch:
        return ""
    try:
        now = now or time.time()
        dt = datetime.datetime.fromtimestamp(float(epoch), IST)
        nowdt = datetime.datetime.fromtimestamp(now, IST)
        if dt.date() == nowdt.date():
            return f"↻{dt:%H:%M}"
        if (dt.date() - nowdt.date()).days < 7:
            return f"↻{dt:%a %H:%M}"
        return f"↻{dt:%d %b %H:%M}"
    except Exception:
        return ""


def _pct(v) -> str:
    return f"{v:.0f}%" if v is not None else ""


def render(p: dict, now: float | None = None) -> str:
    """Compact single line; every segment is dropped when its data is absent."""
    seg: list[str] = []
    m = short_model(p.get("model"))
    if m:
        seg.append(m)
    if p.get("context_pct") is not None:
        tok = _ktok(p.get("context_tokens"))
        seg.append(f"ctx {_pct(p['context_pct'])}" + (f" {tok}" if tok else ""))
    lim = p.get("limits") or {}
    for key, label in (("five_hour", "5h"), ("seven_day", "wk")):
        blk = lim.get(key) or {}
        if blk.get("pct") is None:
            continue
        txt = f"{label} {_pct(blk['pct'])}"
        r = fmt_reset(blk.get("resets_at"), now)
        seg.append(f"{txt} {r}".strip())
    spend = lim.get("spend_limit") or {}
    if spend.get("pct") is not None:
        seg.append(f"spend {_pct(spend['pct'])}")
    if p.get("cost_usd") is not None:
        seg.append(f"${p['cost_usd']:.2f}")
    return " · ".join(seg)


# ------------------------------------------------------------------ storage
def _iso(t: float | None = None) -> str:
    return datetime.datetime.fromtimestamp(t or time.time(), datetime.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%S.000Z")


def should_store(con, p: dict, now: float | None = None) -> bool:
    """One sample a minute per session, and never a duplicate of the previous one."""
    lim = p.get("limits") or {}
    if not lim:
        return False
    row = con.execute(
        "SELECT ts, five_hour_pct, seven_day_pct, spend_pct, context_pct FROM rate_limit_samples "
        "WHERE session_id IS ? ORDER BY id DESC LIMIT 1", (p.get("session_id"),)).fetchone()
    if not row:
        return True
    try:
        last = datetime.datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except Exception:
        return True
    same = (row[1] == (lim.get("five_hour") or {}).get("pct")
            and row[2] == (lim.get("seven_day") or {}).get("pct")
            and row[3] == (lim.get("spend_limit") or {}).get("pct")
            and row[4] == p.get("context_pct"))
    if same:
        return False
    return (now or time.time()) - last >= MIN_SAMPLE_SECONDS


def store(con, p: dict, raw: str, source: str = "statusline", now: float | None = None) -> bool:
    if not should_store(con, p, now):
        return False
    lim = p.get("limits") or {}
    fh, sd, sp = (lim.get("five_hour") or {}), (lim.get("seven_day") or {}), (lim.get("spend_limit") or {})
    con.execute(
        "INSERT INTO rate_limit_samples(ts,session_id,model,five_hour_pct,five_hour_resets_at,"
        "seven_day_pct,seven_day_resets_at,spend_pct,spend_resets_at,context_pct,cost_usd,source,raw)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (_iso(now), p.get("session_id"), p.get("model"), fh.get("pct"),
         int(fh["resets_at"]) if fh.get("resets_at") else None, sd.get("pct"),
         int(sd["resets_at"]) if sd.get("resets_at") else None, sp.get("pct"),
         int(sp["resets_at"]) if sp.get("resets_at") else None, p.get("context_pct"),
         p.get("cost_usd"), source, raw[:8000]))
    return True


CTX_DIR = ROOT / "state" / "ctx"
TITLE_TAIL_BYTES = 1 << 20


def transcript_title(path) -> str | None:
    """The session's newest `ai-title` — the same words Claude Code puts in the terminal title,
    which is how the menu bar's context reader knows which tab is in front."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TITLE_TAIL_BYTES))
            tail = f.read()
    except Exception:
        return None
    for line in reversed(tail.split(b"\n")):
        if b'"ai-title"' not in line:
            continue
        try:
            t = json.loads(line).get("aiTitle")
        except Exception:
            continue
        if isinstance(t, str) and t.strip():
            return t.strip()
    return None


def write_ctx(p: dict, data: dict, now: float | None = None) -> None:
    """One small JSON per session in state/ctx/ for the menu bar context reader. Atomic rename,
    so the reader never sees half a file; it watches the directory and swaps on the write."""
    sid = p.get("session_id")
    if not sid or p.get("context_pct") is None:
        return
    CTX_DIR.mkdir(parents=True, exist_ok=True)
    dst = CTX_DIR / f"{sid}.json"
    title = transcript_title(_dig(data, "transcript_path", "transcriptPath"))
    if not title:
        try:
            title = json.loads(dst.read_text()).get("title")
        except Exception:
            title = None
    rec = {"session_id": sid, "title": title, "pct": p.get("context_pct"),
           "tokens": p.get("context_tokens"), "size": p.get("context_size"),
           "model": short_model(p.get("model")), "cwd": p.get("cwd"), "ts": now or time.time()}
    tmp = dst.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rec))
    os.replace(tmp, dst)


def seed_ctx(days: int = 3) -> int:
    """Fill state/ctx/ from the newest stored payload of every recent session, so tabs that have
    been idle since the reader shipped still resolve before their next status-line refresh."""
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2)
    rows = con.execute(
        "SELECT raw FROM rate_limit_samples WHERE id IN (SELECT MAX(id) FROM rate_limit_samples "
        "WHERE session_id IS NOT NULL AND ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?) "
        "GROUP BY session_id)", (f"-{days} days",)).fetchall()
    n = 0
    for (raw,) in rows:
        try:
            data = json.loads(raw)
        except Exception:
            continue
        p = parse_payload(data)
        if p.get("session_id") and not (CTX_DIR / f"{p['session_id']}.json").exists():
            write_ctx(p, data, now=0)
            n += 1
    return n


def connect():
    con = sqlite3.connect(str(DB_PATH), timeout=2, isolation_level=None)
    con.execute("PRAGMA busy_timeout=1500")
    con.execute(DDL)
    return con


def main() -> None:
    raw = ""
    try:
        raw = sys.stdin.read() or ""
    except Exception:
        raw = ""
    data = {}
    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    p = parse_payload(data if isinstance(data, dict) else {})
    try:
        sys.stdout.write(render(p))
    except Exception:
        pass
    try:
        write_ctx(p, data if isinstance(data, dict) else {})
    except Exception:
        pass
    try:
        con = connect()
        try:
            store(con, p, raw)
        finally:
            con.close()
    except Exception:
        pass            # a status line must never fail loudly
    sys.exit(0)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--seed-ctx"]:
        print(f"seeded {seed_ctx()} session(s) into {CTX_DIR}")
    else:
        main()
