"""Any AI that reads AGENTS.md (the shared instruction-file convention used by Codex, Cursor,
GitHub Copilot and others). Rules only, per project folder."""
from pathlib import Path

from agents.base import Adapter, write_block, remove_block, rules_block, BEGIN


class Generic(Adapter):
    name = "generic"
    display = "Any AGENTS.md tool"
    capabilities = {"capture": False, "rules": True, "gate": False, "usage": False}
    notes = "writes the ledger block into <project>/AGENTS.md; pass --agent <tool> when it logs"

    def detected(self) -> bool:
        return True

    def rules_path(self, project=None):
        return Path(project or ".").resolve() / "AGENTS.md"

    def connected(self) -> bool:
        p = self.rules_path()
        return p.exists() and BEGIN in p.read_text(encoding="utf-8")

    def connect(self, project=None) -> str:
        return write_block(self.rules_path(project), rules_block("<tool>").replace(
            "--agent <tool>", "--agent <your tool name, e.g. cursor>"))

    def disconnect(self, project=None) -> str:
        return remove_block(self.rules_path(project))


ADAPTER = Generic()
