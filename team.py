"""Team ledger: bring employees' Claude work into the owner's Command Center.

Each employee runs the same kit on their own computer. Once a day (or week) their machine drops one
JSON file into a shared folder. The owner's machine reads every file in that folder into its ledger.
The shared folder is any folder both computers can see: a shared OneDrive / Google Drive / Dropbox
folder, or a network share. No server, no accounts, nothing sent anywhere else.

    Employee:  python team.py send    --name Ravi --to "C:/Users/ravi/OneDrive/ClaudeTeam"
    Owner:     python team.py collect --from "C:/Users/me/OneDrive/ClaudeTeam"
    Automatic: python team.py schedule send --name Ravi --to "..." [--weekly] [--time 18:30]
               python team.py schedule collect --from "..." [--time 19:30]

Profiles (owner side): team.json in this folder is the roster. Each person has an id, a name, a role
and the Claude logins (emails) that are theirs. A file is mapped to a person by the Claude login it
was sent from, then by the sender name, then by the machine. A file from someone not on the roster
creates a profile automatically from their Claude account (set "auto_add": false to hold such files
until you add the person yourself).

    python team.py add ravi --name "Ravi Kumar" --role Accounts --email ravi@company.com
    python team.py people            # the roster, one line each, with last file and meter
    python team.py profile ravi      # one person: logins, machines, plan, meter, 7/30-day work, recent items

What a file holds: the sender's Claude login (email, name, organisation, plan; never a token), their
latest usage meter reading, the items they logged (title, summary, usage, kind, project, file paths,
links, tags) and per-session counts and cost. It never holds prompts, transcripts or file contents.
"""
import argparse
import datetime
import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import db  # noqa: E402
import oscompat  # noqa: E402

FORMAT = "claude-team-v1"
MAX_FILE_BYTES = 5_000_000  # a day of one person's work is a few KB
ROSTER = Path(os.environ.get("CLAUDE_TEAM_ROSTER") or ROOT / "team.json")
ACCOUNT_KEYS = ("emailAddress", "fullName", "displayName", "organizationName", "organizationUuid",
                "accountUuid", "billingType", "seatTier", "userRateLimitTier", "organizationRole")
SESSION_COLS = ("session_id", "agent", "project", "model", "started_at", "ended_at", "user_turns", "tool_calls",
                "writes", "edits", "cost_usd", "logged", "waived_at", "gate_blocks", "gate_bypassed",
                "updated_at")


def _meta(con, key, value=None):
    if value is None:
        r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))


def _utc_minus(days: float) -> str:
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def claude_account() -> dict:
    """Who this Claude Code is logged in as, from ~/.claude.json (identity only, no tokens)."""
    try:
        o = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8")).get("oauthAccount") or {}
    except Exception:
        o = {}
    return {k: o.get(k) for k in ACCOUNT_KEYS if o.get(k) is not None}


def latest_meter(con) -> dict | None:
    r = con.execute("SELECT ts, five_hour_pct, five_hour_resets_at, seven_day_pct, seven_day_resets_at "
                    "FROM rate_limit_samples ORDER BY ts DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def _safe(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in "-_ ").strip() or "unknown"


# ------------------------------------------------------------------ employee side
def send(a) -> int:
    con = db.connect()
    db.migrate(con)
    name = _safe(a.name)
    last = _meta(con, "team_last_send")
    if a.days:
        since = _utc_minus(a.days)
    elif last:  # one day of overlap: a re-sent item just updates on the owner's side
        since = (datetime.datetime.fromisoformat(last[:19]) - datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    else:
        since = _utc_minus(7)

    items = []
    for it in con.execute("SELECT * FROM items WHERE updated_at >= ? AND source != 'team' ORDER BY id", (since,)):
        d = {k: it[k] for k in ("id", "agent", "created_at", "updated_at", "built_at", "kind", "title", "summary",
                                "usage", "project", "status", "confidence", "source", "model", "cost_usd")}
        d["tags"] = json.loads(it["tags"] or "[]")
        d["files"] = [{"path": r["path"], "role": r["role"]} for r in
                      con.execute("SELECT path, role FROM item_files WHERE item_id=?", (it["id"],))]
        d["links"] = [{"url": r["url"], "label": r["label"]} for r in
                      con.execute("SELECT url, label FROM item_links WHERE item_id=?", (it["id"],))]
        items.append(d)
    sessions = [{k: s[k] for k in SESSION_COLS} for s in con.execute(
        f"SELECT {','.join(SESSION_COLS)} FROM sessions WHERE source IN ('live', 'agent-log') AND updated_at >= ?", (since,))]

    now = db.now_iso()
    payload = {"format": FORMAT, "account": name, "machine": socket.gethostname(), "sent_at": now,
               "since": since, "claude_account": claude_account(), "meter": latest_meter(con),
               "items": items, "sessions": sessions}
    out_dir = Path(os.path.expanduser(a.to)) / name
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{name}_{datetime.date.today().isoformat()}.json"
    tmp = out.with_suffix(".json.part")  # cloud sync never sees a half-written file
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.replace(tmp, out)
    _meta(con, "team_last_send", now)
    con.commit()
    print(f"sent {len(items)} item(s), {len(sessions)} session(s) -> {out}")
    return 0


# ------------------------------------------------------------------ roster (owner side)
def load_roster() -> dict:
    try:
        r = json.loads(ROSTER.read_text(encoding="utf-8"))
    except FileNotFoundError:
        r = {}
    r.setdefault("auto_add", True)
    r.setdefault("people", [])
    return r


def save_roster(r: dict):
    ROSTER.write_text(json.dumps(r, indent=2), encoding="utf-8")


def _slug(s: str) -> str:
    return "".join(c for c in s.lower().replace(" ", "-") if c.isalnum() or c == "-").strip("-") or "unknown"


def find_person(roster, email=None, sender=None, machine=None):
    """Claude login first (the strongest proof of who it is), then sender name, then machine."""
    email, sender = (email or "").lower(), (sender or "").lower()
    # A file that names its Claude login is matched on that login only: a shared office PC or a
    # reused sender name must never fold a new person into someone else's profile.
    keys = (("claude_emails", email),) if email else (("senders", sender), ("machines", (machine or "").lower()))
    for key, val in keys:
        if not val:
            continue
        for p in roster["people"]:
            if val in [x.lower() for x in p.get(key) or []]:
                return p
    return None


def map_sender(roster, payload) -> dict | None:
    acct = payload.get("claude_account") or {}
    email, sender, machine = acct.get("emailAddress"), payload.get("account"), payload.get("machine")
    p = find_person(roster, email, sender, machine)
    if p is None and roster.get("auto_add"):
        name = acct.get("fullName") or acct.get("displayName") or sender or "unknown"
        pid, n = _slug(name.split()[0] if name else sender), 2
        while any(x["id"] == pid for x in roster["people"]):
            pid, n = f"{_slug(name)}-{n}", n + 1
        p = {"id": pid, "name": name, "role": "", "claude_emails": [], "senders": [], "machines": [],
             "auto_added": db.now_iso()[:10]}
        roster["people"].append(p)
        print(f"  new profile '{pid}' ({name}, {email or 'no Claude login in file'}); "
              f"set a role with: team.py add {pid} --role ...")
    if p is None:
        return None
    for key, val in (("claude_emails", email), ("senders", sender), ("machines", machine)):
        if val and val.lower() not in [x.lower() for x in p.setdefault(key, [])]:
            p[key].append(val)  # every login and machine seen is remembered on the profile
    plan = {k: acct.get(k) for k in ("organizationName", "billingType", "seatTier",
                                     "userRateLimitTier", "organizationRole") if acct.get(k)}
    if plan:
        p["claude_account"] = {k: acct.get(k) for k in ("organizationName", "billingType", "seatTier",
                                                         "userRateLimitTier", "organizationRole") if acct.get(k)}
    return p


# ------------------------------------------------------------------ owner side
def _upsert_item(con, acct, machine, it) -> str:  # acct = the person's roster id
    origin = f"{acct}/{machine}/{it['id']}"
    row = con.execute("SELECT id FROM items WHERE source='team' AND json_extract(extra,'$.team_origin')=?",
                      (origin,)).fetchone()
    tags = sorted(set((it.get("tags") or []) + ["team", acct]))
    project = f"{acct}/{it.get('project') or 'unknown'}"
    extra = json.dumps({"team_origin": origin, "team_account": acct, "team_machine": machine,
                        "remote_source": it.get("source")})
    vals = (it.get("built_at") or it.get("created_at"), it.get("kind") or "build", it["title"],
            it.get("summary") or "", it.get("usage"), project, it.get("status") or "active",
            it.get("confidence") or "confirmed", json.dumps(tags), it.get("model"), it.get("cost_usd"), extra)
    if row:
        iid = row["id"]
        con.execute("UPDATE items SET built_at=?, kind=?, title=?, summary=?, usage=?, project=?, status=?, "
                    "confidence=?, tags=?, model=?, cost_usd=?, extra=?, updated_at=? WHERE id=?",
                    vals + (db.now_iso(), iid))
        con.execute("DELETE FROM item_files WHERE item_id=?", (iid,))
        con.execute("DELETE FROM item_links WHERE item_id=?", (iid,))
        how = "updated"
    else:
        iid = con.execute("INSERT INTO items(built_at, kind, title, summary, usage, project, status, confidence, "
                          "tags, model, cost_usd, extra, source, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'team',?)",
                          vals + (it.get("created_at") or db.now_iso(),)).lastrowid
        how = "new"
    for f in it.get("files") or []:
        con.execute("INSERT OR IGNORE INTO item_files(item_id, path, role, exists_at_log) VALUES(?,?,?,NULL)",
                    (iid, f["path"], f.get("role") or "modified"))
    for l in it.get("links") or []:
        if not str(l.get("url", "")).lower().startswith(("http://", "https://")):
            continue  # only web links from someone else's file
        con.execute("INSERT OR IGNORE INTO item_links(item_id, url, label) VALUES(?,?,?)",
                    (iid, l["url"], l.get("label")))
    con.execute("UPDATE items SET agent=? WHERE id=?", (it.get("agent") or "claude", iid))
    db.reindex_item(con, iid)
    return how


def _upsert_session(con, acct, s):
    sid = f"team:{acct}:{s['session_id']}"
    cols = [c for c in SESSION_COLS if c != "session_id"]
    vals = [f"{acct}/{s.get('project') or 'unknown'}" if c == "project" else s.get(c) for c in cols]
    con.execute(f"INSERT OR REPLACE INTO sessions(session_id, source, {','.join(cols)}) "
                f"VALUES(?, 'team', {','.join('?' * len(cols))})", [sid] + vals)


def collect(a) -> int:
    con = db.connect()
    db.migrate(con)
    root = Path(os.path.expanduser(a.from_))
    if not root.is_dir():
        print(f"error: {root} is not a folder (is the shared folder synced on this computer?)", file=sys.stderr)
        return 1
    files = sorted(root.rglob("*.json"))
    roster = load_roster()
    counts = {"files": 0, "skipped": 0, "new": 0, "updated": 0, "sessions": 0, "bad": 0, "held": 0}
    for f in files:
        if f.stat().st_size > MAX_FILE_BYTES:
            counts["bad"] += 1
            print(f"  skipped {f.name}: larger than {MAX_FILE_BYTES // 1_000_000} MB")
            continue
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        key = "team_seen:" + oscompat.posix(f.relative_to(root))
        if _meta(con, key) == digest:
            counts["skipped"] += 1
            continue
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
            assert p.get("format") == FORMAT
        except Exception:
            counts["bad"] += 1
            print(f"  skipped {f.name}: not a {FORMAT} file")
            continue
        person = map_sender(roster, p)
        if person is None:
            counts["held"] += 1
            print(f"  held {f.name}: sender {p.get('account')!r} / {(p.get('claude_account') or {}).get('emailAddress')} "
                  f"is not on the roster (auto_add is off). Add them: team.py add <id> --email ...")
            continue
        acct, machine = person["id"], p.get("machine") or "?"
        for it in p.get("items") or []:
            if it.get("title"):
                counts[_upsert_item(con, acct, machine, it)] += 1
        for s in p.get("sessions") or []:
            if s.get("session_id"):
                _upsert_session(con, acct, s)
                counts["sessions"] += 1
        _meta(con, key, digest)
        _meta(con, f"team_last_from:{acct}", p.get("sent_at") or "")
        if p.get("meter"):
            _meta(con, f"team_meter:{acct}", json.dumps(p["meter"]))
        counts["files"] += 1
        con.commit()
    save_roster(roster)
    print(f"read {counts['files']} new file(s) ({counts['skipped']} already read, {counts['held']} held, {counts['bad']} bad): "
          f"{counts['new']} new item(s), {counts['updated']} updated, {counts['sessions']} session row(s)")
    return 0


def _meter_text(con, pid) -> str:
    m = _meta(con, f"team_meter:{pid}")
    if not m:
        return "meter n/a"
    m = json.loads(m)
    fmt = lambda v: "-" if v is None else f"{v:.0f}%"
    return f"5h {fmt(m.get('five_hour_pct'))} · week {fmt(m.get('seven_day_pct'))} (at {str(m.get('ts'))[:16].replace('T', ' ')} UTC)"


def _stats(con, pid, days):
    since = _utc_minus(days)
    s = con.execute("SELECT count(*) n, coalesce(sum(cost_usd),0) cost, sum(logged=0 AND waived_at IS NULL) unlogged, "
                    "coalesce(sum(gate_bypassed),0) byp FROM sessions WHERE source='team' AND session_id LIKE ? "
                    "AND coalesce(started_at, updated_at) >= ?", (f"team:{pid}:%", since)).fetchone()
    n = con.execute("SELECT count(*) n FROM items WHERE source='team' AND json_extract(extra,'$.team_account')=? "
                    "AND coalesce(built_at, created_at) >= ?", (pid, since)).fetchone()["n"]
    return s, n


def people(a) -> int:
    con = db.connect()
    db.migrate(con)
    r = load_roster()
    if not r["people"]:
        print("no one on the roster yet: team.py add <id> --name ... --email ...  (or just collect: auto_add is on)")
    for p in r["people"]:
        last = (_meta(con, f"team_last_from:{p['id']}") or "never")[:16].replace("T", " ")
        s, n = _stats(con, p["id"], 7)
        print(f"{p['id']:<12} {p.get('name', ''):<20} {p.get('role') or '-':<14} last file {last:<16} · "
              f"7d: {n} item(s), {s['n']} session(s), ${s['cost']:.2f} · {_meter_text(con, p['id'])}")
    return 0


def profile(a) -> int:
    con = db.connect()
    db.migrate(con)
    p = next((x for x in load_roster()["people"] if x["id"] == a.id), None)
    if not p:
        print(f"no profile '{a.id}'. team.py people lists them.", file=sys.stderr)
        return 1
    print(f"{p.get('name')}  ({p['id']})  {p.get('role') or ''}")
    print(f"  Claude logins : {', '.join(p.get('claude_emails') or []) or '-'}")
    ca = p.get("claude_account") or {}
    if ca:
        print(f"  plan          : {', '.join(f'{k}={v}' for k, v in ca.items())}")
    print(f"  machines      : {', '.join(p.get('machines') or []) or '-'}")
    print(f"  sends as      : {', '.join(p.get('senders') or []) or '-'}")
    last = _meta(con, f"team_last_from:{p['id']}") or "never"
    print(f"  last file     : {last[:16].replace('T', ' ')} UTC")
    print(f"  usage meter   : {_meter_text(con, p['id'])}")
    for days in (7, 30):
        s, n = _stats(con, p["id"], days)
        print(f"  last {days:>2} days  : {n} item(s) logged, {s['n']} session(s), ${s['cost']:.2f}, "
              f"{s['unlogged'] or 0} session(s) never logged, {s['byp']} gate bypass(es)")
    rows = con.execute("SELECT id, coalesce(built_at, created_at) t, kind, title FROM items WHERE source='team' "
                       "AND json_extract(extra,'$.team_account')=? ORDER BY t DESC LIMIT 8", (p["id"],)).fetchall()
    if rows:
        print("  recent items  :")
        for r in rows:
            print(f"    #{r['id']:<5} {r['t'][:10]}  {r['kind']:<11} {r['title']}")
    return 0


def add(a) -> int:
    r = load_roster()
    pid = _slug(a.id)
    p = next((x for x in r["people"] if x["id"] == pid), None)
    if p is None:
        p = {"id": pid, "name": a.name or a.id, "role": "", "claude_emails": [], "senders": [], "machines": []}
        r["people"].append(p)
    if a.name:
        p["name"] = a.name
    if a.role is not None:
        p["role"] = a.role
    for key, vals in (("claude_emails", a.email), ("senders", a.sender), ("machines", a.machine)):
        for v in vals or []:
            if v.lower() not in [x.lower() for x in p.setdefault(key, [])]:
                p[key].append(v)
    p.pop("auto_added", None)
    save_roster(r)
    print(f"saved profile '{pid}': {json.dumps({k: p[k] for k in ('name', 'role', 'claude_emails', 'senders', 'machines')})}")
    return 0


# ------------------------------------------------------------------ automatic
def schedule(a) -> int:
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    exe = str(pyw if oscompat.IS_WIN and pyw.exists() else py)
    me = oscompat.posix(Path(__file__).resolve())
    if a.what == "send":
        if not (a.name and a.to):
            print("error: schedule send needs --name and --to", file=sys.stderr)
            return 2
        args, task = f'send --name "{a.name}" --to "{a.to}"', "ClaudeTeamSend"
    else:
        if not a.from_:
            print("error: schedule collect needs --from", file=sys.stderr)
            return 2
        args, task = f'collect --from "{a.from_}"', "ClaudeTeamCollect"
    when = ["/SC", "WEEKLY", "/D", "FRI"] if a.weekly else ["/SC", "DAILY"]
    if oscompat.IS_WIN:
        rc = subprocess.call(["schtasks", "/Create", "/F", "/TN", task, *when, "/ST", a.time,
                              "/TR", f'"{exe}" "{me}" {args}'])
        print(f"task {task} {'created' if rc == 0 else 'FAILED'}: {' '.join(when[1:])} at {a.time}")
        print(f"check it: schtasks /Query /TN {task}    remove it: schtasks /Delete /F /TN {task}")
        return rc
    hh, mm = a.time.split(":")
    print("Not Windows. Add this line with `crontab -e`:")
    print(f'{int(mm)} {int(hh)} * * {"5" if a.weekly else "*"} "{exe}" "{me}" {args}')
    return 0


def main():
    ap = argparse.ArgumentParser(description="Team ledger: employees send, the owner collects")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("send", help="employee: write this computer's recent work to the shared folder")
    sp.add_argument("--name", required=True, help="the employee's name, used as their folder")
    sp.add_argument("--to", required=True, help="the shared folder")
    sp.add_argument("--days", type=int, help="send the last N days instead of 'since the last send'")
    sp = sub.add_parser("collect", help="owner: read every team file in the shared folder")
    sp.add_argument("--from", dest="from_", required=True)
    sub.add_parser("people", help="owner: the roster, with last file, 7-day work and meter")
    sp = sub.add_parser("profile", help="owner: one person in detail")
    sp.add_argument("id")
    sp = sub.add_parser("add", help="owner: add or update a person on the roster")
    sp.add_argument("id", help="short id, e.g. ravi")
    sp.add_argument("--name")
    sp.add_argument("--role")
    sp.add_argument("--email", action="append", help="a Claude login of theirs (repeatable)")
    sp.add_argument("--sender", action="append", help="the --name their machine sends as (repeatable)")
    sp.add_argument("--machine", action="append", help="a computer name of theirs (repeatable)")
    sp = sub.add_parser("schedule", help="run send or collect automatically (Windows Task Scheduler)")
    sp.add_argument("what", choices=["send", "collect"])
    sp.add_argument("--name")
    sp.add_argument("--to")
    sp.add_argument("--from", dest="from_")
    sp.add_argument("--weekly", action="store_true", help="Fridays instead of daily")
    sp.add_argument("--time", default="18:30", help="HH:MM, default 18:30 (send) - collect an hour later")
    a = ap.parse_args()
    sys.exit({"send": send, "collect": collect, "people": people, "profile": profile, "add": add,
              "schedule": schedule}[a.cmd](a))


if __name__ == "__main__":
    main()
