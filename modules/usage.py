#!/usr/bin/env python3
"""Command Center module 3 — Usage & cost.

GET /api/usage/summary?days=30   per-day per-model tokens and cost, per-project cost,
                                 rolling 5h / 7d totals per model family (the local proxy
                                 for the plan's 5-hour and weekly limits), hour-of-day
                                 histogram, top sessions by cost, avg cost per session.
GET /api/usage/series?days=90    daily series, one row per day per family.
GET /api/usage/models            the price table actually used, and what it is calibrated against.

Data comes from `usage_events`, filled by `backfill.py --usage` over every transcript and by
`index_session(transcript_path)` from the SessionEnd hook. One row per assistant API response
(`message.usage`), keyed by `message.id` so resumed/forked transcripts never double count.

Costs here are ESTIMATES computed from the published per-MTok price table below; the
authoritative number is the `cost-state` record Claude Code writes per session, which lands
in `sessions.cost_usd` — every payload says which basis it used.
Stdlib only.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from modules.common import ApiError  # noqa: E402

MODULE = "usage"

# USD per 1,000,000 tokens: (input, output, cache_read, cache_write).
# Published Anthropic API rates; cache read is 0.1x input and cache write 1.25x input unless
# the model states otherwise (Fable 5.1 reads at $0.25/MTok). Web searches are $10 per 1,000.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5-1":  (10.0, 50.0, 0.25, 12.50),
    "claude-fable-5":    (10.0, 50.0, 1.00, 12.50),
    "claude-opus-5":     (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4":     (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5":   (2.0, 10.0, 0.20, 2.50),
    "claude-sonnet-4":   (3.0, 15.0, 0.30, 3.75),
    "claude-haiku-4":    (1.0, 5.0, 0.10, 1.25),
    "claude-haiku-3":    (0.8, 4.0, 0.08, 1.00),
}
DEFAULT_PRICE = (5.0, 25.0, 0.50, 6.25)          # unknown model → assume Opus tier
FAMILIES = ("fable", "opus", "sonnet", "haiku")
WINDOW_5H_HOURS = 5
WINDOW_7D_DAYS = 7


def model_family(model: str | None) -> str:
    m = (model or "").lower()
    for fam in FAMILIES:
        if fam in m:
            return fam
    return "other"


def price_for(model: str | None) -> tuple[tuple[float, float, float, float], bool]:
    """(prices, exact) — exact=False means we fell back to a prefix or the default."""
    m = (model or "").lower()
    if m in PRICES:
        return PRICES[m], True
    best = None
    for key, val in PRICES.items():
        if m.startswith(key) and (best is None or len(key) > len(best[0])):
            best = (key, val)
    if best:
        return best[1], True
    return DEFAULT_PRICE, False


def estimate_cost(model: str | None, inp: int, out: int, cache_read: int, cache_create: int) -> float:
    (pi, po, pr, pw), _ = price_for(model)
    return round((inp * pi + out * po + cache_read * pr + cache_create * pw) / 1_000_000.0, 6)


# ------------------------------------------------------------------ transcript → rows
_TYPE_HEAD = re.compile(r'^\{"type":"([a-z-]+)"')


def extract_usage(transcript_path: str) -> list[tuple]:
    """Stream one transcript and return usage rows. Pure: no DB, safe in a worker process.
    Row: (event_key, session_id, ts, model, family, in, out, cache_read, cache_create,
          cost_est, project, sidechain)."""
    rows: list[tuple] = []
    seen: set[str] = set()
    sid = db.session_id_for_transcript(transcript_path)
    cwd = None
    try:
        fh = open(transcript_path, "r", errors="replace")
    except OSError:
        return rows
    with fh:
        for line in fh:
            if len(line) < 3 or line.startswith('{"type":"') or '"attachment":{' in line[:220]:
                continue
            if '"usage":' not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not isinstance(rec, dict) or rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            usage = msg.get("usage") if isinstance(msg, dict) else None
            if not isinstance(usage, dict):
                continue
            key = msg.get("id") or rec.get("uuid")
            if not key or key in seen:
                continue
            seen.add(key)
            if rec.get("cwd"):
                cwd = rec["cwd"]
            model = msg.get("model")
            inp = int(usage.get("input_tokens") or 0)
            out = int(usage.get("output_tokens") or 0)
            cr = int(usage.get("cache_read_input_tokens") or 0)
            cc = int(usage.get("cache_creation_input_tokens") or 0)
            if not (inp or out or cr or cc):
                continue
            rows.append((
                key, sid, rec.get("timestamp"), model,
                model_family(model), inp, out, cr, cc,
                estimate_cost(model, inp, out, cr, cc),
                db.derive_project(cwd), 1 if rec.get("isSidechain") else 0,
            ))
    return rows


def store_usage(con, rows) -> int:
    """Insert rows, ignoring ones already recorded (idempotent per message id)."""
    before = con.total_changes
    con.executemany(
        "INSERT OR IGNORE INTO usage_events(event_key,session_id,ts,model,family,input_tokens,"
        "output_tokens,cache_read,cache_create,cost_usd_est,project,sidechain) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return con.total_changes - before


def index_session(transcript_path: str, con=None) -> dict:
    """Index one session's token usage. Called by the SessionEnd hook and by backfill.
    Idempotent; cheap to call repeatedly on the same transcript."""
    own = con is None
    con = con or db.connect()
    try:
        rows = extract_usage(transcript_path)
        n = store_usage(con, rows) if rows else 0
        return {"transcript": transcript_path, "messages": len(rows), "inserted": n}
    finally:
        if own:
            con.close()


# ------------------------------------------------------------------ query helpers
def _iso_days_ago(days: float) -> str:
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _int(query: dict, name: str, default: int, lo: int, hi: int) -> int:
    raw = query.get(name)
    if raw in (None, ""):
        return default
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        raise ApiError(400, f"{name} must be a number")
    return max(lo, min(hi, v))


def _rows(con, sql, args=()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, args).fetchall()]


# ------------------------------------------------------------------ handlers
def get_summary(query: dict, body, match) -> tuple[int, object]:
    days = _int(query, "days", 30, 1, 400)
    con = db.connect()
    try:
        since = _iso_days_ago(days)
        total = con.execute(
            "SELECT count(*) n, sum(input_tokens) i, sum(output_tokens) o, sum(cache_read) cr, "
            "sum(cache_create) cc, sum(cost_usd_est) est FROM usage_events WHERE ts>=?",
            (since,)).fetchone()
        by_day = _rows(con,
            "SELECT substr(ts,1,10) day, family, model, sum(input_tokens) input_tokens, "
            "sum(output_tokens) output_tokens, sum(cache_read) cache_read, "
            "sum(cache_create) cache_create, round(sum(cost_usd_est),4) cost_usd_est, count(*) calls "
            "FROM usage_events WHERE ts>=? GROUP BY day, family, model ORDER BY day, family", (since,))
        by_project = _rows(con,
            "SELECT coalesce(project,'unknown') project, count(DISTINCT session_id) sessions, "
            "sum(input_tokens+output_tokens+cache_read+cache_create) tokens, "
            "round(sum(cost_usd_est),4) cost_usd_est FROM usage_events WHERE ts>=? "
            "GROUP BY project ORDER BY cost_usd_est DESC", (since,))
        by_hour = _rows(con,
            "SELECT cast(strftime('%H', ts, '+5 hours', '+30 minutes') AS INTEGER) hour, "
            "count(*) calls, round(sum(cost_usd_est),4) cost_usd_est FROM usage_events "
            "WHERE ts>=? GROUP BY hour ORDER BY hour", (since,))
        # rolling windows — the local proxy for the 5-hour and weekly plan limits
        windows = {}
        for name, cut in (("last_5h", _iso_days_ago(WINDOW_5H_HOURS / 24.0)),
                          ("last_7d", _iso_days_ago(WINDOW_7D_DAYS))):
            fam = _rows(con,
                "SELECT family, sum(input_tokens) input_tokens, sum(output_tokens) output_tokens, "
                "sum(cache_read) cache_read, sum(cache_create) cache_create, "
                "sum(input_tokens+output_tokens+cache_read+cache_create) tokens, "
                "round(sum(cost_usd_est),4) cost_usd_est, count(*) calls "
                "FROM usage_events WHERE ts>=? GROUP BY family ORDER BY tokens DESC", (cut,))
            windows[name] = {"since": cut, "by_family": fam,
                             "tokens": sum(f["tokens"] or 0 for f in fam),
                             "cost_usd_est": round(sum(f["cost_usd_est"] or 0 for f in fam), 4),
                             "calls": sum(f["calls"] or 0 for f in fam)}
        # top sessions: prefer the recorded cost-state number, fall back to our estimate
        top = _rows(con, """
            SELECT u.session_id, s.title, coalesce(s.project, u.project) project,
                   substr(coalesce(s.started_at, min(u.ts)),1,10) date,
                   round(sum(u.cost_usd_est),4) cost_usd_est, s.cost_usd cost_usd_recorded,
                   sum(u.input_tokens+u.output_tokens+u.cache_read+u.cache_create) tokens,
                   count(*) calls, s.model
            FROM usage_events u LEFT JOIN sessions s ON s.session_id=u.session_id
            WHERE u.ts>=? GROUP BY u.session_id
            ORDER BY coalesce(s.cost_usd, sum(u.cost_usd_est)) DESC LIMIT 20""", (since,))
        avg_project = _rows(con, """
            SELECT project, count(*) sessions, round(avg(c),4) avg_cost_usd_est,
                   round(sum(c),4) total_cost_usd_est FROM (
                SELECT coalesce(project,'unknown') project, session_id, sum(cost_usd_est) c
                FROM usage_events WHERE ts>=? GROUP BY session_id)
            GROUP BY project ORDER BY total_cost_usd_est DESC""", (since,))
        recorded = con.execute(
            "SELECT round(sum(cost_usd),4) c, count(*) n FROM sessions WHERE cost_usd IS NOT NULL "
            "AND started_at>=?", (since,)).fetchone()
        cal = con.execute("""
            SELECT count(*) sessions, round(sum(est),4) est, round(sum(rec),4) rec FROM (
              SELECT sum(u.cost_usd_est) est, s.cost_usd rec FROM usage_events u
              JOIN sessions s ON s.session_id=u.session_id
              WHERE s.cost_usd IS NOT NULL AND s.cost_usd > 0 GROUP BY u.session_id)""").fetchone()
        ratio = round((cal["est"] or 0) / cal["rec"], 4) if cal and cal["rec"] else None
        span = con.execute("SELECT min(ts) a, max(ts) b FROM usage_events").fetchone()
        return 200, {
            "days": days, "since": since,
            "totals": {"api_calls": total["n"] or 0, "input_tokens": total["i"] or 0,
                       "output_tokens": total["o"] or 0, "cache_read": total["cr"] or 0,
                       "cache_create": total["cc"] or 0,
                       "cost_usd_est": round(total["est"] or 0, 4),
                       "cost_usd_recorded": recorded["c"], "sessions_with_recorded_cost": recorded["n"]},
            "by_day": by_day, "by_project": by_project, "by_hour": by_hour,
            "hour_timezone": TZ_LABEL,
            "windows": windows, "top_sessions": top, "avg_cost_by_project": avg_project,
            "coverage": {"first_event": span["a"], "last_event": span["b"]},
            "calibration": {
                "sessions_compared": cal["sessions"] if cal else 0,
                "est_total": cal["est"] if cal else None, "recorded_total": cal["rec"] if cal else None,
                "est_over_recorded": ratio,
                "note": "Measured against Claude Code's own cost-state records. The estimate runs "
                        "low  because transcripts record per-message usage only: title generation, "
                        "compaction and some server-tool calls are billed but never written as "
                        "assistant records. Multiply by 1/ratio for a truer figure."},
            "cost_basis": "cost_usd_est is an ESTIMATE from modules/usage.py PRICES "
                          "(published per-MTok rates); cost_usd_recorded comes from Claude Code's "
                          "own cost-state records and is authoritative where present.",
        }
    finally:
        con.close()


def get_series(query: dict, body, match) -> tuple[int, object]:
    days = _int(query, "days", 90, 1, 400)
    fam_filter = (query.get("family") or "").strip().lower()
    con = db.connect()
    try:
        since = _iso_days_ago(days)
        sql = ("SELECT substr(ts,1,10) day, family, sum(input_tokens) input_tokens, "
               "sum(output_tokens) output_tokens, sum(cache_read) cache_read, "
               "sum(cache_create) cache_create, "
               "sum(input_tokens+output_tokens+cache_read+cache_create) tokens, "
               "round(sum(cost_usd_est),4) cost_usd_est, count(*) calls, "
               "count(DISTINCT session_id) sessions FROM usage_events WHERE ts>=?")
        args: list = [since]
        if fam_filter:
            sql += " AND family=?"
            args.append(fam_filter)
        sql += " GROUP BY day, family ORDER BY day, family"
        rows = _rows(con, sql, args)
        days_present = sorted({r["day"] for r in rows})
        return 200, {"days": days, "since": since, "family": fam_filter or None,
                     "series": rows, "days_present": days_present,
                     "families": sorted({r["family"] for r in rows}),
                     "cost_basis": "estimate (see /api/usage/summary)"}
    finally:
        con.close()


def get_models(query: dict, body, match) -> tuple[int, object]:
    con = db.connect()
    try:
        seen = _rows(con, "SELECT model, family, count(*) calls, "
                          "round(sum(cost_usd_est),4) cost_usd_est FROM usage_events "
                          "GROUP BY model ORDER BY calls DESC")
        for row in seen:
            (pi, po, pr, pw), exact = price_for(row["model"])
            row["price_per_mtok"] = {"input": pi, "output": po, "cache_read": pr, "cache_write": pw}
            row["price_exact"] = exact
        return 200, {"prices_usd_per_mtok": {k: {"input": v[0], "output": v[1],
                                                 "cache_read": v[2], "cache_write": v[3]}
                                             for k, v in PRICES.items()},
                     "default_price": DEFAULT_PRICE, "models_seen": seen,
                     "note": "Estimates only. Published API rates; cache read/write derived at "
                             "0.1x / 1.25x of input unless the model publishes its own rate."}
    finally:
        con.close()



# ==========================================================================================
# Plan limits: the real 5-hour / weekly windows, sampled by hooks/statusline.py
# ==========================================================================================
STALE_MINUTES = 15
RESET_MAX_PCT = 25.0      # a rollover lands near zero; anything else is just a stale snapshot
HISTORY_DAYS = 14
HISTORY_MAX_POINTS = 600
import oscompat  # noqa: E402
IST = oscompat.local_tz()  # name kept for history: this is the LOCAL display timezone
TZ_LABEL = oscompat.tz_label()


def _epoch_iso(epoch) -> str | None:
    if not epoch:
        return None
    try:
        return datetime.datetime.fromtimestamp(float(epoch), datetime.timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def _epoch_ist(epoch) -> str | None:
    if not epoch:
        return None
    try:
        return datetime.datetime.fromtimestamp(float(epoch), IST).strftime("%Y-%m-%d %H:%M ") + TZ_LABEL
    except Exception:
        return None


def _parse_iso(ts: str | None) -> float | None:
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.datetime.strptime(ts, fmt).replace(
                tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _burn(samples: list[dict], pct_key: str, reset_key: str,
          min_span_hours: float = 0.25) -> tuple[float | None, float | None]:
    """(pct per hour, projected epoch at 100%) from samples inside the CURRENT window.
    A window is identified by its resets_at; samples from an earlier window are ignored so a
    reset (100% → 3%) never reads as negative burn."""
    pts = [(_parse_iso(s["ts"]), s[pct_key]) for s in samples
           if s.get(pct_key) is not None and s.get("ts")]
    pts = [(t, p) for t, p in pts if t]
    if len(pts) < 2:
        return None, None
    pts.sort()
    # Two kinds of dips to survive: a window RESET (a big drop, everything before it is a
    # different window) and per-session STALENESS (six running sessions each report their own
    # last refresh, so the series rattles 74 → 28 → 69 within a minute). Cut at the last big
    # drop, then take the running maximum — usage inside one window only ever climbs.
    start = 0
    for i in range(1, len(pts)):
        prev, cur = pts[i - 1][1], pts[i][1]
        if cur < prev * 0.5 and cur < RESET_MAX_PCT:   # halved AND near zero = a real rollover
            start = i
    pts = pts[start:]
    if len(pts) < 2:
        return None, None
    running, hi = [], pts[0][1]
    for t, v in pts:
        hi = max(hi, v)
        running.append((t, hi))
    pts = running
    dt_hours = (pts[-1][0] - pts[0][0]) / 3600.0
    if dt_hours < max(min_span_hours, 0.05):
        return None, None   # too short a baseline; a 5-minute span extrapolates to nonsense
    burn = (pts[-1][1] - pts[0][1]) / dt_hours
    if burn <= 0:
        return round(burn, 4), None
    remaining = 100.0 - pts[-1][1]
    return round(burn, 4), pts[-1][0] + (remaining / burn) * 3600.0


def _downsample(rows: list[dict], cap: int) -> list[dict]:
    if len(rows) <= cap:
        return rows
    step = len(rows) / float(cap)
    out = [rows[int(i * step)] for i in range(cap)]
    if out and out[-1] is not rows[-1]:
        out[-1] = rows[-1]
    return out




# ------------------------------------------------------------------ generic window parsing
WINDOW_KEY_RE = re.compile(r"^(five_hour|seven_day|.*_day.*|.*_hour.*)$", re.I)


def _window_value(node) -> dict | None:
    """A window is any dict carrying a utilisation percentage (and usually a reset)."""
    if not isinstance(node, dict):
        return None
    pct = None
    for k in ("utilization", "utilisation", "used_percentage", "usedPercentage",
              "percentage", "used_pct"):
        if node.get(k) is not None:
            try:
                pct = float(node[k])
            except (TypeError, ValueError):
                pct = None
            break
    if pct is None:
        return None
    reset = None
    for k in ("resets_at", "resetsAt", "reset_at"):
        v = node.get(k)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            reset = int(v)
        else:
            try:
                reset = int(datetime.datetime.fromisoformat(
                    str(v).replace("Z", "+00:00")).timestamp())
            except Exception:
                reset = None
        break
    return {"used_pct": round(pct, 2), "resets_at_epoch": reset}


# the `limits[]` array uses its own vocabulary for the windows the keyed fields also expose
KIND_ALIAS = {"session": "five_hour", "weekly_all": "seven_day"}

# resets_at for one and the same window jitters by a few seconds between calls to Anthropic's
# usage endpoint; anything closer than this is the same window, not a rollover (5 min is far
# below the shortest window, 5 h, and far above the observed ±2 s).
RESET_JITTER_S = 300


def _windows_from_limits_array(data) -> dict:
    """The oauth/usage payload's `limits[]` array — the ONLY place the model-scoped weekly window
    appears (`weekly_scoped` + scope.model.display_name "Fable"). Keyed fields like
    seven_day_opus can be null; the array is where the real numbers live."""
    out: dict = {}
    arr = data.get("limits") if isinstance(data, dict) else None
    if not isinstance(arr, list):
        return out
    for entry in arr:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        pct = entry.get("percent")
        if not kind or pct is None:
            continue
        scope = entry.get("scope") or {}
        label = ((scope.get("model") or {}).get("display_name")
                 or (scope.get("surface") or {}).get("display_name")
                 if isinstance(scope, dict) else None)
        name = f"{kind}:{label}" if label else str(kind)
        reset = entry.get("resets_at")
        if isinstance(reset, str):
            try:
                reset = int(datetime.datetime.fromisoformat(
                    reset.replace("Z", "+00:00")).timestamp())
            except Exception:
                reset = None
        elif isinstance(reset, (int, float)):
            reset = int(reset)
        else:
            reset = None
        try:
            pctf = round(float(pct), 2)
        except (TypeError, ValueError):
            continue
        out[name] = {"used_pct": pctf, "resets_at_epoch": reset, "kind": kind,
                     "group": entry.get("group"), "severity": entry.get("severity"),
                     "is_active": bool(entry.get("is_active")),
                     "scope_model": label, "from_limits_array": True}
    return out


def windows_from_payload(data) -> dict:
    """Every usage window in a payload, whatever it is called. The statusLine payload nests them
    under `rate_limits` and carries only five_hour/seven_day; the oauth/usage endpoint carries
    keyed windows AND a `limits[]` array that holds the model-scoped ones. Both shapes land
    here; array entries win over the keyed duplicate they alias."""
    out: dict = {}
    if not isinstance(data, dict):
        return out
    for container in (data, data.get("rate_limits"), data.get("rateLimits"),
                      data.get("usage"), data.get("limits")):
        if not isinstance(container, dict):
            continue
        for key, node in container.items():
            if not WINDOW_KEY_RE.match(str(key)):
                continue
            val = _window_value(node)
            if val and key not in out:
                out[key] = val
    arr = _windows_from_limits_array(data)
    for name, val in arr.items():
        alias = KIND_ALIAS.get(val.get("kind"))
        if alias and alias in out and out[alias].get("resets_at_epoch") == val["resets_at_epoch"]:
            val["aliases"] = [alias]          # same window, richer record
            out.pop(alias)
        out[name] = val
    return out


def _windows_from_raw(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return windows_from_payload(json.loads(raw))
    except Exception:
        return {}


def _model_specific(name: str) -> str | None:
    """seven_day_opus → 'opus', weekly_scoped:Fable → 'fable'. None for all-models windows."""
    low = name.lower().replace(":", "_")
    for fam in FAMILIES:
        if low.endswith("_" + fam) or f"_{fam}_" in low:
            return fam
    return None


def collect_windows(con, days: float = HISTORY_DAYS) -> dict:
    """Merge every window seen recently. Per name we keep the newest reset window, and within it
    the highest utilisation — sessions report stale snapshots, usage only climbs."""
    merged: dict = {}
    for r in con.execute("SELECT ts, source, raw FROM rate_limit_samples WHERE ts>=? ORDER BY ts",
                         (_iso_days_ago(days),)):
        for name, val in _windows_from_raw(r["raw"]).items():
            cur = merged.get(name)
            new_reset = float(val["resets_at_epoch"] or 0)
            cur_reset = float(cur["resets_at_epoch"] or 0) if cur else 0.0
            # Anthropic stamps resets_at with sub-second jitter, so the same
            # weekly window comes back as ...:59.99, ...:00.28 or ...:01.0 on successive calls.
            # A strict "later epoch = newer window" test let ONE sample rounded to :01 pin
            # weekly_all at 38% for 31 hours while the account sat at 52%. Epochs within
            # RESET_JITTER_S are the same window; inside it a newer oauth sample is the truth
            # (it is the account's own counter) and a statusline sample only raises the figure
            # (sessions report stale snapshots).
            if cur is None or new_reset > cur_reset + RESET_JITTER_S:
                take = True
            elif abs(new_reset - cur_reset) <= RESET_JITTER_S:
                take = r["source"] == "oauth" or val["used_pct"] > cur["used_pct"]
            else:
                take = False                      # an older window: ignore
            if take:
                merged[name] = {**val, "observed_at": r["ts"], "source": r["source"]}
    for val in list(merged.values()):
        alias = KIND_ALIAS.get(val.get("kind"))
        other = merged.get(alias) if alias else None
        # Same window, two names: the oauth `limits[]` array calls it `session`/`weekly_all`
        # while the keyed payload and the statusline call it `five_hour`/`seven_day`, and the two
        # sources stamp reset epochs a few minutes apart. Requiring exact equality left both in
        # the list, so the menu bar rendered the same 5-hour window twice — once unlabelled.
        _a, _b = val.get("resets_at_epoch"), (other or {}).get("resets_at_epoch")
        _same = bool(_a) and bool(_b) and abs(float(_a) - float(_b)) <= 1800
        if other is not None and (_same or _b == _a):
            val["aliases"] = sorted(set(val.get("aliases") or []) | {alias})
            merged.pop(alias, None)          # same window seen from both sources
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    # Drop windows whose reset has already passed. A sample taken before a rollover still
    # describes the *old* window; once its resets_at is in the past it measures nothing, and
    # keeping it showed a dead "five_hour 66%" beside the live "session 3%" for the same window
    # (they never merged because alias suppression requires equal reset epochs).
    for name in [n for n, v in merged.items()
                 if (v.get("resets_at_epoch") or 0) and float(v["resets_at_epoch"]) <= now]:
        merged.pop(name)
    for name, val in merged.items():
        reset = val.get("resets_at_epoch")
        val["resets_at"] = _epoch_iso(reset)
        val["resets_at_ist"] = _epoch_ist(reset)
        val["resets_in_min"] = round((float(reset) - now) / 60.0, 1) if reset else None
        val["model"] = _model_specific(name)
    return merged


def _pick_binding(wins: dict) -> dict | None:
    """What will actually stop work: the window Claude marks `is_active`, else the fullest one."""
    if not wins:
        return None
    active = {k: v for k, v in wins.items() if v.get("is_active")}
    pool = active or wins
    name, val = max(pool.items(), key=lambda kv: kv[1]["used_pct"])
    return {"name": name, **val}


def binding_window(con=None) -> dict | None:
    """The window closest to its ceiling — what will actually stop work. The SessionStart
    nudge should lead with this: a model-scoped weekly window can run far ahead of the
    all-models weekly, so the plain seven_day number understates the real constraint."""
    own = con is None
    con = con or db.connect()
    try:
        return _pick_binding(collect_windows(con))
    finally:
        if own:
            con.close()


def _payload_extras(raw: str | None) -> dict:
    """The statusLine payload carries more than the limits — prompt-cache health, effort, the
    session's own name and context size. We keep the whole thing in `raw`; this lifts the useful
    parts out without needing new columns."""
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    pc = d.get("prompt_cache") or {}
    cw = d.get("context_window") or {}
    out = {"session_name": d.get("session_name"), "cc_version": d.get("version"),
           "cwd": (d.get("workspace") or {}).get("current_dir"),
           "effort": (d.get("effort") or {}).get("level"),
           "fast_mode": d.get("fast_mode"), "exceeds_200k": d.get("exceeds_200k_tokens"),
           "context_window_size": cw.get("context_window_size"),
           "cache_hit_ratio": round(pc["hit_ratio"], 4) if isinstance(pc.get("hit_ratio"), (int, float)) else None,
           "cache_ttl": pc.get("ttl"), "cache_misses": pc.get("misses"),
           "cache_miss_causes": pc.get("miss_causes") or None}
    return {k: v for k, v in out.items() if v is not None}


def _context_of(raw: str | None) -> dict | None:
    """The context-window reading one statusLine payload carried: percent used, tokens in the
    context now (input + cache writes + cache reads of the current request) and the window size.
    `recorded` provenance: Claude Code's own figures, not the tracker's arithmetic."""
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except Exception:
        return None
    cw = d.get("context_window") if isinstance(d, dict) else None
    if not isinstance(cw, dict):
        return None
    cu = cw.get("current_usage") if isinstance(cw.get("current_usage"), dict) else {}
    parts = [cu.get(k) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
    parts = [v for v in parts if isinstance(v, (int, float)) and not isinstance(v, bool)]
    used = sum(parts) if parts else None
    size = cw.get("context_window_size")
    pct = cw.get("used_percentage")
    if pct is None and used is not None and isinstance(size, (int, float)) and size:
        pct = round(100.0 * used / size, 1)
    if pct is None and used is None:
        return None
    return {"used_pct": pct, "tokens_used": used, "window_size": size,
            "remaining_pct": cw.get("remaining_percentage")}


def context_block(con, now: float, hours: float = 6.0, limit: int = 12) -> dict:
    """Per-session context-window fill, newest statusLine sample per session in the last `hours`."""
    sessions = []
    for r in con.execute(
            "SELECT session_id, max(ts) ts, model, raw FROM rate_limit_samples "
            "WHERE ts>=? AND session_id IS NOT NULL AND raw LIKE '%context_window%' "
            "GROUP BY session_id ORDER BY ts DESC LIMIT ?", (_iso_days_ago(hours / 24.0), limit)):
        c = _context_of(r["raw"])
        if not c:
            continue
        c.update({"session_id": r["session_id"], "model": r["model"], "observed_at": r["ts"],
                  "age_minutes": round((now - (_parse_iso(r["ts"]) or now)) / 60.0, 1)})
        sessions.append(c)
    return {"latest": sessions[0] if sessions else None, "sessions": sessions,
            "window_hours": hours, "provenance": "recorded",
            "basis": "Claude Code's statusLine payload, context_window: used_percentage, "
                     "context_window_size, and current_usage input + cache creation + cache read "
                     "tokens. One reading per session, the newest."}


def _oauth_extra(con, key: str):
    """Blocks that only the oauth/usage payload carries (spend, extra_usage, breakdowns)."""
    row = con.execute("SELECT raw FROM rate_limit_samples WHERE source='oauth' "
                      "ORDER BY id DESC LIMIT 1").fetchone()
    if not row or not row["raw"]:
        return None
    try:
        return json.loads(row["raw"]).get(key)
    except Exception:
        return None


def _refresh_max_age_min() -> float:
    """Minutes an oauth sample may be before `?refresh=1` pulls a new one (config.json
    usage.oauth_refresh_min_age_min; was a hard-coded 10 — the 5-hour window moves faster)."""
    try:
        return float((db.config().get("usage") or {}).get("oauth_refresh_min_age_min", 2))
    except Exception:
        return 2.0


REFRESH_MAX_AGE_MIN = _refresh_max_age_min()


def _maybe_refresh(con) -> dict:
    """`?refresh=1`: pull fresh windows when the newest oauth sample has gone stale. Best effort —
    a disabled or failing refresh must never break the read."""
    if not (db.config().get("usage") or {}).get("oauth_refresh"):
        return {"refreshed": False, "reason": "usage.oauth_refresh is false"}
    row = con.execute("SELECT ts FROM rate_limit_samples WHERE source='oauth' "
                      "ORDER BY id DESC LIMIT 1").fetchone()
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    age = (now - (_parse_iso(row["ts"]) if row else 0)) / 60.0 if row else 1e9
    if age < _refresh_max_age_min():
        return {"refreshed": False, "reason": f"last oauth sample is {age:.1f} min old"}
    try:
        oauth_refresh(con)
        return {"refreshed": True, "age_before_min": round(age, 1)}
    except Exception as exc:
        return {"refreshed": False, "reason": str(exc)[:200]}


def get_limits(query: dict, body, match) -> tuple[int, object]:
    """The plan's real usage windows, as reported by Claude Code's statusLine payload and, when
    enabled, the oauth/usage endpoint (the only source of the model-scoped weekly window)."""
    con = db.connect()
    try:
        refresh = _maybe_refresh(con) if query.get("refresh") in ("1", "true", "yes") else None
        latest = con.execute(
            "SELECT * FROM rate_limit_samples ORDER BY ts DESC, id DESC LIMIT 1").fetchone()
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        hist_rows = _rows(con,
            "SELECT ts, five_hour_pct, seven_day_pct, spend_pct, five_hour_resets_at, "
            "seven_day_resets_at, source FROM rate_limit_samples WHERE ts>=? ORDER BY ts",
            (_iso_days_ago(HISTORY_DAYS),))
        if not latest:
            return 200, {"current": None, "sampled_at": None, "source": None, "stale": True,
                         "history": [], "forecast": {},
                         "note": "No rate-limit samples yet. They arrive automatically from "
                                 "hooks/statusline.py on the next status-line refresh of any "
                                 "Claude Code session (wire it with: tracker limits --install)."}
        lat = dict(latest)
        sampled = _parse_iso(lat["ts"]) or now
        cur: dict = {}
        for key, pct_col, reset_col in (("five_hour", "five_hour_pct", "five_hour_resets_at"),
                                        ("seven_day", "seven_day_pct", "seven_day_resets_at"),
                                        ("spend", "spend_pct", "spend_resets_at")):
            # Every running session reports its OWN last-refresh snapshot, so three sessions can
            # disagree (28% / 45% / 62%) at the same instant. Usage only climbs inside a window,
            # so the truth is the MAXIMUM seen in the current window, not the newest sample.
            reset = lat.get(reset_col)
            best = con.execute(
                f"SELECT {pct_col} pct, ts FROM rate_limit_samples WHERE {pct_col} IS NOT NULL "
                f"AND {reset_col} IS ? ORDER BY {pct_col} DESC, ts DESC LIMIT 1",
                (reset,)).fetchone()
            if not best:
                continue
            cur[key] = {"used_pct": round(best["pct"], 2), "observed_at": best["ts"],
                        "newest_sample_pct": round(lat[pct_col], 2) if lat.get(pct_col) is not None
                                             else None,
                        "resets_at": _epoch_iso(reset), "resets_at_ist": _epoch_ist(reset),
                        "resets_at_epoch": reset,
                        "resets_in_min": round((float(reset) - now) / 60.0, 1) if reset else None,
                        # the percentage is Claude's own; the pace built on it is ours
                        "provenance": "official",
                        "source": lat.get("source") or "statusline"}
            cur[key]["pace"] = window_pace(key, cur[key], now)
        # forecasts: only samples from the window the latest sample belongs to
        same5 = [h for h in hist_rows
                 if h.get("five_hour_resets_at") == lat.get("five_hour_resets_at")]
        same7 = [h for h in hist_rows
                 if h.get("seven_day_resets_at") == lat.get("seven_day_resets_at")]
        burn5, hit5 = _burn(same5 or hist_rows, "five_hour_pct", "five_hour_resets_at", 0.25)
        burn7, hit7 = _burn(same7 or hist_rows, "seven_day_pct", "seven_day_resets_at", 2.0)
        reset5 = lat.get("five_hour_resets_at")
        reset7 = lat.get("seven_day_resets_at")
        forecast = {
            "burn_pct_per_hour_5h": burn5,
            "burn_pct_per_day_7d": round(burn7 * 24, 3) if burn7 is not None else None,
            "five_hour_hits_100_at": _epoch_iso(hit5),
            "five_hour_hits_100_at_ist": _epoch_ist(hit5),
            "five_hour_hits_100_before_reset": bool(hit5 and reset5 and hit5 < float(reset5)),
            "seven_day_hits_100_at": _epoch_iso(hit7),
            "seven_day_hits_100_at_ist": _epoch_ist(hit7),
            "seven_day_hits_100_before_reset": bool(hit7 and reset7 and hit7 < float(reset7)),
            "samples_in_5h_window": len(same5), "samples_in_7d_window": len(same7),
            "min_span_hours": {"five_hour": 0.25, "seven_day": 2.0},
        }
        per_session = []
        for r in con.execute(
                "SELECT session_id, max(ts) ts, model, five_hour_pct, seven_day_pct, context_pct, "
                "cost_usd, raw FROM rate_limit_samples WHERE ts>=? AND session_id IS NOT NULL "
                "GROUP BY session_id ORDER BY ts DESC LIMIT 12", (_iso_days_ago(1 / 24.0),)):
            row = dict(r)
            extras = _payload_extras(row.pop("raw", None))
            row.update(extras)
            row["age_minutes"] = round((now - (_parse_iso(row["ts"]) or now)) / 60.0, 1)
            per_session.append(row)
        all_windows = collect_windows(con)
        for _name, _w in all_windows.items():
            _w["provenance"] = "official"          # straight out of Claude's limits[] / rate_limits
            _w["pace"] = window_pace(_name, _w, now)
        history = _downsample([{"ts": h["ts"], "five_hour_pct": h["five_hour_pct"],
                                "seven_day_pct": h["seven_day_pct"], "spend_pct": h["spend_pct"]}
                               for h in hist_rows], HISTORY_MAX_POINTS)
        return 200, {
            "current": cur, "sampled_at": lat["ts"], "sampled_at_ist": _epoch_ist(sampled),
            "source": lat.get("source") or "statusline", "model": lat.get("model"),
            "session_id": lat.get("session_id"), "context_pct": lat.get("context_pct"),
            "context": context_block(con, now),
            "age_minutes": round((now - sampled) / 60.0, 1),
            "stale": (now - sampled) > STALE_MINUTES * 60,
            "stale_after_minutes": STALE_MINUTES,
            "refresh": refresh,
            "history": history, "history_days": HISTORY_DAYS,
            "sessions_last_hour": per_session,
            "windows": all_windows,
            "binding_window": _pick_binding(all_windows),
            "seven_day_breakdown": _oauth_extra(con, "seven_day_breakdown"),
            "extra_usage": _oauth_extra(con, "extra_usage"),
            "spend": _oauth_extra(con, "spend"),
            "model_specific_available": any(v.get("model") for v in all_windows.values()),
            "model_specific_hint": None if any(v.get("model") for v in all_windows.values()) else
                "Model-specific weekly limits (the Fable weekly window) need "
                "usage.oauth_refresh: true in config.json — it sends your Claude Code OAuth "
                "token to api.anthropic.com only.",
            "payload_extras": _payload_extras(lat.get("raw")),
            "total_samples": con.execute(
                "SELECT count(*) c FROM rate_limit_samples").fetchone()["c"],
            "forecast": forecast,
            "pace": {
                "binding": (_pick_binding(all_windows) or {}).get("pace"),
                "windows": {n: w.get("pace") for n, w in all_windows.items() if w.get("pace")},
                "tolerance_pct": PACE_TOLERANCE_PCT,
                "why": "A level without a pace is not a decision. 87% means one thing 20 minutes "
                       "into a window and the opposite 20 minutes before it resets.",
            },
            "provenance": {
                "basis": PROVENANCE_BASIS,
                "fields": {
                    "current.*.used_pct": "official",
                    "current.*.resets_at": "official",
                    "windows.*.used_pct": "official",
                    "windows.*.severity": "official",
                    "current.*.pace": "estimate",
                    "windows.*.pace": "estimate",
                    "forecast.*": "estimate",
                    "sessions_last_hour.*.cost_usd": "recorded",
                    "context.*": "recorded",
                    "spend": "official",
                    "extra_usage": "official",
                },
                "sampled_from": lat.get("source") or "statusline",
            },
            "note": "Percentages are Claude's own (statusLine payload / oauth usage endpoint) and "
                    "are labelled `official`. Everything derived from them — pace, burn rate, "
                    "forecast — is labelled `estimate`. See the `provenance` block.",
        }
    finally:
        con.close()


# ------------------------------------------------------------------ optional OAuth refresh
def _keychain_token() -> str | None:
    """Read the Claude Code OAuth access token from the login keychain. Never stored, never
    logged, never returned to an API caller — only used for the outbound request below."""
    from modules.common import run_cmd
    import oscompat
    if oscompat.IS_MAC:
        rc, out, _ = run_cmd(["security", "find-generic-password", "-s", "Claude Code-credentials",
                              "-w"], timeout=6)
    else:  # Windows / Linux: Claude Code keeps the login in ~/.claude/.credentials.json
        try:
            rc, out = 0, oscompat.credentials_file().read_text(encoding="utf-8")
        except OSError:
            rc, out = 1, ""
    if rc != 0 or not out.strip():
        return None
    try:
        return ((json.loads(out.strip()).get("claudeAiOauth") or {}).get("accessToken")) or None
    except Exception:
        return None


def oauth_refresh(con=None, _fetch=None) -> dict:
    """Ask Anthropic for the current usage windows when no session is running.
    OFF unless config.json sets usage.oauth_refresh = true. This is the ONLY thing in the whole
    tracker that talks to the network, and it sends your own OAuth token to Anthropic's own API
    and nothing else. `_fetch` is a seam for tests."""
    cfg = (db.config().get("usage") or {})
    if not cfg.get("oauth_refresh"):
        raise ApiError(403, "usage.oauth_refresh is false in config.json — on-demand refresh is "
                            "disabled. The status line samples limits automatically.")
    fetch = _fetch or _fetch_oauth_usage
    token = _keychain_token()
    if not token:
        raise ApiError(404, "no Claude Code OAuth token in the login keychain / ~/.claude/.credentials.json "
                            '(security find-generic-password -s "Claude Code-credentials")')
    data = fetch(token)
    if not isinstance(data, dict):
        raise ApiError(502, "unexpected response from the usage endpoint")
    own = con is None
    con = con or db.connect()
    try:
        sample = _sample_from_oauth(data)
        if not sample:
            raise ApiError(502, "usage response carried no rate-limit windows")
        con.execute(
            "INSERT INTO rate_limit_samples(ts,session_id,model,five_hour_pct,five_hour_resets_at,"
            "seven_day_pct,seven_day_resets_at,spend_pct,spend_resets_at,context_pct,cost_usd,"
            "source,raw) VALUES(?,?,NULL,?,?,?,?,?,?,NULL,NULL,'oauth',?)",
            (db.now_iso(), None, sample["five_hour_pct"], sample["five_hour_resets_at"],
             sample["seven_day_pct"], sample["seven_day_resets_at"], sample["spend_pct"],
             sample["spend_resets_at"], json.dumps(data)[:8000]))
        return {"stored": True, "source": "oauth", "windows": windows_from_payload(data),
                **sample}
    finally:
        if own:
            con.close()


def _fetch_oauth_usage(token: str) -> dict:
    import urllib.request
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "User-Agent": "ai-command-center/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _sample_from_oauth(data: dict) -> dict | None:
    """Map the /api/oauth/usage payload onto our columns, tolerating key drift."""
    def pick(*paths):
        for path in paths:
            node = data
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
                if node is None:
                    break
            if node is not None:
                return node
        return None

    def pct(node):
        if isinstance(node, dict):
            for k in ("utilization", "used_percentage", "usedPercentage", "percentage"):
                if node.get(k) is not None:
                    try:
                        return float(node[k])
                    except (TypeError, ValueError):
                        return None
        return None

    def reset(node):
        if isinstance(node, dict):
            for k in ("resets_at", "resetsAt", "reset_at"):
                v = node.get(k)
                if v is None:
                    continue
                if isinstance(v, (int, float)):
                    return int(v)
                try:
                    return int(datetime.datetime.fromisoformat(
                        str(v).replace("Z", "+00:00")).timestamp())
                except Exception:
                    return None
        return None

    five = pick(("five_hour",), ("rate_limits", "five_hour"), ("fiveHour",))
    seven = pick(("seven_day",), ("rate_limits", "seven_day"), ("sevenDay",))
    spend = pick(("spend_limit",), ("rate_limits", "spend_limit"), ("spendLimit",))
    if five is None and seven is None:
        return None
    return {"five_hour_pct": pct(five), "five_hour_resets_at": reset(five),
            "seven_day_pct": pct(seven), "seven_day_resets_at": reset(seven),
            "spend_pct": pct(spend), "spend_resets_at": reset(spend)}


def post_refresh(query: dict, body, match) -> tuple[int, object]:
    return 200, oauth_refresh()


# ==========================================================================================
# Comprehensive analysis
# ==========================================================================================
def _analysis_pace(con, days: int) -> dict:
    """Pace for the live limit windows, plus today's spend against the recent daily average.

    The limit windows are the same objects /api/usage/limits returns, so the two endpoints can
    never disagree about how far through a window we are. Today's pace is a second, cheaper
    question: at this hour of the day, is today running hot or cold against the last fortnight?
    """
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    try:
        wins = collect_windows(con)
    except Exception:
        wins = {}
    paces = {n: window_pace(n, w, now) for n, w in wins.items()}
    paces = {n: v for n, v in paces.items() if v}
    binding = _pick_binding(wins) if wins else None
    ist_now = datetime.datetime.now(IST)
    today = ist_now.strftime("%Y-%m-%d")
    hour = ist_now.hour + ist_now.minute / 60.0
    TOK = "input_tokens+output_tokens+cache_read+cache_create"
    rows = _rows(con, f"""
        SELECT date(ts,'+5 hours','+30 minutes') d,
               sum({TOK}) tokens, round(sum(cost_usd_est),4) cost
        FROM usage_events WHERE ts>=? GROUP BY d ORDER BY d""", (_iso_days_ago(14),))
    prior = [r for r in rows if r["d"] != today]
    today_row = next((r for r in rows if r["d"] == today), None)
    avg_cost = round(sum(r["cost"] or 0 for r in prior) / len(prior), 4) if prior else None
    # what fraction of a typical day's spend has usually happened by this hour
    so_far = round((today_row or {}).get("cost") or 0.0, 4)
    expected = round(avg_cost * min(1.0, hour / 24.0), 4) if avg_cost else None
    return {
        "windows": paces,
        "binding": {"name": binding["name"], **(paces.get(binding["name"]) or {})}
                   if binding and paces.get(binding.get("name")) else None,
        "today": {
            "date_ist": today, "hour_ist": round(hour, 2),
            "cost_usd_est_so_far": so_far,
            "avg_daily_cost_usd_est_14d": avg_cost,
            "expected_by_this_hour_usd_est": expected,
            "vs_expected_pct": round(100.0 * (so_far / expected - 1.0), 1)
                               if expected else None,
            "verdict": (None if not expected else
                        "hot" if so_far > expected * 1.25 else
                        "cold" if so_far < expected * 0.75 else "normal"),
            "basis": "a flat hour-of-day profile: a typical day's estimated spend prorated by "
                     "how much of the local day has passed. Crude on purpose — it answers "
                     "'is today unusual', not 'what will today cost'.",
            "provenance": "estimate",
        },
        "tolerance_pct": PACE_TOLERANCE_PCT,
    }


def get_analysis(query: dict, body, match) -> tuple[int, object]:
    days = _int(query, "days", 30, 1, 400)
    since = _iso_days_ago(days)
    con = db.connect()
    try:
        TOK = "input_tokens+output_tokens+cache_read+cache_create"
        by_day = _rows(con, f"""
            SELECT substr(ts,1,10) day, family, sum(input_tokens) input_tokens,
                   sum(output_tokens) output_tokens, sum(cache_read) cache_read,
                   sum(cache_create) cache_create, sum({TOK}) tokens,
                   round(sum(cost_usd_est),4) cost_usd_est, count(*) calls,
                   count(DISTINCT session_id) sessions
            FROM usage_events WHERE ts>=? GROUP BY day, family ORDER BY day, family""", (since,))
        recorded_by_day = {r["day"]: r["c"] for r in con.execute(
            "SELECT substr(started_at,1,10) day, round(sum(cost_usd),4) c FROM sessions "
            "WHERE cost_usd IS NOT NULL AND started_at>=? GROUP BY day", (since,))}
        by_week = _rows(con, f"""
            SELECT strftime('%Y-W%W', ts) week, min(substr(ts,1,10)) week_start,
                   sum({TOK}) tokens, round(sum(cost_usd_est),4) cost_usd_est, count(*) calls,
                   count(DISTINCT session_id) sessions, count(DISTINCT substr(ts,1,10)) active_days
            FROM usage_events WHERE ts>=? GROUP BY week ORDER BY week""", (since,))
        by_hour = _rows(con, f"""
            SELECT cast(strftime('%H', ts, '+5 hours', '+30 minutes') AS INTEGER) hour_ist,
                   count(*) calls, sum({TOK}) tokens, round(sum(cost_usd_est),4) cost_usd_est
            FROM usage_events WHERE ts>=? GROUP BY hour_ist ORDER BY hour_ist""", (since,))
        weekday_names = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
        by_weekday = _rows(con, f"""
            SELECT cast(strftime('%w', ts, '+5 hours', '+30 minutes') AS INTEGER) weekday,
                   count(*) calls, sum({TOK}) tokens, round(sum(cost_usd_est),4) cost_usd_est,
                   count(DISTINCT substr(ts,1,10)) days
            FROM usage_events WHERE ts>=? GROUP BY weekday ORDER BY weekday""", (since,))
        for r in by_weekday:
            r["name"] = weekday_names[r["weekday"] % 7]
        by_model = _rows(con, f"""
            SELECT model, family, count(*) calls, sum(input_tokens) input_tokens,
                   sum(output_tokens) output_tokens, sum(cache_read) cache_read,
                   sum(cache_create) cache_create, sum({TOK}) tokens,
                   round(sum(cost_usd_est),4) cost_usd_est,
                   count(DISTINCT session_id) sessions
            FROM usage_events WHERE ts>=? GROUP BY model ORDER BY cost_usd_est DESC""", (since,))
        by_project = _rows(con, f"""
            SELECT coalesce(project,'unknown') project, count(DISTINCT session_id) sessions,
                   sum({TOK}) tokens, sum(output_tokens) output_tokens,
                   round(sum(cost_usd_est),4) cost_usd_est, count(*) calls
            FROM usage_events WHERE ts>=? GROUP BY project ORDER BY cost_usd_est DESC""", (since,))
        # ---- recorded cost, rolled up exactly the way the estimate is ------------------
        # Claude Code records a cost per SESSION (sessions.cost_usd, out of its cost-state
        # records); there is no recorded figure per event, per project or per hour. So each
        # session's recorded cost is split across its own events in proportion to their
        # estimated cost. That allocation conserves the session's total, attributes it to the
        # same groups the estimate uses (usage_events.project / .model / its hour), and scales
        # down when only part of a session falls inside the window. A group with no recorded
        # session gets null — never a zero, and never a part-sum dressed as a total.
        ALLOC = """
            WITH est_all AS (
              SELECT session_id, sum(cost_usd_est) est FROM usage_events GROUP BY session_id),
            alloc AS (
              SELECT u.project, u.model, u.ts, u.session_id,
                     s.cost_usd * u.cost_usd_est / nullif(e.est,0) rec
              FROM usage_events u
              JOIN sessions s ON s.session_id=u.session_id
              JOIN est_all e ON e.session_id=u.session_id
              WHERE s.cost_usd IS NOT NULL AND s.cost_usd>0 AND u.ts>=?)
        """

        def _recorded(group_expr: str) -> dict:
            return {r["k"]: (r["rec"], r["n"]) for r in con.execute(
                ALLOC + f"SELECT {group_expr} k, round(sum(rec),4) rec, "
                        "count(DISTINCT session_id) n FROM alloc GROUP BY k", (since,))}

        rec_by_project = _recorded("coalesce(project,'unknown')")
        rec_by_model = _recorded("coalesce(model,'unknown')")
        rec_by_hour = _recorded("cast(strftime('%H', ts, '+5 hours', '+30 minutes') AS INTEGER)")
        rec_total = round(sum((v[0] or 0) for v in rec_by_project.values()), 4)
        # One session can touch several projects, so the per-group counts cannot be summed.
        rec_sessions = con.execute(
            ALLOC + "SELECT count(DISTINCT session_id) n FROM alloc", (since,)).fetchone()["n"]

        def _attach(rows: list, lookup: dict, key: str, default="unknown") -> None:
            for r in rows:
                hit = lookup.get(r.get(key) if r.get(key) is not None else default)
                r["cost_usd_recorded"] = hit[0] if hit else None
                r["recorded_sessions"] = hit[1] if hit else 0

        _attach(by_project, rec_by_project, "project")
        _attach(by_model, rec_by_model, "model")
        _attach(by_hour, rec_by_hour, "hour_ist", default=-1)

        grand = sum(p["cost_usd_est"] or 0 for p in by_project) or 1.0
        for p in by_project:
            p["avg_cost_per_session"] = round((p["cost_usd_est"] or 0) / max(p["sessions"], 1), 4)
            p["share_pct"] = round(100.0 * (p["cost_usd_est"] or 0) / grand, 2)
        by_session = _rows(con, f"""
            SELECT u.session_id, s.title, coalesce(s.project,u.project) project,
                   substr(coalesce(s.started_at,min(u.ts)),1,10) date,
                   min(u.ts) first_call, max(u.ts) last_call,
                   sum(u.input_tokens) input_tokens, sum(u.output_tokens) output_tokens,
                   sum(u.cache_read) cache_read, sum(u.cache_create) cache_create,
                   sum({TOK.replace('input_tokens','u.input_tokens').replace('output_tokens','u.output_tokens').replace('cache_read','u.cache_read').replace('cache_create','u.cache_create')}) tokens,
                   round(sum(u.cost_usd_est),4) cost_usd_est, s.cost_usd cost_usd_recorded,
                   count(*) calls, s.user_turns, s.model, s.started_at, s.ended_at
            FROM usage_events u LEFT JOIN sessions s ON s.session_id=u.session_id
            WHERE u.ts>=? GROUP BY u.session_id
            ORDER BY coalesce(s.cost_usd, sum(u.cost_usd_est)) DESC LIMIT 30""", (since,))
        for r in by_session:
            t0, t1 = _parse_iso(r["first_call"]), _parse_iso(r["last_call"])
            r["duration_min"] = round((t1 - t0) / 60.0, 1) if t0 and t1 else None
            turns = r["user_turns"] or 0
            r["cost_per_user_turn"] = round((r["cost_usd_est"] or 0) / turns, 4) if turns else None
        cache_trend = _rows(con, """
            SELECT substr(ts,1,10) day, sum(cache_read) cache_read,
                   sum(input_tokens+cache_read+cache_create) input_side,
                   round(100.0*sum(cache_read)/nullif(sum(input_tokens+cache_read+cache_create),0),2)
                       cache_hit_pct
            FROM usage_events WHERE ts>=? GROUP BY day ORDER BY day""", (since,))
        # what is eating the weekly Fable window right now
        since7 = _iso_days_ago(7)
        fable_sessions = _rows(con, f"""
            SELECT u.session_id, s.title, coalesce(s.project,u.project) project,
                   sum({TOK.replace('input_tokens','u.input_tokens').replace('output_tokens','u.output_tokens').replace('cache_read','u.cache_read').replace('cache_create','u.cache_create')}) tokens,
                   round(sum(u.cost_usd_est),4) cost_usd_est
            FROM usage_events u LEFT JOIN sessions s ON s.session_id=u.session_id
            WHERE u.ts>=? AND u.family='fable' GROUP BY u.session_id
            ORDER BY tokens DESC LIMIT 10""", (since7,))
        fable_projects = _rows(con, f"""
            SELECT coalesce(project,'unknown') project, sum({TOK}) tokens,
                   round(sum(cost_usd_est),4) cost_usd_est, count(DISTINCT session_id) sessions
            FROM usage_events WHERE ts>=? AND family='fable'
            GROUP BY project ORDER BY tokens DESC""", (since7,))
        fam7 = _rows(con, f"SELECT family, sum({TOK}) tokens, round(sum(cost_usd_est),4) "
                          f"cost_usd_est FROM usage_events WHERE ts>=? GROUP BY family", (since7,))
        tot7 = sum(f["tokens"] or 0 for f in fam7) or 1
        fable_tok = sum(f["tokens"] or 0 for f in fam7 if f["family"] == "fable")
        longest = _rows(con, """
            SELECT session_id, title, project, started_at, ended_at, cost_usd, user_turns,
                   round((julianday(replace(ended_at,'Z','')) -
                          julianday(replace(started_at,'Z','')))*24, 2) hours
            FROM sessions WHERE started_at IS NOT NULL AND ended_at IS NOT NULL
              AND started_at>=? ORDER BY hours DESC LIMIT 10""", (since,))
        top_hour = con.execute(f"""
            SELECT substr(ts,1,13) hour_utc, count(*) calls, sum({TOK}) tokens,
                   round(sum(cost_usd_est),4) cost_usd_est FROM usage_events
            GROUP BY hour_utc ORDER BY cost_usd_est DESC LIMIT 1""").fetchone()
        active_days = [r[0] for r in con.execute(
            "SELECT DISTINCT substr(ts,1,10) d FROM usage_events ORDER BY d")]
        streak_best = streak_cur = 0
        prev = None
        for d in active_days:
            cur_dt = datetime.datetime.strptime(d, "%Y-%m-%d")
            streak_cur = streak_cur + 1 if prev and (cur_dt - prev).days == 1 else 1
            streak_best = max(streak_best, streak_cur)
            prev = cur_dt
        today = datetime.datetime.now(IST).strftime("%Y-%m-%d")
        if active_days and (datetime.datetime.strptime(today, "%Y-%m-%d")
                            - datetime.datetime.strptime(active_days[-1], "%Y-%m-%d")).days > 1:
            streak_cur = 0
        alltime = con.execute(f"""
            SELECT count(*) calls, count(DISTINCT session_id) sessions, sum({TOK}) tokens,
                   sum(output_tokens) output_tokens, round(sum(cost_usd_est),4) cost_usd_est,
                   min(ts) first_event, max(ts) last_event,
                   count(DISTINCT substr(ts,1,10)) active_days FROM usage_events""").fetchone()
        cal = con.execute("""
            SELECT count(*) sessions, round(sum(est),4) est, round(sum(rec),4) rec FROM (
              SELECT sum(u.cost_usd_est) est, s.cost_usd rec FROM usage_events u
              JOIN sessions s ON s.session_id=u.session_id
              WHERE s.cost_usd IS NOT NULL AND s.cost_usd>0 GROUP BY u.session_id)""").fetchone()
        ratio = round((cal["est"] or 0) / cal["rec"], 4) if cal and cal["rec"] else None
        for d in by_day:
            d["cost_usd_recorded_day"] = recorded_by_day.get(d["day"])
        return 200, {
            "days": days, "since": since, "hour_timezone": TZ_LABEL,
            "by_day": by_day, "by_week": by_week, "by_hour_of_day": by_hour,
            "by_weekday": by_weekday, "by_model": by_model, "by_project": by_project,
            "by_session": by_session, "cache_hit_ratio_trend": cache_trend,
            "fable_share_of_7d": {
                "window_since": since7, "fable_tokens": fable_tok,
                "share_pct": round(100.0 * fable_tok / tot7, 2),
                "by_family": fam7, "top_sessions": fable_sessions, "by_project": fable_projects},
            "longest_sessions": longest,
            "most_expensive_hour": dict(top_hour) if top_hour else None,
            "streaks": {"current_days": streak_cur, "longest_days": streak_best,
                        "active_days_total": len(active_days),
                        "first_day": active_days[0] if active_days else None,
                        "last_day": active_days[-1] if active_days else None},
            "totals_all_time": dict(alltime) if alltime else {},
            "cost_calibration": {"sessions_compared": cal["sessions"] if cal else 0,
                                 "est_total": cal["est"] if cal else None,
                                 "recorded_total": cal["rec"] if cal else None,
                                 "est_over_recorded": ratio},
            "recorded_totals": {"window": rec_total, "sessions": rec_sessions},
            "pace": _analysis_pace(con, days),
            "provenance": {
                "basis": PROVENANCE_BASIS,
                "fields": {
                    "*.input_tokens": "recorded", "*.output_tokens": "recorded",
                    "*.cache_read": "recorded", "*.cache_create": "recorded",
                    "*.tokens": "recorded", "*.calls": "recorded",
                    "*.cost_usd_est": "estimate",
                    "*.cost_usd_recorded": "recorded",
                    "by_day.*.cost_usd_recorded_day": "recorded",
                    "by_project.*.share_pct": "estimate",
                    "by_project.*.avg_cost_per_session": "estimate",
                    "by_session.*.cost_per_user_turn": "estimate",
                    "by_session.*.duration_min": "recorded",
                    "cache_hit_ratio_trend.*.cache_hit_pct": "recorded",
                    "cost_calibration.est_over_recorded": "estimate",
                    "recorded_totals.*": "recorded",
                    "streaks.*": "recorded",
                    "pace.windows.*": "estimate",
                    "pace.today.*": "estimate",
                },
                "headline": "Every token count here is Claude's own per-response `message.usage` "
                            "(recorded). Every dollar marked _est is the tracker's arithmetic "
                            "(estimate). Plan percentages are at /api/usage/limits and are "
                            "`official`. Nothing is blended.",
            },
            "cost_basis": "cost_usd_est is an estimate; cost_usd_recorded comes from Claude Code's "
                          "cost-state records. Plan-limit percentages live at /api/usage/limits. "
                          "Every field's basis is labelled in the `provenance` block.",
            "recorded_basis": "Claude Code records a cost per session, not per call. A group's "
                              "cost_usd_recorded is each session's recorded cost split across its "
                              "own calls in proportion to their estimate, so the totals add up and "
                              "part of a session outside the window is left out. recorded_sessions "
                              "says how many sessions carried a recorded cost; null means none did.",
        }
    finally:
        con.close()




# ==========================================================================================
# Provenance and pace
# ==========================================================================================
# An unlabelled estimate shown as fact is how a wrong number becomes somebody's constant, and
# a level without a pace is not a decision: "87% used" means one thing 20 minutes into a
# 5-hour window and the opposite 20 minutes before it resets. Every figure this module returns
# now says which of three things it is, and every limit window carries how far through itself
# it is.
PROVENANCE_BASIS = {
    "official": "Claude's own quota numbers, as Claude reported them — the statusLine payload's "
                "`rate_limits` block and the oauth usage endpoint's `limits[]` array. Nothing "
                "here is computed by the tracker.",
    "recorded": "A figure Claude Code wrote down for itself and we read back: per-session "
                "`cost-state.totalCostUSD`, and the per-response `message.usage` token counts "
                "in the transcript. Measured, not modelled — but ours only by transcription.",
    "estimate": "The tracker's own arithmetic: dollar costs from the price table in "
                "modules/usage.py, burn rates, pace, forecasts, and the split of a session's "
                "recorded cost across its calls. Useful, and wrong by some amount.",
}

# How long each kind of window is, so "how far through it are we" can be answered from the
# reset time Claude gives us. Claude reports `resets_at` but never the window's length.
WINDOW_SECONDS = {
    "session": 5 * 3600, "five_hour": 5 * 3600,
    "weekly_all": 7 * 86400, "weekly_scoped": 7 * 86400, "seven_day": 7 * 86400,
}
PACE_TOLERANCE_PCT = 5.0      # inside this band of the linear line, call it "on pace"


def _window_seconds(name: str, win: dict) -> int | None:
    """Window length in seconds, from its kind, its group, or its name. None when unknown —
    and an unknown length means no pace at all rather than a guessed one."""
    for key in (win.get("kind"), name):
        if key and str(key).split(":")[0] in WINDOW_SECONDS:
            return WINDOW_SECONDS[str(key).split(":")[0]]
    if win.get("group") == "weekly":
        return 7 * 86400
    if win.get("group") == "session":
        return 5 * 3600
    low = str(name).lower()
    if "hour" in low:
        return 5 * 3600
    if "day" in low or "week" in low:
        return 7 * 86400
    return None


def window_pace(name: str, win: dict, now: float | None = None) -> dict | None:
    """Level plus pace for one limit window.

    "87% used with 34% of the window elapsed" is a decision; "87%" alone is not. Returns the
    elapsed fraction, the straight-line position you would be at if you spent evenly, how far
    ahead or behind that you actually are, the average burn so far, and where this pace lands
    at the reset. All of it is `estimate`: only `used_pct` and `resets_at` are official.
    """
    reset = win.get("resets_at_epoch")
    dur = _window_seconds(name, win)
    used = win.get("used_pct")
    if not reset or not dur or used is None:
        return None
    now = now if now is not None else datetime.datetime.now(datetime.timezone.utc).timestamp()
    started = float(reset) - dur
    elapsed = max(0.0, min(float(dur), now - started))
    frac = elapsed / dur
    remaining_h = max(0.0, (float(reset) - now) / 3600.0)
    elapsed_h = elapsed / 3600.0
    linear = round(frac * 100.0, 1)
    delta = round(float(used) - linear, 1)
    if delta > PACE_TOLERANCE_PCT:
        verdict, phrase = "ahead", "ahead of a linear pace"
    elif delta < -PACE_TOLERANCE_PCT:
        verdict, phrase = "behind", "behind a linear pace"
    else:
        verdict, phrase = "on_pace", "on a linear pace"
    burn = round(float(used) / elapsed_h, 2) if elapsed_h > 0.05 else None
    projected = round(float(used) / frac, 1) if frac > 0.02 else None
    affordable = round((100.0 - float(used)) / remaining_h, 2) if remaining_h > 0.05 else None
    return {
        "window_started_at": _epoch_iso(started),
        "window_started_at_ist": _epoch_ist(started),
        "window_hours": round(dur / 3600.0, 2),
        "elapsed_fraction": round(frac, 4),
        "elapsed_pct": linear,
        "elapsed_hours": round(elapsed_h, 2),
        "remaining_hours": round(remaining_h, 2),
        "used_pct": round(float(used), 1),
        "vs_linear_pct": delta,
        "verdict": verdict,
        "burn_pct_per_hour": burn,
        "projected_pct_at_reset": projected,
        "will_exhaust_before_reset": bool(projected is not None and projected >= 100.0),
        "affordable_pct_per_hour": affordable,
        "sentence": (f"{float(used):.0f}% used with {linear:.0f}% of the window elapsed — "
                     f"{abs(delta):.0f} point{'' if round(abs(delta)) == 1 else 's'} {phrase}"
                     + (f"; on this pace it reaches {projected:.0f}% by the reset"
                        if projected is not None else "")
                     + (f". {affordable:.1f}%/h keeps you inside it."
                        if affordable is not None and float(used) < 100 else ".")),
        "provenance": "estimate",
        "basis": "elapsed fraction is derived from Claude's own resets_at and the known length "
                 "of this window kind; everything else is the tracker's arithmetic on it.",
    }


# ==========================================================================================
# Coverage — is the history still complete?
# ==========================================================================================
# Claude Code deletes transcripts on a rolling ~30-day window. Anything not indexed by day 30
# is gone forever and cannot be reconstructed. The ledger already holds the mined history; what
# was missing was any way to see a hole. This endpoint makes a gap loud instead of silent:
# first and last indexed day, every day in between with no usage events, how many transcript
# files are on disk right now, and how many of those sessions we actually hold.
COVERAGE_LOOKBACK_DAYS = 400
# Transcript files live at ~/.claude/projects/<project>/<session-uuid>.jsonl, and a session's
# subagent/workflow transcripts under directories beside it (<session-uuid>/…, subagents/…,
# wf_…/…). Only a UUID component names a session; "subagents" and "wf_xxxx" do not, and reading
# them as session ids invented three sessions that were never missing.
SESSION_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _session_id_of(rel_parts: tuple) -> str | None:
    """Session id from a transcript's path components relative to the projects dir."""
    for part in rel_parts[1:]:                       # [0] is the project directory
        cand = part[:-6] if part.endswith(".jsonl") else part
        if SESSION_UUID_RE.match(cand):
            return cand
    return None


def _projects_dir() -> Path:
    return Path(db.config().get("projects_dir") or (Path.home() / ".claude/projects")).expanduser()


def _disk_transcripts(root: Path) -> dict:
    """Every *.jsonl under the projects dir, with its session id and mtime. stat() only — no
    file is opened, so this stays a few milliseconds even at 800+ files."""
    out = {"files": 0, "bytes": 0, "sessions": set(), "oldest_mtime": None, "newest_mtime": None,
           "subagent_files": 0, "unidentified_files": 0, "exists": root.is_dir()}
    if not out["exists"]:
        return out
    for f in root.rglob("*.jsonl"):
        try:
            st = f.stat()
        except OSError:
            continue
        out["files"] += 1
        out["bytes"] += st.st_size
        # <proj>/<sid>.jsonl is the session itself; anything deeper is one of its agents.
        if f.parent.parent != root:
            out["subagent_files"] += 1
        sid = _session_id_of(f.relative_to(root).parts)
        if sid:
            out["sessions"].add(sid)
        else:
            out["unidentified_files"] = out.get("unidentified_files", 0) + 1
        m = st.st_mtime
        out["oldest_mtime"] = m if out["oldest_mtime"] is None else min(out["oldest_mtime"], m)
        out["newest_mtime"] = m if out["newest_mtime"] is None else max(out["newest_mtime"], m)
    return out


def _day_range(first: str, last: str) -> list[str]:
    d0 = datetime.datetime.strptime(first, "%Y-%m-%d").date()
    d1 = datetime.datetime.strptime(last, "%Y-%m-%d").date()
    return [(d0 + datetime.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def get_coverage(query: dict, body, match) -> tuple[int, object]:
    """What the permanent record actually covers, and where it does not."""
    con = db.connect()
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        span = con.execute(
            "SELECT min(substr(ts,1,10)) first_day, max(substr(ts,1,10)) last_day, "
            "count(*) events, count(DISTINCT session_id) sessions, "
            "count(DISTINCT substr(ts,1,10)) indexed_days FROM usage_events").fetchone()
        first_day, last_day = span["first_day"], span["last_day"]
        per_day = {r["d"]: r["n"] for r in con.execute(
            "SELECT substr(ts,1,10) d, count(*) n FROM usage_events GROUP BY d")}

        gaps, expected_days = [], []
        if first_day and last_day:
            expected_days = _day_range(first_day, last_day)
            run: list[str] = []
            for d in expected_days:
                if per_day.get(d):
                    if run:
                        gaps.append({"from": run[0], "to": run[-1], "days": len(run)})
                        run = []
                else:
                    run.append(d)
            if run:
                gaps.append({"from": run[0], "to": run[-1], "days": len(run)})

        disk = _disk_transcripts(_projects_dir())
        disk_sessions = disk["sessions"]
        held = {r["session_id"] for r in con.execute("SELECT session_id FROM sessions")}
        usage_sessions = {r["session_id"] for r in con.execute(
            "SELECT DISTINCT session_id FROM usage_events WHERE session_id IS NOT NULL")}
        try:
            text_sessions = {r["session_id"] for r in con.execute(
                "SELECT DISTINCT session_id FROM transcript_chunks")}
        except Exception:
            text_sessions = set()

        on_disk_not_held = sorted(disk_sessions - held)
        on_disk_no_usage = sorted(disk_sessions - usage_sessions)
        on_disk_no_text = sorted(disk_sessions - text_sessions)
        # Most "unindexed" sessions are one-turn stubs — an aborted prompt, or an assistant
        # message that was nothing but a rate-limit rejection with an all-zero usage block.
        # Those have no tokens to index and are not a hole. Only a multi-turn session with no
        # usage row is evidence the mining actually missed something.
        substantive_missing = [sid for sid in on_disk_no_usage
                               if (con.execute("SELECT assistant_turns FROM sessions WHERE "
                                               "session_id=?", (sid,)).fetchone() or [0])[0] > 1]
        # The whole point: sessions we hold whose transcript Claude has already deleted. That
        # history exists ONLY here now.
        held_only_here = len(held - disk_sessions)

        def _iso(m):
            return (datetime.datetime.fromtimestamp(m, datetime.timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ") if m else None)

        oldest_disk_days = (round((now.timestamp() - disk["oldest_mtime"]) / 86400.0, 1)
                            if disk["oldest_mtime"] else None)
        last_run = con.execute(
            "SELECT value FROM meta WHERE key='reindex_last_run'").fetchone()
        last_run = json.loads(last_run["value"]) if last_run and last_run["value"] else None

        stale_hours = None
        if last_run and last_run.get("finished_at"):
            t = _parse_iso(last_run["finished_at"])
            stale_hours = round((now.timestamp() - t) / 3600.0, 1) if t else None

        # A gap that ends before the oldest surviving transcript can never be filled again —
        # that is the whole point of the job. One that overlaps live files is still recoverable.
        oldest_disk_day = (datetime.datetime.fromtimestamp(
            disk["oldest_mtime"], datetime.timezone.utc).strftime("%Y-%m-%d")
            if disk["oldest_mtime"] else None)
        for g in gaps:
            g["recoverable"] = bool(oldest_disk_day and g["to"] >= oldest_disk_day)
        permanent = [g for g in gaps if not g["recoverable"]]
        recoverable = [g for g in gaps if g["recoverable"]]

        problems = []
        if recoverable:
            problems.append(
                f"{sum(g['days'] for g in recoverable)} day(s) are unindexed but their "
                f"transcripts are STILL ON DISK — index them now: python3 jobs/reindex.py --full")
        if substantive_missing:
            problems.append(f"{len(substantive_missing)} multi-turn transcript(s) on disk have no "
                            f"token usage indexed — run: python3 jobs/reindex.py --full")
        if on_disk_not_held:
            problems.append(f"{len(on_disk_not_held)} transcript(s) on disk have no session row "
                            f"at all — run: python3 jobs/reindex.py --full")
        if stale_hours is not None and stale_hours > 36:
            problems.append(f"the reindex job last finished {stale_hours:.0f} h ago")
        if last_run is None:
            problems.append("the reindex job has never recorded a run "
                            "(install it: python3 install.py --autostart)")

        return 200, {
            "ok": not problems,
            "problems": problems,
            "indexed": {
                "first_day": first_day, "last_day": last_day,
                "indexed_days": span["indexed_days"],
                "expected_days": len(expected_days),
                "missing_days": len(expected_days) - (span["indexed_days"] or 0)
                                if expected_days else 0,
                "usage_events": span["events"], "sessions_with_usage": span["sessions"],
            },
            "gaps": gaps,
            "gap_summary": {
                "total_gap_days": sum(g["days"] for g in gaps),
                "permanent_gap_days": sum(g["days"] for g in permanent),
                "recoverable_gap_days": sum(g["days"] for g in recoverable),
                "oldest_surviving_transcript_day": oldest_disk_day,
                "meaning": "A permanent gap predates the oldest transcript Claude still keeps; "
                           "those days cannot be reconstructed from this machine. A recoverable "
                           "gap has files on disk right now and should be indexed today.",
            },
            "on_disk": {
                "projects_dir": str(_projects_dir()),
                "exists": disk["exists"],
                "transcript_files": disk["files"],
                "subagent_files": disk["subagent_files"],
                "files_with_no_session_uuid": disk.get("unidentified_files", 0),
                "megabytes": round(disk["bytes"] / 1e6, 1),
                "sessions": len(disk_sessions),
                "oldest_file": _iso(disk["oldest_mtime"]),
                "oldest_file_age_days": oldest_disk_days,
                "newest_file": _iso(disk["newest_mtime"]),
            },
            "held": {
                "sessions": len(held),
                "sessions_with_usage": len(usage_sessions),
                "sessions_with_text": len(text_sessions),
                "sessions_no_longer_on_disk": held_only_here,
            },
            "unindexed": {
                "sessions_on_disk_with_no_row": len(on_disk_not_held),
                "sessions_on_disk_with_no_usage": len(on_disk_no_usage),
                "sessions_on_disk_with_no_text": len(on_disk_no_text),
                "multi_turn_with_no_usage": len(substantive_missing),
                "examples": substantive_missing[:10],
                "note": "sessions_on_disk_with_no_usage counts one-turn stubs too (an aborted "
                        "prompt, or an assistant message that was only a rate-limit rejection "
                        "with an all-zero usage block). multi_turn_with_no_usage is the number "
                        "that actually indicates missed mining.",
            },
            "reindex_job": {
                "label": "com.aicommandcenter.reindex",
                "last_run": last_run,
                "hours_since_last_run": stale_hours,
            },
            "why": "Claude Code deletes transcripts on a rolling ~30-day window, so a day that "
                   "was never indexed can never be recovered. A gap listed here is permanent "
                   "unless the transcript is still on disk.",
        }
    finally:
        con.close()


# ==========================================================================================
# CLI (tracker usage / tracker limits) — tracker.py dispatches here so it stays one file thin
# ==========================================================================================
STATUSLINE_BACKUP_SUFFIX = ".bak-statusline-2026-09-12"


def install_statusline(settings_path: str | None = None, python_bin: str | None = None) -> dict:
    """Wire hooks/statusline.py into ~/.claude/settings.json as the statusLine command.
    Everything else in the file is preserved and a timestamped backup is written first."""
    sp = Path(settings_path or (Path.home() / ".claude" / "settings.json"))
    cfg = {}
    if sp.exists():
        try:
            cfg = json.loads(sp.read_text())
        except Exception as exc:
            raise ApiError(500, f"settings.json is not valid JSON ({exc}); refusing to touch it")
        bak = sp.with_name(sp.name + STATUSLINE_BACKUP_SUFFIX)
        if not bak.exists():
            bak.write_text(sp.read_text())
    py = python_bin or db.config().get("python") or sys.executable
    want = {"type": "command",
            "command": f"{py} {Path(__file__).resolve().parent.parent / 'hooks' / 'statusline.py'}",
            "padding": 0}
    before = cfg.get("statusLine")
    cfg["statusLine"] = want
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(cfg, indent=2) + "\n")
    return {"settings": str(sp), "backup": str(sp) + STATUSLINE_BACKUP_SUFFIX,
            "statusLine": want, "previous": before, "changed": before != want,
            "other_keys_preserved": sorted(k for k in cfg if k != "statusLine")}


def _bar(pct, width=28):
    if pct is None:
        return " " * width
    n = max(0, min(width, int(round(width * float(pct) / 100.0))))
    return "█" * n + "·" * (width - n)


def _print_limits(out: dict) -> None:
    w = sys.stdout.write
    c_dim, c_off = ("\033[2m", "\033[0m") if sys.stdout.isatty() and not os.environ.get("NO_COLOR") \
        else ("", "")
    if not out.get("current"):
        w("no rate-limit samples yet.\n" + (out.get("note") or "") + "\n")
        return
    age = out.get("age_minutes")
    w(f"PLAN LIMITS  (sampled {age:.0f} min ago from {out.get('source')}"
      f"{' — STALE' if out.get('stale') else ''})\n")
    labels = {"five_hour": "5-hour", "seven_day": "weekly", "spend": "spend"}
    shown = dict(out.get("windows") or {})
    covered = {a for v in shown.values() for a in (v.get("aliases") or [])}
    for key, blk in (out.get("current") or {}).items():
        if key not in covered:               # the richer limits[] entry already covers it
            shown.setdefault(key, blk)
    binding = (out.get("binding_window") or {}).get("name")
    for key in sorted(shown, key=lambda k: -shown[k].get("used_pct", 0)):
        blk = shown[key]
        fam = _model_specific(key)
        scoped = blk.get("scope_model")
        lab = (f"{scoped} weekly" if scoped and blk.get("group") == "weekly" else
               f"{scoped} session" if scoped else
               labels.get(key) or (f"{fam} weekly" if fam and "day" in key else
                                   f"{fam} 5-hour" if fam else key.replace("_", " ")))
        mins = blk.get("resets_in_min")
        when = blk.get("resets_at_ist") or ""
        rest = f"resets {when}" + (f" (in {mins/60:.1f}h)" if mins else "")
        sev = (blk.get("severity") or "").upper()
        mark = ("  ACTIVE" if blk.get("is_active") else "") + (" ←binding" if key == binding else "")
        w(f"  {lab:18} {blk['used_pct']:5.1f}% {sev[:8]:<8} {_bar(blk['used_pct'], 22)}  "
          f"{rest}{mark}\n")
        # pace, not level: the same percentage means opposite things at either end of a window
        pc = blk.get("pace")
        if pc:
            arrow = {"ahead": "↑", "behind": "↓", "on_pace": "="}.get(pc["verdict"], " ")
            w(f"  {'':18} {c_dim}{arrow} {pc['elapsed_pct']:.0f}% of the window elapsed"
              f" · {pc['vs_linear_pct']:+.0f} pts vs linear"
              + (f" · burn {pc['burn_pct_per_hour']:.1f}%/h" if pc.get("burn_pct_per_hour") else "")
              + (f" · lands at {pc['projected_pct_at_reset']:.0f}%"
                 if pc.get("projected_pct_at_reset") is not None else "")
              + (f" · {pc['affordable_pct_per_hour']:.1f}%/h keeps you inside"
                 if pc.get("affordable_pct_per_hour") and blk["used_pct"] < 100 else "")
              + f"{c_off}\n")
    if out.get("model_specific_hint"):
        w(f"  {out['model_specific_hint']}\n")
    f = out.get("forecast") or {}
    if f.get("burn_pct_per_hour_5h") is not None:
        w(f"  burn     5h {f['burn_pct_per_hour_5h']:+.2f} %/h"
          f"   7d {f.get('burn_pct_per_day_7d') or 0:+.2f} %/day\n")
    for key, lab in (("five_hour_hits_100_at_ist", "5-hour window"),
                     ("seven_day_hits_100_at_ist", "weekly window")):
        if f.get(key):
            hit = f[key]
            before = f.get(key.replace("_at_ist", "_before_reset").replace("hits_100", "hits_100"))
            w(f"  forecast {lab} reaches 100% at {hit}"
              f"{'  ← BEFORE it resets' if before else '  (after the reset — fine)'}\n")
    ps = out.get("sessions_last_hour") or []
    if len(ps) > 1:
        w(f"  live sessions (each reports its own last refresh):\n")
        for s_ in ps[:6]:
            name = (s_.get("session_name") or s_["session_id"][:8])[:34]
            w(f"    {name:34} {(s_.get('model') or '')[:18]:18} "
              f"wk {s_.get('seven_day_pct') or 0:5.1f}%  ctx {s_.get('context_pct') or 0:4.0f}%  "
              f"${s_.get('cost_usd') or 0:8.2f}  {s_['age_minutes']:.0f}m ago\n")
    xu = out.get("extra_usage") or {}
    sp = out.get("spend") or {}
    if xu or sp:
        bits = []
        if xu.get("is_enabled") is not None:
            bits.append("extra usage " + ("on" if xu.get("is_enabled") else "off")
                        + (f" ({xu.get('utilization')}% of {xu.get('monthly_limit')})"
                           if xu.get("utilization") is not None else ""))
        if sp.get("percent") is not None:
            bits.append(f"spend {sp['percent']}% ({(sp.get('severity') or '').lower()})")
        if out.get("seven_day_breakdown"):
            bits.append("7d breakdown available")
        if bits:
            w("  " + " · ".join(bits) + "\n")
    ex = out.get("payload_extras") or {}
    if ex.get("cache_hit_ratio") is not None:
        w(f"  prompt cache {ex['cache_hit_ratio']*100:.1f}% hit ({ex.get('cache_ttl')} ttl, "
          f"{ex.get('cache_misses')} misses)\n")
    missing = [k for k in ("five_hour", "seven_day") if k not in (out.get("current") or {})]
    if missing:
        w(f"  (no {', '.join(m.replace('_', '-') for m in missing)} window in the statusLine "
          f"payload on this plan)\n")
    w(f"  {out.get('total_samples', 0)} samples on record, newest from "
      f"{(out.get('model') or '?')}\n")
    prov = out.get("provenance") or {}
    if prov:
        w(f"  {c_dim}provenance: percentages OFFICIAL (Claude's own "
          f"{prov.get('sampled_from', 'statusline')} payload) · pace, burn and forecast "
          f"ESTIMATE (tracker arithmetic on them){c_off}\n")


def _print_usage(out: dict) -> None:
    w = sys.stdout.write
    t = out["totals_all_time"]
    c = out["cost_calibration"]
    w(f"USAGE — last {out['days']} days   (hours in {out['hour_timezone']})\n")
    w(f"  all time: {t.get('calls', 0):,} API calls · {t.get('sessions', 0)} sessions · "
      f"{(t.get('tokens') or 0)/1e9:.2f}B tokens · ${t.get('cost_usd_est') or 0:,.0f} est "
      f"({t.get('active_days')} active days, {out['streaks']['longest_days']}-day best streak)\n")
    if c.get("est_over_recorded"):
        w(f"  calibration: estimate is {c['est_over_recorded']:.2f}x the recorded cost-state "
          f"total over {c['sessions_compared']} sessions\n\n")
    w(f"  {'model':26} {'calls':>7} {'tokens':>14} {'est $':>10}\n")
    for m in out["by_model"][:8]:
        w(f"  {(m['model'] or '?')[:26]:26} {m['calls']:>7,} {m['tokens']:>14,} "
          f"{m['cost_usd_est'] or 0:>10,.2f}\n")
    w(f"\n  {'project':20} {'sess':>5} {'tokens':>14} {'est $':>10} {'share':>7} {'$/sess':>9}\n")
    for p in out["by_project"][:10]:
        w(f"  {p['project'][:20]:20} {p['sessions']:>5} {p['tokens']:>14,} "
          f"{p['cost_usd_est'] or 0:>10,.2f} {p['share_pct']:>6.1f}% "
          f"{p['avg_cost_per_session']:>9,.2f}\n")
    w(f"\n  most expensive sessions\n")
    for s_ in out["by_session"][:8]:
        rec = f" (recorded ${s_['cost_usd_recorded']:,.2f})" if s_["cost_usd_recorded"] else ""
        w(f"  ${s_['cost_usd_est'] or 0:>9,.2f}{rec:>22}  {s_['date']}  "
          f"{(s_['title'] or s_['session_id'][:8])[:44]}\n")
    fs = out["fable_share_of_7d"]
    w(f"\n  last 7 days: fable took {fs['share_pct']:.0f}% of all tokens "
      f"({fs['fable_tokens']/1e6:,.0f}M). Top consumers:\n")
    for s_ in fs["top_sessions"][:5]:
        w(f"    {s_['tokens']/1e6:>8,.0f}M  {(s_['project'] or '?')[:14]:14} "
          f"{(s_['title'] or s_['session_id'][:8])[:44]}\n")
    w(f"\n  busiest hours ({TZ_LABEL}): " + ", ".join(
        f"{h['hour_ist']:02d}h {h['calls']}" for h in
        sorted(out["by_hour_of_day"], key=lambda r: -r["calls"])[:6]) + "\n")
    pace = out.get("pace") or {}
    wins = pace.get("windows") or {}
    if wins:
        w("\n  PACE — where each plan window stands, and how fast it is moving\n")
        for name in sorted(wins, key=lambda k: -(wins[k]["used_pct"])):
            pc = wins[name]
            flag = "  ← runs out before it resets" if pc.get("will_exhaust_before_reset") else ""
            w(f"    {name[:22]:22} {pc['sentence']}{flag}\n")
    t_ = pace.get("today") or {}
    if t_.get("expected_by_this_hour_usd_est"):
        w(f"    {'today (' + TZ_LABEL + ')':22} ${t_['cost_usd_est_so_far']:,.2f} est by {t_['hour_ist']:.1f}h "
          f"vs ${t_['expected_by_this_hour_usd_est']:,.2f} typical "
          f"({t_['vs_expected_pct']:+.0f}%, {t_['verdict']})\n")
    prov = out.get("provenance") or {}
    if prov:
        w("\n  PROVENANCE — what each number is\n")
        import textwrap
        for label in ("official", "recorded", "estimate"):
            txt = (prov.get("basis") or {}).get(label)
            if txt:
                lines = textwrap.wrap(txt, 84)
                w(f"    {label:9} {lines[0]}\n")
                for extra in lines[1:]:
                    w(f"    {'':9} {extra}\n")
        w(f"    {'':9} tokens are `recorded`; every $ marked est is `estimate`; plan "
          f"percentages at /api/usage/limits are `official`.\n")


def cli_main(argv: list[str]) -> int:
    """`tracker usage` / `tracker limits`. Returns a process exit code."""
    import argparse
    ap = argparse.ArgumentParser(prog="tracker usage|limits",
                                 description="Token usage analysis and plan-limit windows.")
    ap.add_argument("command", choices=["usage", "limits"])
    ap.add_argument("--days", type=int, default=30, help="analysis window (usage)")
    ap.add_argument("--json", action="store_true", help="print the raw API payload")
    ap.add_argument("--refresh", action="store_true",
                    help="limits: ask Anthropic for the current windows (needs config.json "
                         "usage.oauth_refresh=true; sends your OAuth token to Anthropic only)")
    ap.add_argument("--install", action="store_true",
                    help="limits: wire hooks/statusline.py into ~/.claude/settings.json")
    a = ap.parse_args(argv)
    try:
        if a.command == "limits":
            if a.install:
                print(json.dumps(install_statusline(), indent=2))
                return 0
            if a.refresh:
                print(json.dumps(oauth_refresh(), indent=2))
                return 0
            out = get_limits({}, None, None)[1]
            print(json.dumps(out, indent=2)) if a.json else _print_limits(out)
            return 0
        out = get_analysis({"days": str(a.days)}, None, None)[1]
        print(json.dumps(out, indent=2, default=str)) if a.json else _print_usage(out)
        return 0
    except ApiError as exc:
        print(f"tracker {a.command}: {exc.message}", file=sys.stderr)
        return 1

def get_bundle(query: dict, body, match) -> tuple[int, object]:
    """Everything the Usage view needs, in one round trip.

    The view used to open with limits + summary + analysis as three separate requests (and the
    shell asked for /api/usage/limits twice more), so a cold load cost ~10 API calls. One handler
    computing all three keeps first paint at four calls total and lets the three answers share a
    single moment in time. `?parts=limits,summary` trims it. Errors inside one part are reported
    as `{"error": ...}` for that part instead of failing the whole bundle.
    """
    query = query or {}
    want = [p.strip() for p in (query.get("parts") or "limits,summary,analysis").split(",") if p.strip()]
    out: dict = {"ok": True, "parts": want}
    sub = {k: v for k, v in query.items() if k in ("days", "refresh")}
    for name, fn in (("limits", get_limits), ("summary", get_summary), ("analysis", get_analysis)):
        if name not in want:
            continue
        try:
            status, payload = fn(dict(sub), None, None)
            out[name] = payload if status == 200 else {"error": payload}
        except ApiError as e:
            out[name] = {"error": e.message, "status": e.status}
        except Exception as e:                                   # never let one part 500 the view
            out[name] = {"error": f"{type(e).__name__}: {e}"[:300]}
    return 200, out


ROUTES = [
    ("GET", r"/api/usage/summary/?$", get_summary),
    ("GET", r"/api/usage/bundle/?$", get_bundle),
    ("GET", r"/api/usage/series/?$", get_series),
    ("GET", r"/api/usage/models/?$", get_models),
    ("GET", r"/api/usage/limits/?$", get_limits),
    ("GET", r"/api/usage/analysis/?$", get_analysis),
    ("GET", r"/api/usage/coverage/?$", get_coverage),
    ("POST", r"/api/usage/refresh/?$", post_refresh),
]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("usage", "limits"):
        sys.exit(cli_main(sys.argv[1:]))
    import argparse
    ap = argparse.ArgumentParser(description="Usage module — index a transcript or print the summary.")
    ap.add_argument("--index", metavar="TRANSCRIPT", help="index one transcript's token usage")
    ap.add_argument("--summary", action="store_true", help="print the summary payload as JSON")
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args()
    if a.index:
        print(json.dumps(index_session(a.index), indent=2))
    else:
        print(json.dumps(get_summary({"days": str(a.days)}, None, None)[1], indent=2, default=str))
