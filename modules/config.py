"""Command Center module 5 — Config, hooks, skills.

GET  /api/config/overview    settings.json (redacted), the wired hooks with existence checks,
                             every skill (global + plugin), plugins, default model, versions,
                             keybindings, and the tracker doctor results
GET  /api/config/skill?name= one skill's raw markdown
POST /api/config/skill       {name, content} -> create or edit a GLOBAL skill (keeps a .bak)
POST /api/config/doctor      run tracker.doctor_checks() FRESH and return it (no cache), so the
                             app's "Run doctor" button is a run and not a re-read of a 20s cache

Read-only apart from the skill write, which is confined to ~/.claude/skills/<name>/SKILL.md.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from modules.common import ApiError, read_json_file, redact_secrets, run_cmd  # noqa: E402

HOME = Path.home()
CLAUDE = HOME / ".claude"
SKILLS = CLAUDE / "skills"
PLUGIN_DIRS = [CLAUDE / "plugins" / "cache", CLAUDE / "plugins" / "marketplaces"]
BAK_DIR = Path(__file__).resolve().parent.parent / "logs" / "config-bak"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,60}$")

_CACHE: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: float, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (time.time(), val)
    return val


# ---------------------------------------------------------------- skills
def parse_frontmatter(text: str) -> dict:
    """Minimal YAML frontmatter reader: `key: value` pairs in the leading --- block."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out: dict = {}
    key = None
    for line in text[3:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^(\w[\w.-]*):\s*(.*)$", line)
        if m:
            key = m.group(1)
            out[key] = m.group(2).strip().strip('"').strip("'")
        elif key and line.startswith((" ", "\t")):
            out[key] = (str(out.get(key, "")) + " " + line.strip()).strip()
    return out


def _skill_entry(md: Path, scope: str, plugin: str | None = None) -> dict:
    try:
        text = md.read_text(errors="replace")
        st = md.stat()
    except OSError as e:
        return {"name": md.parent.name, "path": str(md), "scope": scope, "error": str(e)}
    fm = parse_frontmatter(text)
    return {
        "name": fm.get("name") or md.parent.name,
        "description": (fm.get("description") or "")[:600],
        "path": str(md),
        "scope": scope,
        "plugin": plugin,
        "bytes": st.st_size,
        "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
        "lines": text.count("\n") + 1,
        "editable": scope == "global",
    }


def list_skills() -> list[dict]:
    out = []
    if SKILLS.is_dir():
        for d in sorted(SKILLS.iterdir()):
            md = d / "SKILL.md"
            if md.is_file():
                out.append(_skill_entry(md, "global"))
    # Plugin skills are mirrored into several hashed cache dirs; keep one copy per skill name,
    # preferring the readable marketplaces path over a cache hash.
    by_name: dict[str, dict] = {}
    for root in PLUGIN_DIRS:
        if not root.is_dir():
            continue
        for md in sorted(root.glob("*/**/skills/*/SKILL.md")):
            parts = list(md.parts)
            plugin = parts[parts.index("skills") - 1] if "skills" in parts else None
            e = _skill_entry(md, "plugin", plugin)
            key = f"{plugin}/{e['name']}" if plugin and not re.fullmatch(r"[0-9a-f]{8,}", plugin) else e["name"]
            prev = by_name.get(key)
            if prev is None or ("marketplaces" in str(md) and "marketplaces" not in prev["path"]):
                by_name[key] = e
    named = {e["name"] for e in by_name.values() if not re.fullmatch(r"[0-9a-f]{8,}", e.get("plugin") or "")}
    entries = [e for e in by_name.values()
               if not (re.fullmatch(r"[0-9a-f]{8,}", e.get("plugin") or "") and e["name"] in named)]
    out.extend(sorted(entries, key=lambda e: (e.get("plugin") or "", e["name"])))
    return out


# ---------------------------------------------------------------- hooks
def _tokens(cmd) -> list[str]:
    """Split a hook command, honouring quotes ("C:/Program Files/.../python.exe" "C:/x/hook.py")."""
    import shlex
    try:
        return shlex.split(str(cmd))
    except ValueError:
        return str(cmd).split()


def hook_rows(settings: dict) -> list[dict]:
    rows = []
    for event, groups in (settings.get("hooks") or {}).items():
        for g in (groups if isinstance(groups, list) else []):
            for h in (g.get("hooks") or []):
                cmd = h.get("command") or h.get("url") or ""
                script = None
                for tok in _tokens(cmd):
                    if tok.endswith((".py", ".sh", ".js")) and "/" in tok:
                        script = tok
                        break
                rows.append({
                    "event": event,
                    "matcher": g.get("matcher") or "(all)",
                    "type": h.get("type", "command"),
                    "command": str(cmd)[:300],
                    "script": script,
                    "exists": bool(script and Path(script).exists()) if script else None,
                    "timeout": h.get("timeout"),
                    "source": "user settings.json",
                })
    return rows



# ==========================================================================================
# Self-lint of our own Claude surface
# ==========================================================================================
# agnix lints CLAUDE.md / SKILL.md / hooks; medusa watches `.claude/` for tampering. Both halves
# belong here, because RULES.md I4 already forbids editing the hooks to escape the gate — and a
# rule with no check is a wish. Every check below names its own fix.
import hashlib     # noqa: E402
import plistlib    # noqa: E402

SETTINGS_PATH = CLAUDE / "settings.json"
LAUNCHAGENTS = HOME / "Library" / "LaunchAgents"
OUR_LABEL_PREFIX = "com.aicommandcenter."
HOOKS_HASH_KEY = "hooks_block_sha256"
# The plists we own that are long-running daemons: these MUST have KeepAlive true .
# Everything else we own is a scheduled one-shot or a login app.
DAEMON_LABELS = {"com.aicommandcenter.server"}


def _lint_cfg() -> dict:
    return (db.config().get("lint") or {})


def _skill_files() -> list[tuple[str, Path]]:
    """(scope, path) for every SKILL.md we are responsible for. Plugin skills belong to their
    plugin's author and are not linted here — only what lives on this machine by our hand."""
    out: list[tuple[str, Path]] = []
    if SKILLS.is_dir():
        for d in sorted(SKILLS.iterdir()):
            md = d / "SKILL.md"
            if md.is_file():
                out.append(("global", md))
    repo = Path(__file__).resolve().parent.parent
    for md in sorted((repo / ".claude" / "skills").glob("*/SKILL.md")):
        out.append(("project", md))
    return out


def _claude_md_files() -> list[Path]:
    """The CLAUDE.md files that cost tokens on this machine: the global one and one per project
    directly under ~/Desktop. Every turn of every session in that project pays for its size."""
    out = []
    g = CLAUDE / "CLAUDE.md"
    if g.is_file():
        out.append(g)
    desktop = Path(db.config().get("desktop") or (HOME / "Desktop"))
    if desktop.is_dir():
        for d in sorted(desktop.iterdir()):
            try:
                md = d / "CLAUDE.md"
                if d.is_dir() and md.is_file():
                    out.append(md)
            except OSError:
                continue
    return out


def _script_and_interpreter(cmd: str) -> tuple[str | None, str | None]:
    """('/abs/script.py', '/abs/python3') for a hook command line. Either may be None."""
    interp = script = None
    for tok in _tokens(cmd):
        if tok.startswith("-"):
            continue
        if script is None and tok.endswith((".py", ".sh", ".js", ".ts")) and "/" in tok:
            script = tok
        elif interp is None and script is None and __import__('oscompat').is_abs(tok):
            interp = tok
    if script is None and interp is not None:
        script, interp = interp, None       # the command IS the script
    return script, interp


def hooks_block_sha(settings: dict) -> str:
    """A stable hash of the hooks block plus the statusLine command — the two places a session
    could quietly disarm the tracker. Key order is normalised so reformatting is not tampering."""
    payload = {"hooks": settings.get("hooks") or {}, "statusLine": settings.get("statusLine") or {},
               "disableAllHooks": settings.get("disableAllHooks")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"))
                          .encode()).hexdigest()


def lint_claude_surface(fix: bool = False) -> list[dict]:
    """Lint our own Claude Code installation. Returns doctor-shaped rows, each naming its fix."""
    checks: list[dict] = []

    def chk(name, ok, detail="", fix_hint=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail, "fix": fix_hint})

    cfg = _lint_cfg()
    settings = read_json_file(SETTINGS_PATH, None)

    # ---------------------------------------------------------------- 1. SKILL.md frontmatter
    bad: list[str] = []
    min_desc = int(cfg.get("skill_description_min_chars", 40))
    max_bytes = int(cfg.get("skill_max_bytes", 60000))
    skills = _skill_files()
    for scope, md in skills:
        try:
            text = md.read_text(errors="replace")
        except OSError as e:
            bad.append(f"{md.parent.name}: unreadable ({e})")
            continue
        if not text.startswith("---"):
            bad.append(f"{md.parent.name}: no YAML frontmatter block")
            continue
        fm = parse_frontmatter(text)
        name = (fm.get("name") or "").strip()
        desc = (fm.get("description") or "").strip()
        if not name:
            bad.append(f"{md.parent.name}: frontmatter has no `name`")
        elif not NAME_RE.match(name):
            bad.append(f"{md.parent.name}: name {name!r} is not [a-z0-9._-]")
        elif name != md.parent.name:
            bad.append(f"{md.parent.name}: name {name!r} does not match its directory")
        if not desc:
            bad.append(f"{md.parent.name}: frontmatter has no `description`")
        elif len(desc) < min_desc:
            bad.append(f"{md.parent.name}: description is {len(desc)} chars "
                       f"(< {min_desc}; it is what decides whether the skill ever fires)")
        try:
            if md.stat().st_size > max_bytes:
                bad.append(f"{md.parent.name}: SKILL.md is {md.stat().st_size:,} bytes "
                           f"(> {max_bytes:,})")
        except OSError:
            pass
    chk("skills: frontmatter valid", not bad,
        f"{len(skills)} SKILL.md checked" if not bad else "; ".join(bad[:4])
        + (f" (+{len(bad) - 4} more)" if len(bad) > 4 else ""),
        "" if not bad else "edit the SKILL.md's --- block: `name` must be lowercase and match "
                           "its directory, `description` must say WHEN to use the skill")

    # ---------------------------------------------------------------- 2. CLAUDE.md size
    # A CLAUDE.md is re-read on every single turn of every session in its directory, so its size
    # is a tax on every prompt. Two checks, deliberately: the files this system is responsible
    # for are a hard pass/fail; other people's projects are reported but never turn the doctor
    # red, because a permanent red light over a file we do not own is a light nobody reads.
    limit = int(cfg.get("claude_md_max_bytes", 12000))
    repo_root = Path(__file__).resolve().parent.parent
    ours = {str(CLAUDE / "CLAUDE.md"), str(repo_root / "CLAUDE.md")}
    files = _claude_md_files()
    sized = []
    for md in files:
        try:
            sized.append((md, md.stat().st_size))
        except OSError:
            continue
    big_ours = [f"{md} is {n:,} bytes (limit {limit:,})" for md, n in sized
                if str(md) in ours and n > limit]
    big_other = [(md, n) for md, n in sized if str(md) not in ours and n > limit]
    CLAUDE_MD_FIX = ("a CLAUDE.md is re-read on every single turn of every session in that "
                     "directory, so its size is a tax on every prompt, forever. Move the detail "
                     "into a skill (loaded only when it is needed) or a doc the model can choose "
                     "to read, and leave only the rules behind.")
    chk("CLAUDE.md not oversized (ours)", not big_ours,
        f"{len([1 for md, _ in sized if str(md) in ours])} of ours checked, "
        f"largest {max([n for md, n in sized if str(md) in ours] or [0]):,} bytes, "
        f"limit {limit:,}" if not big_ours else "; ".join(big_ours),
        "" if not big_ours else CLAUDE_MD_FIX)
    chk("CLAUDE.md size, other projects (informational)", True,
        f"{len(sized)} file(s) found, all within {limit:,} bytes" if not big_other else
        "over the limit: " + "; ".join(f"{md.parent.name}/CLAUDE.md {n:,}B" for md, n in big_other)
        + " — " + CLAUDE_MD_FIX)

    # ---------------------------------------------------------------- 3. hook commands resolve
    if settings is None:
        chk("hooks: every command resolves", False, f"cannot read {SETTINGS_PATH}",
            "restore ~/.claude/settings.json from a settings.json.bak-* backup beside it")
    else:
        problems = []
        cmds = [(r["event"], r["command"]) for r in hook_rows(settings)]
        sl = (settings.get("statusLine") or {}).get("command")
        if sl:
            cmds.append(("statusLine", sl))
        for event, cmd in cmds:
            script, interp = _script_and_interpreter(cmd)
            if not script:
                problems.append(f"{event}: no script path in {cmd[:60]!r}")
                continue
            sp = Path(script)
            if not sp.exists():
                problems.append(f"{event}: {script} does not exist")
            elif interp is None and not os.access(sp, os.X_OK):
                problems.append(f"{event}: {script} is run directly but is not executable")
            elif interp is not None and not Path(interp).exists():
                problems.append(f"{event}: interpreter {interp} does not exist")
        chk("hooks: every command resolves", not problems,
            f"{len(cmds)} command(s) across {len(settings.get('hooks') or {})} event(s)"
            if not problems else "; ".join(problems[:4]),
            "" if not problems else "fix the path in ~/.claude/settings.json, or `chmod +x` the "
                                    "script. A hook whose command is missing fails open — the "
                                    "gate simply stops firing and nothing says so.")

        # ------------------------------------------------------------ 4. tamper hash
        sha = hooks_block_sha(settings)
        con = db.connect()
        try:
            row = con.execute("SELECT value FROM meta WHERE key=?", (HOOKS_HASH_KEY,)).fetchone()
            stored = json.loads(row["value"]) if row and row["value"] else None
            events = sorted((settings.get("hooks") or {}).keys())
            if stored is None or fix or stored.get("sha256") != sha:
                changed = stored is not None and stored.get("sha256") != sha
                if stored is None or fix:
                    con.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) "
                                "DO UPDATE SET value=excluded.value",
                                (HOOKS_HASH_KEY, json.dumps(
                                    {"sha256": sha, "recorded_at": db.now_iso(),
                                     "events": events})))
                if stored is None:
                    chk("hooks: unchanged since last check", True,
                        f"baseline recorded ({sha[:12]}…, {len(events)} events)")
                elif fix:
                    chk("hooks: unchanged since last check", True,
                        f"re-baselined to {sha[:12]}… ({len(events)} events)")
                else:
                    was = sorted(stored.get("events") or [])
                    delta = ("events " + ", ".join(f"-{e}" for e in was if e not in events)
                             + " " + ", ".join(f"+{e}" for e in events if e not in was)).strip()
                    chk("hooks: unchanged since last check", False,
                        f"the hooks block changed since {stored.get('recorded_at', '?')[:19]} "
                        f"({stored.get('sha256', '')[:12]}… → {sha[:12]}…)"
                        + (f"; {delta}" if delta.strip() != "events" else ""),
                        "The hooks are not to be edited to get past the gate. If YOU "
                        "changed them on purpose, accept the new baseline with "
                        "`tracker doctor --fix`; if you did not, compare against the newest "
                        "~/.claude/settings.json.bak-* and restore.")
            else:
                chk("hooks: unchanged since last check", True,
                    f"{sha[:12]}… matches the baseline recorded "
                    f"{stored.get('recorded_at', '?')[:19]}")
        finally:
            con.close()

        if settings.get("disableAllHooks"):
            chk("hooks: not globally disabled", False,
                '"disableAllHooks": true is set in ~/.claude/settings.json',
                "remove that key — it turns off the Stop gate and every capture hook at once "
                "")
        else:
            chk("hooks: not globally disabled", True, "disableAllHooks is not set")

    # ---------------------------------------------------------------- 5. launchd KeepAlive
    plists: dict[str, Path] = {}
    repo_launchd = Path(__file__).resolve().parent.parent / "launchd"
    for d in (repo_launchd, LAUNCHAGENTS):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("com.aicommandcenter.*.plist")):
            plists[str(f)] = f
    wrong = []
    for path in plists.values():
        try:
            with open(path, "rb") as fh:
                pl = plistlib.load(fh)
        except Exception as e:
            wrong.append(f"{path.name} ({'installed' if LAUNCHAGENTS in path.parents else 'repo'})"
                         f": unreadable ({type(e).__name__})")
            continue
        label = pl.get("Label") or path.stem
        ka = pl.get("KeepAlive")
        where = "installed" if LAUNCHAGENTS in path.parents else "repo"
        if isinstance(ka, dict):
            wrong.append(f"{label} ({where}): KeepAlive is {ka!r}, not the plain boolean true")
        elif label in DAEMON_LABELS and ka is not True:
            wrong.append(f"{label} ({where}): a long-running job with KeepAlive={ka!r}")
        elif ka is True and pl.get("StartCalendarInterval"):
            wrong.append(f"{label} ({where}): a scheduled one-shot with KeepAlive=true "
                         f"(launchd would restart it forever)")
    chk("launchd: KeepAlive shape", not wrong,
        f"{len(plists)} plist(s) checked ({len(DAEMON_LABELS)} daemon label(s))"
        if not wrong else "; ".join(wrong[:4]),
        "" if not wrong else "KeepAlive = {SuccessfulExit: false} "
                             "means 'restart only on a bad exit'. The server exits WELL on an "
                             "orderly shutdown, so launchd left it dead and the menu bar read "
                             "`tracker offline` until somebody noticed by eye. Set the plain "
                             "boolean true for daemons, and omit KeepAlive entirely for "
                             "scheduled one-shots. Then: python3 install.py --autostart")
    return checks


def get_lint(query=None, body=None, match=None):
    """GET /api/config/lint — the self-lint of our own Claude surface, as data."""
    checks = lint_claude_surface(fix=False)
    return 200, {"ok": True, "checks": checks, "green": all(c["ok"] for c in checks),
                 "failing": [c["check"] for c in checks if not c["ok"]],
                 "why": "agnix lints the config surface; medusa watches it for tampering. "
                        "The rule is not to edit the hooks to escape the gate, and a rule with "
                        "no check is a wish."}


# ---------------------------------------------------------------- handlers
def get_overview(query=None, body=None, match=None):
    def build():
        settings_path = CLAUDE / "settings.json"
        settings = read_json_file(settings_path, {}) or {}
        local = read_json_file(CLAUDE / "settings.local.json", {}) or {}
        rc, ver, _ = run_cmd(["claude", "--version"], timeout=10)
        try:
            import tracker
            doctor = tracker.doctor_checks()
        except Exception as e:  # never let a doctor bug take the view down
            doctor = [{"check": "doctor", "ok": False, "detail": f"{type(e).__name__}: {e}"}]
        skills = list_skills()
        return {
            "ok": True,
            "settings_path": str(settings_path),
            "settings": redact_secrets(settings),
            "settings_local": redact_secrets(local),
            "model": settings.get("model") or local.get("model"),
            "plugins": settings.get("enabledPlugins") or {},
            "hooks": hook_rows(settings),
            "hooks_disabled": bool(settings.get("disableAllHooks")),
            "skills": skills,
            "skill_counts": {"global": sum(1 for s in skills if s["scope"] == "global"),
                             "plugin": sum(1 for s in skills if s["scope"] == "plugin")},
            "claude_version": (ver or "").strip() if rc == 0 else None,
            "python": sys.version.split()[0],
            "keybindings": {"path": str(CLAUDE / "keybindings.json"),
                            "exists": (CLAUDE / "keybindings.json").exists()},
            "claude_md": [{"path": str(p), "bytes": p.stat().st_size,
                           "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(p.stat().st_mtime))}
                          for p in [CLAUDE / "CLAUDE.md"] if p.exists()],
            "doctor": doctor,
            "doctor_ok": all(c["ok"] for c in doctor),
            "generated_at": db.now_iso(),
        }
    return 200, _cached("overview", 20.0, build)


def _skill_path(name: str) -> Path:
    if not NAME_RE.match(name or ""):
        raise ApiError(400, "skill name must be lowercase letters, digits, dot, dash or underscore")
    return SKILLS / name / "SKILL.md"


def get_skill(query=None, body=None, match=None):
    query = query or {}
    name = query.get("name") or ""
    p = _skill_path(name)
    if not p.exists():
        raise ApiError(404, f"no global skill named {name}")
    text = p.read_text(errors="replace")
    return 200, {"ok": True, "name": name, "path": str(p), "content": text,
                 "frontmatter": parse_frontmatter(text)}


def post_skill(query=None, body=None, match=None):
    body = body or {}
    name = str(body.get("name", "")).strip()
    content = body.get("content")
    if not isinstance(content, str) or len(content.strip()) < 40:
        raise ApiError(400, "content must be the full SKILL.md text (>= 40 chars)")
    if not content.lstrip().startswith("---"):
        raise ApiError(400, "a skill must start with YAML frontmatter containing name: and description:")
    fm = parse_frontmatter(content)
    if not fm.get("name") or not fm.get("description"):
        raise ApiError(400, "frontmatter needs both name: and description:")
    p = _skill_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    if existed:
        BAK_DIR.mkdir(parents=True, exist_ok=True)
        bak = BAK_DIR / f"skill-{name}-{time.strftime('%Y%m%d-%H%M%S')}.md"
        bak.write_text(p.read_text(errors="replace"))
    p.write_text(content)
    _CACHE.pop("overview", None)
    con = db.connect()
    db.add_event(con, "cc", "action", {"module": "config", "action": "write_skill", "name": name,
                                       "existed": existed, "bytes": len(content)})
    return 200, {"ok": True, "name": name, "path": str(p), "created": not existed,
                 "backup": str(bak) if existed else None}


def post_doctor(query=None, body=None, match=None):
    """POST /api/config/doctor — run the checks now, bypassing the overview's 20 s cache.

    The overview embeds `doctor`/`doctor_ok` from the same function; this route exists so the
    app's button is honest. `tracker.doctor_checks()` is imported, never shelled out, so the
    app sees the same objects `tracker doctor --json` prints rather than scraped terminal text.
    """
    try:
        import tracker
    except Exception as exc:
        raise ApiError(500, f"cannot import tracker: {type(exc).__name__}: {exc}")
    try:
        checks = tracker.doctor_checks()
    except Exception as exc:
        raise ApiError(500, f"doctor failed: {type(exc).__name__}: {exc}")
    if not isinstance(checks, list):
        raise ApiError(500, "doctor_checks() did not return a list")
    ok = all(bool(c.get("ok")) for c in checks)
    ran_at = db.now_iso()
    # The cached overview now holds a stale doctor run; drop it so the next read agrees.
    _CACHE.pop("overview", None)
    try:
        con = db.connect()
        db.add_event(con, "cc", "action", {"module": "config", "action": "doctor",
                                           "ok": ok, "checks": len(checks),
                                           "failing": [c.get("check") for c in checks if not c.get("ok")]})
    except Exception:
        pass  # an audit failure must never take the check run down
    return 200, {"ok": True, "doctor": checks, "doctor_ok": ok,
                 "count": len(checks), "ran_at": ran_at}


ROUTES = [
    ("GET", r"/api/config/overview/?$", get_overview),
    ("GET", r"/api/config/lint/?$", get_lint),
    ("GET", r"/api/config/skill/?$", get_skill),
    ("POST", r"/api/config/skill/?$", post_skill),
    ("POST", r"/api/config/doctor/?$", post_doctor),
]

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Command Center config module — settings, hooks, skills, doctor.")
    ap.add_argument("--skill", help="print one global skill instead")
    a = ap.parse_args()
    res = get_skill({"name": a.skill})[1] if a.skill else get_overview()[1]
    print(json.dumps(res, indent=2, default=str))
