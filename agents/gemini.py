"""Google Gemini CLI: rules through ~/.gemini/GEMINI.md, its global instruction file."""
from agents.base import Adapter


class Gemini(Adapter):
    name = "gemini"
    display = "Gemini CLI"
    vendor = "Google"
    binaries = ("gemini",)
    data_dirs = (".gemini",)
    rules_file = ".gemini/GEMINI.md"
    capabilities = {"capture": False, "rules": True, "gate": False, "usage": False}
    notes = "rules only for now; session-log capture is not built yet"


ADAPTER = Gemini()
