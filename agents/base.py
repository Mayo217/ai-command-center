"""The adapter contract: one small class per AI tool.

An adapter says what the Command Center can do with a tool and does it:

    capture  - read the tool's own session logs into the ledger (`tracker agents scan`)
    rules    - teach the tool the ledger protocol through its instruction file (`tracker agents connect`)
    gate     - the tool can be stopped until its work is logged (only tools with blocking hooks)
    usage    - the tool's plan limits or token use can be read

Every tool that can run a shell command can keep the ledger: `connect` writes a short block into
its instruction file telling it to run `tracker log --agent <name> ...`. Adapters that also read
the tool's logs (`scan`) add sessions, files touched and usage on top.

To add a tool: `tracker agents new <name>`, then fill in the generated file.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

HOME = Path.home()
BEGIN = "<!-- ai-command-center:begin -->"
END = "<!-- ai-command-center:end -->"


def rules_block(agent: str) -> str:
    return f"""{BEGIN}
## Build ledger
This computer keeps a ledger of what AI sessions build (AI Command Center, CLI `tracker`).
- Before building, run `tracker recent --cwd .` or `tracker search "<words>"`. If the thing already exists, extend it instead of rebuilding it.
- After you finish each distinct thing (script, page, config, doc, pipeline), record it:
  `tracker log --agent {agent} --title "<name of the thing>" --summary "<what it is and why, 60+ chars>" --usage "<how to run it>" --file <path>`
- Routine work that made nothing lasting needs no entry.
{END}
"""


class Adapter:
    name = "base"                 # the --agent value, lower-case
    display = "Base"              # shown in `tracker agents`
    vendor = ""
    binaries: tuple[str, ...] = ()           # any of these on PATH means installed
    data_dirs: tuple[str, ...] = ()          # or any of these exists (relative to home)
    rules_file: str | None = None            # global instruction file, relative to home
    capabilities = {"capture": False, "rules": True, "gate": False, "usage": False}
    notes = ""

    # ---- detection
    def detected(self) -> bool:
        if any(shutil.which(b) for b in self.binaries):
            return True
        return any((HOME / d).exists() for d in self.data_dirs)

    # ---- rules (the universal path)
    def rules_path(self, project: str | None = None) -> Path | None:
        return HOME / self.rules_file if self.rules_file else None

    def connected(self) -> bool:
        p = self.rules_path()
        try:
            return bool(p and BEGIN in p.read_text(encoding="utf-8"))
        except OSError:
            return False

    def connect(self, project: str | None = None) -> str:
        p = self.rules_path(project)
        if p is None:
            return "this tool has no global instruction file; use: tracker agents connect generic --project <folder>"
        return write_block(p, rules_block(self.name))

    def disconnect(self, project: str | None = None) -> str:
        p = self.rules_path(project)
        return remove_block(p) if p else "nothing to remove"

    # ---- capture (optional)
    def scan(self, con, full: bool = False) -> dict:
        return {"skipped": "this adapter does not read session logs"}

    # ---- usage (optional)
    def usage(self, con) -> dict | None:
        return None


def write_block(path: Path, block: str) -> str:
    """Insert or replace our marked block; everything else in the file is left as it was."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        tail = rest.split(END, 1)[1].lstrip("\n")
        text = head + block + tail
        how = "updated"
    else:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
        how = "added"
    path.write_text(text, encoding="utf-8")
    return f"{how} the ledger block in {path}"


def remove_block(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return f"{path} not found"
    if BEGIN not in text:
        return f"no ledger block in {path}"
    head, rest = text.split(BEGIN, 1)
    tail = rest.split(END, 1)[1] if END in rest else ""
    path.write_text((head.rstrip("\n") + "\n" + tail.lstrip("\n")).lstrip("\n"), encoding="utf-8")
    return f"removed the ledger block from {path}"


def env_home(var: str, default: str) -> Path:
    """Tools that honour a home override (CODEX_HOME, GEMINI_HOME...)."""
    v = os.environ.get(var)
    return Path(v).expanduser() if v else HOME / default
