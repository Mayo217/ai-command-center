"""Command Center module — usage dynamics: the meter as a measured instrument, not a guess.

GET /api/usage/dynamics      the whole model below (cached 60 s; ?fresh=1 recomputes)

    python3 modules/dynamics.py            plain-words report
    python3 modules/dynamics.py --json     the raw model
    python3 modules/dynamics.py --brief    the 2-3 lines the UserPromptSubmit hook injects

What it measures, and how
-------------------------
1. Exchange rate. For every limit window (session 5h, weekly_all, weekly_scoped:<model>) it
   regresses the hourly climb of the official meter on the API-priced spend recorded on this Mac
   that hour, split by model family, with non-negative least squares:
       d_pct(hour) = sum_f k_f * cost_f(hour) + noise
   k_f is "% of this window per $ of family f". 1/k_f is what 1% of the window buys.
   Fitted twice: a long baseline (14 d) and a recent one (48 h). recent/long is the DRIFT — if the
   meter suddenly charges 1.6x more per dollar than its own baseline, the source changed.
2. Anomalies. Residuals of the long fit give sigma. An hour where the meter climbed more than
   3 sigma above what local spend explains is UNEXPLAINED DRAIN: usage that did not come from this
   Mac (claude.ai, phone, another machine) or a change in how Anthropic counts. The live check
   compares the last 60 min of meter climb with the last 60 min of live spend from the status line.
3. Forecast. Burn over the last 1 h and 3 h (official samples, running max inside the window),
   time to 100% at that burn, whether that lands before the reset, the pace that would exactly
   last to the reset, and the remaining window converted back into dollars and into turns.
4. Context economics. Every turn re-reads the whole context (cache read). Per live session:
   context tokens x cache-read price = the floor cost of one more turn, expressed as % of the
   binding window. That is the number that says when /compact or a fresh session pays for itself.
5. Drain board. Which live session ate how much of each window in the last hour.

Everything here is an ESTIMATE built on two official inputs (used_pct, resets_at) and Claude
Code's own cost figures. Every output carries provenance.
"""
from __future__ import annotations

import datetime
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from modules import usage  # noqa: E402

MODULE = "dynamics"
ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
CACHE_FILE = STATE / "dynamics.json"
CTX_DIR = STATE / "ctx"
CACHE_TTL_S = 60
LONG_DAYS = 14
RECENT_HOURS = 48
CALIB_EXCLUDE_HOURS = 3        # usage_events for running sessions lag; keep them out of the fit
FAMILIES = ("fable", "opus", "sonnet", "haiku")
DRIFT_ALERT = 1.5              # recent/long exchange rate beyond this (or below 1/this) = source change
SIGMA_ALERT = 3.0
DRIFT_MIN_USD = 60.0          # recent spend a family needs before its drift is judged
LIVE_MIN_UNEXPLAINED = {"session": 3.0, "weekly": 0.5}   # pct points, so noise never alarms


def _now() -> float:
    return time.time()


def _iso(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _ist(t: float | None) -> str | None:
    if not t:
        return None
    return datetime.datetime.fromtimestamp(t, usage.IST if hasattr(usage, "IST") else None).strftime("%a %H:%M")


def _kind(name: str) -> str:
    return "session" if name in ("session", "five_hour") else "weekly"


def _canon(name: str) -> str:
    return {"five_hour": "session", "seven_day": "weekly_all"}.get(name, name)


def _scope_family(name: str) -> str | None:
    """weekly_scoped:Fable counts only Fable; the others count every family."""
    if name.startswith("weekly_scoped:"):
        return name.split(":", 1)[1].strip().lower()
    return None


# ── inputs ────────────────────────────────────────────────────────────────────────────────────

def meter_series(con, since: float) -> dict:
    """{window name: {reset bucket: [(t, pct)]}} from every stored sample's raw payload."""
    out: dict = defaultdict(lambda: defaultdict(list))
    for r in con.execute("SELECT ts, raw FROM rate_limit_samples WHERE ts>=? ORDER BY ts", (_iso(since),)):
        t = usage._parse_iso(r["ts"])
        if not t:
            continue
        for name, val in usage._windows_from_raw(r["raw"]).items():
            reset, pct = val.get("resets_at_epoch"), val.get("used_pct")
            if reset is None or pct is None:
                continue
            out[_canon(name)][round(float(reset) / 1800)].append((t, float(pct)))
    return out


def hourly_climb(series: dict) -> dict:
    """{name: {hour: d_pct}} — running max inside each window, so stale snapshots never dip it."""
    out: dict = defaultdict(dict)
    for name, wins in series.items():
        for pts in wins.values():
            pts.sort()
            hi, last_by_hour = 0.0, {}
            for t, p in pts:
                hi = max(hi, p)
                last_by_hour[int(t // 3600)] = hi
            hours = sorted(last_by_hour)
            for a, b in zip(hours, hours[1:]):
                if b == a + 1:
                    out[name][b] = last_by_hour[b] - last_by_hour[a]
    return out


def hourly_spend(con, since: float, until: float) -> dict:
    """{hour: {family: $}} from the transcript-mined usage_events (API list prices)."""
    out: dict = defaultdict(lambda: defaultdict(float))
    for r in con.execute(
            "SELECT ts, family, cost_usd_est FROM usage_events WHERE ts>=? AND ts<? AND cost_usd_est>0",
            (_iso(since), _iso(until))):
        t = usage._parse_iso(r["ts"])
        if t:
            fam = r["family"] if r["family"] in FAMILIES else "opus"
            out[int(t // 3600)][fam] += r["cost_usd_est"]
    return out


def live_spend(con, since: float) -> dict:
    """{session_id: {"usd": $, "family": f}} spent since `since`, from the status line's running
    per-session cost (cost.total_cost_usd). Live to the minute, unlike usage_events."""
    first: dict = {}
    last: dict = {}
    fam: dict = {}
    for r in con.execute(
            "SELECT session_id, model, cost_usd, ts FROM rate_limit_samples "
            "WHERE ts>=? AND session_id IS NOT NULL AND cost_usd IS NOT NULL ORDER BY ts",
            (_iso(since - 900),)):
        sid = r["session_id"]
        t = usage._parse_iso(r["ts"]) or 0
        if t < since:
            first[sid] = r["cost_usd"]          # the baseline: last reading before the window
        else:
            first.setdefault(sid, 0.0 if sid not in last else first.get(sid, 0.0))
        last[sid] = r["cost_usd"]
        fam[sid] = usage.model_family(r["model"])
    out = {}
    for sid, v in last.items():
        d = max(0.0, v - first.get(sid, 0.0))
        if d > 0:
            out[sid] = {"usd": round(d, 4), "family": fam.get(sid) or "opus"}
    return out


# ── the math ──────────────────────────────────────────────────────────────────────────────────

def nnls(rows: list[tuple[list[float], float]], n: int, iters: int = 400) -> list[float]:
    """Non-negative least squares by cyclic coordinate descent. Tiny n (<=4), stdlib only."""
    k = [0.0] * n
    xx = [[sum(x[i] * x[j] for x, _ in rows) for j in range(n)] for i in range(n)]
    xy = [sum(x[i] * y for x, y in rows) for i in range(n)]
    for _ in range(iters):
        moved = 0.0
        for i in range(n):
            if xx[i][i] <= 0:
                continue
            g = xy[i] - sum(xx[i][j] * k[j] for j in range(n) if j != i)
            new = max(0.0, g / xx[i][i])
            moved = max(moved, abs(new - k[i]))
            k[i] = new
        if moved < 1e-12:
            break
    return k


def fit(climb: dict, spend: dict, families: tuple, hours: list[int]) -> dict | None:
    rows = []
    for h in hours:
        if h not in climb:
            continue
        x = [spend.get(h, {}).get(f, 0.0) for f in families]
        rows.append((x, climb[h], h))
    used = [f for i, f in enumerate(families) if sum(r[0][i] for r in rows) > 1.0]
    if len(rows) < 12 or not used:
        return None
    idx = [families.index(f) for f in used]
    rr = [([r[0][i] for i in idx], r[1]) for r in rows]
    k = nnls(rr, len(idx))
    res = [(y - sum(a * b for a, b in zip(k, x)), h) for (x, y), (_, _, h) in zip(rr, rows)]
    sigma = math.sqrt(sum(e * e for e, _ in res) / max(1, len(res) - len(idx)))
    tot_x = sum(sum(x) for x, _ in rr)
    tot_y = sum(y for _, y in rr)
    ss_tot = sum((y - tot_y / len(rr)) ** 2 for _, y in rr) or 1.0
    r2 = 1 - sum(e * e for e, _ in res) / ss_tot
    return {
        "k_pct_per_usd": {f: round(v, 5) for f, v in zip(used, k)},
        "usd_per_pct": {f: (round(1 / v, 2) if v > 0 else None) for f, v in zip(used, k)},
        "blended_usd_per_pct": round(tot_x / tot_y, 2) if tot_y > 0 else None,
        "sigma_pct": round(sigma, 3), "r2": round(r2, 3), "hours": len(rr),
        "spend_usd": {f: round(sum(r[0][families.index(f)] for r in rows), 2) for f in used},
        "_res": res, "_k": dict(zip(used, k)),
    }


def window_burn(pts: list[tuple[float, float]], now: float, hours: float) -> float | None:
    pts = sorted(p for p in pts if p[0] >= now - hours * 3600)
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < min(0.25, hours / 2) * 3600:
        return None
    hi, run = 0.0, []
    for t, p in pts:
        hi = max(hi, p)
        run.append((t, hi))
    return (run[-1][1] - run[0][1]) / ((run[-1][0] - run[0][0]) / 3600)


# ── the model ─────────────────────────────────────────────────────────────────────────────────

def compute(con=None, now: float | None = None) -> dict:
    own = con is None
    con = con or db.connect()
    now = now or _now()
    try:
        return _compute(con, now)
    finally:
        if own:
            con.close()


def _compute(con, now: float) -> dict:
    wins = {_canon(k): v for k, v in usage.collect_windows(con).items()}
    series = meter_series(con, now - LONG_DAYS * 86400)
    climb = hourly_climb(series)
    cut = now - CALIB_EXCLUDE_HOURS * 3600
    spend = hourly_spend(con, now - LONG_DAYS * 86400, cut)
    long_hours = [h for h in range(int((now - LONG_DAYS * 86400) // 3600), int(cut // 3600))]
    recent_hours = [h for h in long_hours if h >= (now - RECENT_HOURS * 3600) // 3600]
    live_1h = live_spend(con, now - 3600)
    live_usd = defaultdict(float)
    for v in live_1h.values():
        live_usd[v["family"]] += v["usd"]

    windows, alerts = [], []
    for name, w in sorted(wins.items(), key=lambda kv: -kv[1]["used_pct"]):
        scope = _scope_family(name)
        fams = (scope,) if scope in FAMILIES else FAMILIES
        lf = fit(climb.get(name, {}), spend, fams, long_hours)
        rf = fit(climb.get(name, {}), spend, fams, recent_hours)
        used = float(w["used_pct"])
        reset = float(w.get("resets_at_epoch") or 0)
        left_h = max(0.0, (reset - now) / 3600) if reset else None
        cur = series.get(name, {}).get(round(reset / 1800), []) if reset else []
        b1, b3 = window_burn(cur, now, 1), window_burn(cur, now, 3)
        burn = b1 if b1 is not None else b3
        rem = max(0.0, 100.0 - used)
        eta_h = rem / burn if burn and burn > 0 else None
        safe = rem / left_h if left_h else None
        row = {
            "name": name, "kind": _kind(name), "used_pct": used, "remaining_pct": round(rem, 1),
            "resets_at": reset or None, "resets_ist": _ist(reset), "hours_to_reset": round(left_h, 2) if left_h is not None else None,
            "burn_1h": None if b1 is None else round(b1, 2), "burn_3h": None if b3 is None else round(b3, 2),
            "eta_full_hours": None if eta_h is None else round(eta_h, 2),
            "hits_before_reset": bool(eta_h is not None and left_h is not None and eta_h < left_h),
            "safe_burn_per_hour": None if safe is None else round(safe, 2),
            "provenance": "used_pct/resets official; everything else estimate",
        }
        if lf:
            k = lf["_k"]
            row["exchange"] = {kk: vv for kk, vv in lf.items() if not kk.startswith("_")}
            # what is left, in dollars of the family that is spending now (else the cheapest-per-pct)
            fam_now = max(live_usd, key=live_usd.get) if live_usd else max(k, key=k.get)
            if k.get(fam_now):
                row["remaining_usd"] = {"family": fam_now, "usd": round(rem / k[fam_now], 2)}
            # anomaly 1: hours in the baseline the meter climbed more than local spend explains
            sig = lf["sigma_pct"]
            row["unexplained_hours_14d"] = [
                {"hour": _iso(h * 3600)[:13], "excess_pct": round(e, 1)}
                for e, h in lf["_res"] if sig and e > SIGMA_ALERT * sig][-5:]
            # anomaly 2: the last hour, live
            expected = sum(k.get(f, 0.0) * u for f, u in live_usd.items())
            if b1 is not None:
                excess = b1 - expected
                floor = LIVE_MIN_UNEXPLAINED[row["kind"]]
                row["live_check"] = {"meter_pct_1h": round(b1, 2), "explained_pct_1h": round(expected, 2),
                                     "excess_pct_1h": round(excess, 2)}
                if excess > max(floor, SIGMA_ALERT * sig):
                    alerts.append({"level": "anomaly", "window": name,
                                   "text": f"{name}: meter climbed {b1:.1f}% in the last hour but this Mac's spend "
                                           f"explains {expected:.1f}% — {excess:.1f} pts came from elsewhere "
                                           f"(claude.ai / phone / another machine) or the counting changed"})
            # drift: the source charging a different rate than its own baseline
            if rf:
                drift = {}
                for f, v in rf["_k"].items():
                    base = k.get(f)
                    # a family with little recent spend gives a noisy slope: no verdict on it
                    if base and v and rf["hours"] >= 12 and rf["spend_usd"].get(f, 0) >= DRIFT_MIN_USD:
                        drift[f] = round(v / base, 2)
                row["drift_recent_vs_14d"] = drift
                for f, ratio in drift.items():
                    if ratio >= DRIFT_ALERT or ratio <= 1 / DRIFT_ALERT:
                        alerts.append({"level": "drift", "window": name,
                                       "text": f"{name}: {f} now costs {ratio:.2f}x the % per $ of its 14-day norm "
                                               f"(1% = ${1 / rf['_k'][f]:.2f} vs ${1 / k[f]:.2f})"})
        if row["hits_before_reset"]:
            alerts.append({"level": "drain", "window": name,
                           "text": f"{name} at {used:.0f}%: this pace ({burn:.1f}%/h) fills it in "
                                   f"{eta_h * 60:.0f} min, reset is in {left_h * 60:.0f} min — "
                                   f"safe pace is {safe:.1f}%/h"})
        windows.append(row)

    binding = next((w for w in windows if w["used_pct"] < 100), windows[0] if windows else None)
    # When a model-scoped weekly is already full, the window that limits the OTHER models binds.
    sessions = context_economics(windows, live_1h, now)
    model = {
        "computed_at": now, "computed_ist": _ist(now),
        "windows": windows,
        "binding": binding["name"] if binding else None,
        "alerts": alerts,
        "sessions": sessions,
        "waste_7d": (wst := waste(con, now)),
        "advice": advice(windows, sessions, alerts, wst),
        "provenance": "used_pct and resets_at are Anthropic's; spend is API list price from local "
                      "transcripts and the status line; exchange rates, anomalies and forecasts are estimates",
    }
    return model


def waste(con, now: float, days: int = 7) -> dict:
    """Where the meter goes that need not, measured from the transcript-mined events.

    cold_rebuild: a mid-session request that wrote >=100k tokens of cache — the prompt cache had
      expired (5-minute TTL; a break, a long tool run) and the whole context was re-billed at the
      cache-WRITE price (1.25x input) instead of the cache-read price (0.1x).
    overhead: the first request of each session — system prompt, tools, MCP schemas, CLAUDE.md,
      MEMORY.md — paid before any work; median over sessions.
    agents: share of spend in sidechains (subagents)."""
    rows = con.execute(
        "SELECT session_id, ts, family, model, input_tokens, cache_create, cache_read, output_tokens, "
        "cost_usd_est, sidechain FROM usage_events WHERE ts>=? ORDER BY session_id, ts",
        (_iso(now - days * 86400),)).fetchall()
    total = sum(r["cost_usd_est"] or 0 for r in rows) or 1e-9
    seen, firsts, cold, cold_usd, agent_usd, out_usd, fam_usd = set(), [], 0, 0.0, 0.0, 0.0, defaultdict(float)
    for r in rows:
        c = r["cost_usd_est"] or 0
        fam_usd[r["family"] or "other"] += c
        if r["sidechain"]:
            agent_usd += c
        price = usage.price_for(r["model"] or r["family"])[0]
        out_usd += (r["output_tokens"] or 0) / 1e6 * price[1]
        sid = (r["session_id"], r["sidechain"])
        if sid not in seen:
            seen.add(sid)
            if not r["sidechain"]:
                firsts.append((r["input_tokens"] or 0) + (r["cache_create"] or 0) + (r["cache_read"] or 0))
            continue
        if (r["cache_create"] or 0) >= 100_000:
            cold += 1
            # the avoidable part: write price minus what a warm cache read would have cost
            cold_usd += (r["cache_create"] / 1e6) * (price[3] - price[2])
    firsts.sort()
    med = firsts[len(firsts) // 2] if firsts else 0
    return {
        "days": days, "spend_usd": round(total, 2),
        "cold_rebuilds": cold, "cold_rebuild_avoidable_usd": round(cold_usd, 2),
        "cold_rebuild_share": round(cold_usd / total, 3),
        "overhead_median_tokens": med, "sessions": len(firsts),
        "agent_share": round(agent_usd / total, 3), "output_share": round(out_usd / total, 3),
        "family_share": {f: round(v / total, 3) for f, v in sorted(fam_usd.items(), key=lambda kv: -kv[1])},
    }


def _k_for(window: dict, family: str) -> float | None:
    k = ((window.get("exchange") or {}).get("k_pct_per_usd") or {})
    return k.get(family) or (max(k.values()) if k else None)


def context_economics(windows: list[dict], live_1h: dict, now: float) -> list[dict]:
    """Per session (state/ctx/*.json, fresh within 6 h): context size, the floor cost of one more
    turn (re-reading the cached context), that cost in % of each window, and last-hour drain."""
    out = []
    for f in CTX_DIR.glob("*.json") if CTX_DIR.exists() else []:
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if now - float(d.get("ts") or 0) > 6 * 3600:
            continue
        fam = usage.model_family(d.get("model"))
        tok = d.get("tokens") or 0
        price = usage.price_for(d.get("model") or fam)[0]          # (in, out, cache_read, cache_write) per M
        turn_floor = tok / 1e6 * price[2]
        row = {"session_id": d["session_id"], "title": d.get("title"), "family": fam,
               "ctx_pct": d.get("pct"), "ctx_tokens": tok, "ctx_size": d.get("size"),
               "turn_floor_usd": round(turn_floor, 4),
               "drain_1h_usd": (live_1h.get(d["session_id"]) or {}).get("usd", 0.0), "pct": {}}
        for w in windows:
            k = _k_for(w, fam) if (_scope_family(w["name"]) in (None, fam)) else None
            if k:
                row["pct"][w["name"]] = {"turn_floor": round(turn_floor * k, 3),
                                         "drain_1h": round(row["drain_1h_usd"] * k, 2)}
        out.append(row)
    out.sort(key=lambda r: -r["drain_1h_usd"])
    return out


def advice(windows: list[dict], sessions: list[dict], alerts: list[dict], wst: dict | None = None) -> list[str]:
    tips = []
    wk = next((w for w in windows if w["name"] == "weekly_all"), None)
    blended = ((wk or {}).get("exchange") or {}).get("blended_usd_per_pct")
    if wst and wst.get("cold_rebuild_avoidable_usd", 0) > 10:
        pts = f" ≈ {wst['cold_rebuild_avoidable_usd'] / blended:.0f} pts of weekly" if blended else ""
        tips.append(f"{wst['cold_rebuilds']} cold cache rebuilds in 7 d cost ${wst['cold_rebuild_avoidable_usd']:.0f}{pts}: "
                    f"the 5-min prompt cache expired mid-session; /compact or /clear before a break, not after")
    if wst and wst.get("overhead_median_tokens", 0) > 40_000:
        tips.append(f"every session starts at ~{wst['overhead_median_tokens'] / 1000:.0f}k tokens of fixed overhead "
                    f"(system prompt, tools, MCP, CLAUDE.md, MEMORY.md) — trim unused MCP servers and the memory index")
    live = [w for w in windows if w["used_pct"] < 100]
    for w in windows:
        if w["used_pct"] >= 100 and w["name"].startswith("weekly_scoped:"):
            fam = w["name"].split(":", 1)[1]
            tips.append(f"{fam} weekly is full until {w['resets_ist']} — run work on another model until then")
    tight = next((w for w in live if w["kind"] == "weekly" and w["used_pct"] >= 85), None)
    if tight and tight.get("remaining_usd"):
        r = tight["remaining_usd"]
        tips.append(f"{tight['name']} has {tight['remaining_pct']:.0f}% left ≈ ${r['usd']:.0f} of {r['family']} "
                    f"at API prices until {tight['resets_ist']} — spend it on decisions, not menial reads")
    for s in sessions:
        if (s.get("ctx_pct") or 0) >= 40 and s["ctx_tokens"] >= 150_000:
            worst = max(s["pct"].items(), key=lambda kv: kv[1]["turn_floor"], default=None)
            if worst:
                tips.append(f"'{s['title'] or s['session_id'][:8]}' carries {s['ctx_tokens'] / 1000:.0f}k tokens: "
                            f"each turn costs ≥{worst[1]['turn_floor']:.2f}% of {worst[0]} just to re-read it — "
                            f"/compact or hand off to a fresh session")
    if sessions:
        top = sessions[0]
        if top["drain_1h_usd"] > 0:
            share = max((v["drain_1h"] for v in top["pct"].values()), default=0)
            tips.append(f"top drain last hour: '{top['title'] or top['session_id'][:8]}' "
                        f"${top['drain_1h_usd']:.2f} (≈{share:.1f}% of a window)")
    return tips[:8]


# ── outputs ───────────────────────────────────────────────────────────────────────────────────

def cached(max_age: float = CACHE_TTL_S, fresh: bool = False) -> dict:
    if not fresh:
        try:
            m = json.loads(CACHE_FILE.read_text())
            if _now() - float(m.get("computed_at") or 0) < max_age:
                return m
        except Exception:
            pass
    m = compute()
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, default=str))
        tmp.replace(CACHE_FILE)
    except Exception:
        pass
    return m


def brief(model: dict, session_id: str | None = None) -> str:
    """The lines every session sees on each prompt: its own context, the binding window's state,
    and any alert. Short on purpose — it is paid for on every turn."""
    lines = []
    me = None
    if session_id:
        try:
            me = json.loads((CTX_DIR / f"{session_id}.json").read_text())
        except Exception:
            me = None
    parts = []
    if me and me.get("pct") is not None:
        tok = me.get("tokens") or 0
        size = me.get("size") or 0
        parts.append(f"your context {me['pct']:.0f}% ({tok / 1000:.0f}k of {size / 1000:.0f}k)")
        mine = next((s for s in model.get("sessions", []) if s["session_id"] == session_id), None)
        if mine and mine["pct"]:
            nm, v = max(mine["pct"].items(), key=lambda kv: kv[1]["turn_floor"])
            parts.append(f"each turn ≥{v['turn_floor']:.2f}% of {nm}")
    for w in model.get("windows", []):
        seg = f"{w['name']} {w['used_pct']:.0f}%"
        if w["used_pct"] < 100 and w.get("burn_1h") is not None:
            seg += f" (+{w['burn_1h']:.1f}%/h"
            seg += f", full in {w['eta_full_hours'] * 60:.0f}m before reset)" if w["hits_before_reset"] else ")"
        if w.get("resets_ist"):
            seg += f" ↻{w['resets_ist']}"
        parts.append(seg)
    lines.append("[usage] " + " · ".join(parts))
    for a in model.get("alerts", [])[:2]:
        lines.append(f"[usage {a['level']}] {a['text']}")
    tight = [t for t in model.get("advice", []) if "left ≈" in t or "full until" in t]
    if tight and any(w["used_pct"] >= 90 for w in model.get("windows", [])):
        lines.append("[usage tip] " + tight[0])
    return "\n".join(lines)


def get_dynamics(query: dict, body, match):
    fresh = (query.get("fresh") or [""])[0] in ("1", "true") if isinstance(query.get("fresh"), list) else bool(query.get("fresh"))
    m = cached(fresh=fresh)
    return 200, {k: v for k, v in m.items()}


ROUTES = [("GET", r"^/api/usage/dynamics$", get_dynamics)]


def _report(m: dict) -> str:
    out = [f"usage dynamics · {m['computed_ist']} · binding {m['binding']}", ""]
    for w in m["windows"]:
        out.append(f"{w['name']:<22} {w['used_pct']:5.1f}%  ↻{w['resets_ist']} ({w['hours_to_reset']}h)")
        out.append(f"   burn 1h {w['burn_1h']}  3h {w['burn_3h']} %/h · safe {w['safe_burn_per_hour']} %/h · "
                   f"full in {w['eta_full_hours']} h{'  ⚠ BEFORE RESET' if w['hits_before_reset'] else ''}")
        ex = w.get("exchange")
        if ex:
            fams = ", ".join(f"{f} ${v}" + ("?" if ex["spend_usd"].get(f, 0) < DRIFT_MIN_USD else "")
                             for f, v in ex["usd_per_pct"].items() if v)
            out.append(f"   1% = {fams}  (blended ${ex['blended_usd_per_pct']}, σ {ex['sigma_pct']} pts, "
                       f"R² {ex['r2']}, {ex['hours']} h)")
        if w.get("drift_recent_vs_14d"):
            out.append(f"   drift 48h vs 14d: {w['drift_recent_vs_14d']}")
        if w.get("remaining_usd"):
            out.append(f"   left ≈ ${w['remaining_usd']['usd']} of {w['remaining_usd']['family']}")
        if w.get("live_check"):
            lc = w["live_check"]
            out.append(f"   last hour: meter +{lc['meter_pct_1h']}%, local spend explains +{lc['explained_pct_1h']}%")
        if w.get("unexplained_hours_14d"):
            out.append(f"   unexplained hours: " + ", ".join(f"{u['hour']} +{u['excess_pct']}" for u in w["unexplained_hours_14d"]))
    out.append("")
    for a in m["alerts"]:
        out.append(f"! {a['level']}: {a['text']}")
    out.append("")
    out.append("sessions (last hour drain · context · floor cost per turn)")
    for s in m["sessions"][:12]:
        pct = " ".join(f"{k} {v['turn_floor']}%" for k, v in s["pct"].items())
        out.append(f"  ${s['drain_1h_usd']:>6.2f}  ctx {s['ctx_pct'] or 0:>3.0f}% {s['ctx_tokens'] / 1000:>4.0f}k  "
                   f"{(s['title'] or s['session_id'][:8])[:38]:<38} {pct}")
    w7 = m.get("waste_7d") or {}
    if w7:
        out.append("")
        out.append(f"last {w7['days']} d: ${w7['spend_usd']} API-priced · cold cache rebuilds {w7['cold_rebuilds']} "
                   f"(${w7['cold_rebuild_avoidable_usd']} avoidable, {w7['cold_rebuild_share'] * 100:.1f}%) · "
                   f"session overhead {w7['overhead_median_tokens'] / 1000:.0f}k · agents {w7['agent_share'] * 100:.0f}% · "
                   f"output {w7['output_share'] * 100:.0f}% · mix " +
                   ", ".join(f"{f} {v * 100:.0f}%" for f, v in w7["family_share"].items()))
    out.append("")
    for t in m["advice"]:
        out.append(f"→ {t}")
    return "\n".join(out)


if __name__ == "__main__":
    args = sys.argv[1:]
    model = cached(fresh="--fresh" in args or "--json" in args)
    if "--json" in args:
        print(json.dumps(model, indent=1, default=str))
    elif "--brief" in args:
        sid = args[args.index("--brief") + 1] if len(args) > args.index("--brief") + 1 else None
        print(brief(model, sid))
    else:
        print(_report(model))
