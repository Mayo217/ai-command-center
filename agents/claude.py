"""Anthropic Claude Code: the full set, through Claude Code hooks (installed by install.py)."""
from agents.base import Adapter, HOME


class Claude(Adapter):
    name = "claude"
    display = "Claude Code"
    vendor = "Anthropic"
    binaries = ("claude",)
    data_dirs = (".claude",)
    capabilities = {"capture": True, "rules": True, "gate": True, "usage": True}
    notes = "hooks capture every session and write live; the Stop gate and status-line meter are Claude-only"

    def connected(self) -> bool:
        try:
            return "ai-command-center-hook" in (HOME / ".claude" / "settings.json").read_text(encoding="utf-8")
        except OSError:
            return False

    def connect(self, project=None) -> str:
        return "Claude Code is connected by `python install.py` (hooks, status line, skill)."

    def disconnect(self, project=None) -> str:
        return "Run `python install.py --uninstall` to remove the Claude Code hooks."


ADAPTER = Claude()
