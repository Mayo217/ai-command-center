"""Shared helpers for Command Center modules. Keep this tiny and stable; every module imports it."""
from __future__ import annotations
import json, re, subprocess, os
from pathlib import Path

class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message); self.status = status; self.message = message

def run_cmd(argv, timeout: float = 8.0, cwd: str | None = None) -> tuple[int, str, str]:
    """Run a command without a shell. Returns (rc, stdout, stderr); never raises on non-zero exit."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or "") if isinstance(e.stdout, str) else "", f"timeout after {timeout}s"
    except FileNotFoundError as e:
        return 127, "", str(e)

def read_json_file(path, default=None):
    try:
        return json.loads(Path(os.path.expanduser(str(path))).read_text())
    except Exception:
        return default

_SECRET_KEY = re.compile(r"(key|token|secret|password|passwd|auth|cookie|bearer)", re.I)
_SECRET_VAL = re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,}|[A-Za-z0-9_\-]{32,})\b")
def redact_secrets(obj):
    """Recursively mask values whose key looks secret-ish, or long token-like strings."""
    if isinstance(obj, dict):
        return {k: ("•••redacted•••" if _SECRET_KEY.search(str(k)) and isinstance(v, (str, int)) else redact_secrets(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_secrets(v) for v in obj]
    if isinstance(obj, str) and len(obj) >= 32 and _SECRET_VAL.fullmatch(obj):
        return obj[:4] + "…" + obj[-3:]
    return obj

__all__ = ["ApiError", "run_cmd", "read_json_file", "redact_secrets"]
