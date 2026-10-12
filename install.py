"""Install the AI Command Center for this user. Windows first; also runs on macOS/Linux.

    python install.py              # install (safe to run again)
    python install.py --autostart  # also start the server at logon + a daily reindex (Task Scheduler / launchd)
    python install.py --uninstall  # take our hooks/statusLine back out of settings.json

It never overwrites your settings: it backs up ~/.claude/settings.json, then ADDS its hook entries
next to whatever is already there. An existing statusLine is left alone (you are told).
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import oscompat  # noqa: E402

HOME = Path.home()
CLAUDE = HOME / ".claude"
SETTINGS = CLAUDE / "settings.json"
MARK = "ai-command-center-hook"   # every command we add carries this, so we can find our own entries

HOOKS = [  # (event, matcher, script, timeout)
    ("SessionStart", None, "session_start.py", 10),
    ("UserPromptSubmit", None, "user_prompt_submit.py", 10),
    ("PostToolUse", "Write|Edit|MultiEdit|NotebookEdit", "post_tool_use.py", 10),
    ("PostToolUse", "Bash", "post_tool_use.py", 10),
    ("Stop", None, "stop_gate.py", 10),
    ("SessionEnd", None, "session_end.py", 10),
    ("SubagentStop", None, "subagent_stop.py", 10),
    ("PreCompact", None, "pre_compact.py", 10),
    ("Notification", None, "notification.py", 5),
    ("PermissionRequest", None, "permission_request.py", 5),
]


def say(msg):
    print("  " + msg)


def python_exe() -> str:
    return sys.executable


def cmd_for(script: Path) -> str:
    # Quoted for paths with spaces; the trailing marker is a harmless extra argv the hooks ignore.
    return f'"{python_exe()}" "{oscompat.posix(script)}" --{MARK}'


def find_desktop() -> Path:
    """OneDrive often moves the Windows Desktop; prefer that one when it exists."""
    for env in ("OneDriveConsumer", "OneDrive", "OneDriveCommercial"):
        base = os.environ.get(env)
        if base and (Path(base) / "Desktop").is_dir():
            return Path(base) / "Desktop"
    if (HOME / "OneDrive" / "Desktop").is_dir():
        return HOME / "OneDrive" / "Desktop"
    return HOME / "Desktop"


def write_config():
    p = ROOT / "config.json"  # your copy (git-ignored); config.default.json is the shipped default
    cfg = json.loads((p if p.exists() else ROOT / "config.default.json").read_text(encoding="utf-8"))
    cfg["python"] = python_exe()
    cfg["desktop"] = oscompat.posix(find_desktop())
    cfg["projects_dir"] = oscompat.posix(CLAUDE / "projects")
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    say(f"config.json: python={cfg['python']}  desktop={cfg['desktop']}")


def load_settings() -> dict:
    if not SETTINGS.exists():
        return {}
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        sys.exit(f"error: {SETTINGS} is not valid JSON; fix it by hand first, nothing was changed.")


def save_settings(s: dict):
    CLAUDE.mkdir(parents=True, exist_ok=True)
    if SETTINGS.exists():
        bak = SETTINGS.with_name("settings.json.bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(SETTINGS, bak)
        say(f"backed up settings to {bak.name}")
    SETTINGS.write_text(json.dumps(s, indent=2), encoding="utf-8")


def strip_ours(s: dict):
    hooks = s.get("hooks") or {}
    for ev in list(hooks):
        groups = []
        for g in hooks[ev]:
            kept = [h for h in g.get("hooks", []) if MARK not in str(h.get("command", ""))]
            if kept:
                groups.append({**g, "hooks": kept})
        if groups:
            hooks[ev] = groups
        else:
            del hooks[ev]
    if MARK in str((s.get("statusLine") or {}).get("command", "")):
        del s["statusLine"]


def merge_hooks():
    s = load_settings()
    strip_ours(s)  # re-running replaces our entries instead of duplicating them
    hooks = s.setdefault("hooks", {})
    for ev, matcher, script, timeout in HOOKS:
        g = {"hooks": [{"type": "command", "command": cmd_for(ROOT / "hooks" / script), "timeout": timeout}]}
        if matcher:
            g = {"matcher": matcher, **g}
        hooks.setdefault(ev, []).append(g)
    if s.get("statusLine"):
        say("statusLine: you already have one, left alone (the usage meter samples limits from ours;"
            " to use it, remove yours and run install again)")
    else:
        s["statusLine"] = {"type": "command", "command": cmd_for(ROOT / "hooks" / "statusline.py"), "padding": 0}
        say("statusLine: installed")
    save_settings(s)
    say(f"hooks: {len(HOOKS)} entries added to {SETTINGS}")


def write_shim():
    py, tr = python_exe(), ROOT / "tracker.py"
    targets = []
    if oscompat.IS_WIN:
        body = f'@echo off\r\n"{py}" "{tr}" %*\r\n'
        scripts = Path(sysconfig.get_path("scripts"))  # on PATH when Python was installed with "Add to PATH"
        targets = [HOME / ".local" / "bin" / "tracker.cmd", scripts / "tracker.cmd"]
    else:
        body = f'#!/bin/sh\nexec "{py}" "{tr}" "$@"\n'
        targets = [HOME / ".local" / "bin" / "tracker"]
    for t in targets:
        try:
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_text(body, encoding="utf-8", newline="")
            if not oscompat.IS_WIN:
                t.chmod(0o755)
            say(f"command: {t}")
        except OSError as e:
            say(f"command: could not write {t} ({e})")
    if not shutil.which("tracker"):
        say(f"note: `tracker` is not on PATH yet. Add {targets[0].parent} to PATH, or open a new terminal.")


def install_skill_and_rule():
    dst = CLAUDE / "skills" / "log-build" / "SKILL.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "skills" / "log-build" / "SKILL.md", dst)
    say(f"skill: {dst}")
    md = CLAUDE / "CLAUDE.md"
    text = md.read_text(encoding="utf-8") if md.exists() else ""
    if "Build ledger (mandatory)" in text:
        say("CLAUDE.md: build-ledger paragraph already there")
    else:
        md.write_text(text + (ROOT / "skills" / "CLAUDE_md_snippet.md").read_text(encoding="utf-8"),
                      encoding="utf-8")
        say(f"CLAUDE.md: build-ledger paragraph appended to {md}")


def init_db():
    import db
    con = db.connect()
    db.migrate(con)
    say(f"database: {db.db_path()}")


def autostart_mac():
    import plistlib
    agents = HOME / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    jobs = {
        "com.aicommandcenter.server": {"ProgramArguments": [python_exe(), str(ROOT / "server.py")],
                                            "RunAtLoad": True, "KeepAlive": True},
        "com.aicommandcenter.reindex": {"ProgramArguments": [python_exe(), str(ROOT / "jobs" / "reindex.py")],
                                                    "StartCalendarInterval": {"Hour": 5, "Minute": 45}},
    }
    for label, spec in jobs.items():
        f = agents / f"{label}.plist"
        plistlib.dump({"Label": label, "WorkingDirectory": str(ROOT),
                       "StandardOutPath": str(logs / f"{label}.out"),
                       "StandardErrorPath": str(logs / f"{label}.err"), **spec}, f.open("wb"))
        if os.environ.get("CCC_NO_LAUNCHCTL"):  # tests: write the plist, load nothing
            say(f"autostart: wrote {f.name} (not loaded)")
            continue
        subprocess.call(["/bin/launchctl", "bootout", f"gui/{oscompat.uid()}/{label}"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rc = subprocess.call(["/bin/launchctl", "bootstrap", f"gui/{oscompat.uid()}", str(f)])
        say(f"autostart: {label} {'loaded' if rc == 0 else 'FAILED (rc=%d)' % rc}")


def autostart():
    if oscompat.IS_MAC:
        autostart_mac()
        return
    if not oscompat.IS_WIN:
        say("autostart: on Linux add `python server.py` to your session start-up (systemd --user or cron @reboot)")
        return
    pyw = Path(python_exe()).with_name("pythonw.exe")
    py = str(pyw if pyw.exists() else python_exe())
    for name, sched, script in (("AICommandCenter", ["/SC", "ONLOGON"], ROOT / "server.py"),
                                ("AICommandCenterReindex", ["/SC", "DAILY", "/ST", "05:45"],
                                 ROOT / "jobs" / "reindex.py")):
        rc = subprocess.call(["schtasks", "/Create", "/F", "/TN", name, *sched,
                              "/TR", f'"{py}" "{script}"'])
        say(f"autostart: task {name} {'created' if rc == 0 else 'FAILED (rc=%d)' % rc}")


def uninstall():
    s = load_settings()
    strip_ours(s)
    save_settings(s)
    say("hooks and statusLine removed from settings.json (skill, CLAUDE.md text and tracker.db kept)")
    if oscompat.IS_MAC:
        for label in ("com.aicommandcenter.server", "com.aicommandcenter.reindex"):
            subprocess.call(["/bin/launchctl", "bootout", f"gui/{oscompat.uid()}/{label}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (HOME / "Library" / "LaunchAgents" / f"{label}.plist").unlink(missing_ok=True)
    if oscompat.IS_WIN:
        for name in ("AICommandCenter", "AICommandCenterReindex"):
            subprocess.call(["schtasks", "/Delete", "/F", "/TN", name],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    ap = argparse.ArgumentParser(description="Install the AI Command Center")
    ap.add_argument("--autostart", action="store_true", help="server at logon + daily reindex (Windows Task Scheduler, macOS launchd)")
    ap.add_argument("--uninstall", action="store_true")
    a = ap.parse_args()
    if sys.version_info < (3, 12):
        sys.exit("error: Python 3.12 or newer is needed (python.org, tick 'Add python.exe to PATH').")
    print("AI Command Center install")
    if a.uninstall:
        uninstall()
        return
    write_config()
    init_db()
    merge_hooks()
    write_shim()
    install_skill_and_rule()
    if a.autostart:
        autostart()
    print("Done. Restart Claude Code, then run:  tracker doctor   and   tracker open")


if __name__ == "__main__":
    main()
