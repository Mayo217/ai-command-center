"""The few places where Windows and macOS differ, in one file.

Everything else in the tracker stores paths with forward slashes on both systems
(`C:/Users/x/Desktop/proj/file.py` on Windows), so prefix checks and SQL LIKE patterns
written for "/" keep working. Call `posix()` on any path that comes in from outside.
"""
import os
import subprocess
import sys
from pathlib import Path

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"


def posix(p) -> str:
    """Forward-slash form of a path string. A no-op on macOS/Linux."""
    s = str(p)
    return s.replace("\\", "/") if IS_WIN else s


def is_abs(p: str) -> bool:
    """`/x` on any system, `C:/x` or `C:\\x` on Windows."""
    return p.startswith("/") or (IS_WIN and len(p) > 2 and p[1] == ":" and p[2] in "/\\")


def uid() -> int:
    return 0 if IS_WIN else os.getuid()


def has_launchd() -> bool:
    return IS_MAC and Path("/bin/launchctl").exists()


def detached_kwargs() -> dict:
    """Popen kwargs that let a child outlive the terminal that started it."""
    if IS_WIN:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                                 | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def open_path(target: str) -> int:
    """Open a URL or file with the system default app."""
    if IS_WIN:
        try:
            os.startfile(target)  # noqa: E1101 (Windows only)
            return 0
        except OSError:
            return 1
    opener = "open" if IS_MAC else "xdg-open"
    try:
        return subprocess.call([opener, target])
    except FileNotFoundError:
        return 127


def credentials_file() -> Path:
    """Where Claude Code keeps its OAuth login on Windows and Linux (macOS uses the keychain)."""
    return Path.home() / ".claude" / ".credentials.json"


def local_tz():
    """The display timezone: config.json "timezone" as "+05:30"-style offset, else this computer's."""
    import datetime
    import json
    try:
        root = Path(__file__).resolve().parent
        cfg = root / "config.json" if (root / "config.json").exists() else root / "config.default.json"
        off = json.loads(cfg.read_text(encoding="utf-8")).get("timezone")
    except Exception:
        off = None
    if off and off not in ("local", ""):
        sign = -1 if off.startswith("-") else 1
        hh, _, mm = off.lstrip("+-").partition(":")
        return datetime.timezone(sign * datetime.timedelta(hours=int(hh), minutes=int(mm or 0)), off)
    return datetime.datetime.now().astimezone().tzinfo


def tz_label() -> str:
    import datetime
    return datetime.datetime.now(local_tz()).strftime("%Z") or "local"
