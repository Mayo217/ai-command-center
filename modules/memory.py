"""Command Center module 4 — Memory & knowledge.

GET  /api/memory/index      every ~/.claude/projects/*/memory/*.md with frontmatter, project,
                            size, mtime, wikilinks, and whether MEMORY.md indexes it
GET  /api/memory/file?path= one file raw (memory dirs and CLAUDE.md files only)
POST /api/memory/file       {path, content} -> write (same allow-list; keeps a .bak)
POST /api/memory/reindex    {project} -> append missing files to that project's MEMORY.md

The allow-list is the whole point: nothing outside a `memory/` directory or a CLAUDE.md can be
read or written through this module, and every write keeps the previous version under
logs/memory-bak/.
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
from modules.common import ApiError  # noqa: E402
from modules.config import parse_frontmatter  # noqa: E402

HOME = Path.home()
PROJECTS = HOME / ".claude" / "projects"
BAK_DIR = Path(__file__).resolve().parent.parent / "logs" / "memory-bak"
WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
MDLINK = re.compile(r"\[([^\]]+)\]\(([^)]+\.md)\)")


# ---------------------------------------------------------------- allow-list
def _resolve(path: str) -> Path:
    if not path:
        raise ApiError(400, "path is required")
    p = Path(os.path.expanduser(str(path)))
    try:
        p = p.resolve()
    except OSError:
        raise ApiError(400, "unresolvable path")
    s = str(p)
    if p.name == "CLAUDE.md":
        if p.is_relative_to(HOME / ".claude") or p.is_relative_to(HOME / "Desktop"):
            return p
        raise ApiError(403, "only CLAUDE.md files under ~/.claude or ~/Desktop are allowed")
    if "memory" not in p.parts or not p.is_relative_to(PROJECTS):
        raise ApiError(403, "only files inside ~/.claude/projects/*/memory/ are allowed")
    if p.suffix.lower() != ".md":
        raise ApiError(403, "only .md files")
    return p


def decode_project(dirname: str) -> str:
    """`-Users-you-Desktop-myproject` -> project name, using the tracker's own derivation."""
    if re.match(r"^[A-Za-z]--", dirname):  # Windows: C--Users-x-Desktop-y -> C:/Users/x/Desktop/y
        guess = dirname[0] + ":/" + dirname[3:].replace("-", "/")
    else:
        guess = "/" + dirname.lstrip("-").replace("-", "/")
    if Path(guess).is_dir():
        return db.derive_project(guess)
    parts = [p for p in dirname.split("-") if p]
    return parts[-1] if parts else dirname


# ---------------------------------------------------------------- index
def memory_dirs() -> list[Path]:
    if not PROJECTS.is_dir():
        return []
    return sorted([d / "memory" for d in PROJECTS.iterdir() if (d / "memory").is_dir()])


def _entry(md: Path, index_text: str) -> dict:
    try:
        text = md.read_text(errors="replace")
        st = md.stat()
    except OSError as e:
        return {"name": md.stem, "path": str(md), "error": str(e)}
    fm = parse_frontmatter(text)
    meta = {}
    mm = re.search(r"^\s*metadata:\s*$(.*?)^---", text, re.S | re.M)
    if mm:
        for line in mm.group(1).splitlines():
            m = re.match(r"^\s+([\w_]+):\s*(.+?)\s*$", line)
            if m:
                meta[m.group(1)] = m.group(2).strip().strip('"')
    body = text.split("\n---", 1)[-1] if text.startswith("---") else text
    return {
        "name": fm.get("name") or md.stem,
        "file": md.name,
        "path": str(md),
        "project_dir": md.parent.parent.name,
        "project": decode_project(md.parent.parent.name),
        "description": (fm.get("description") or "")[:500],
        "type": meta.get("type") or fm.get("type") or "note",
        "node_type": meta.get("node_type"),
        "origin_session": meta.get("originSessionId"),
        "bytes": st.st_size,
        "words": len(body.split()),
        "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
        "mtime_epoch": st.st_mtime,
        "wikilinks": sorted(set(WIKILINK.findall(text))),
        "md_links": sorted({t for _l, t in MDLINK.findall(text)}),
        "is_index": md.name == "MEMORY.md",
        "indexed": md.name == "MEMORY.md" or md.name in index_text,
    }


def get_index(query=None, body=None, match=None):
    query = query or {}
    want_project = query.get("project")
    q = (query.get("q") or "").lower()
    files, orphans = [], []
    for d in memory_dirs():
        index_path = d / "MEMORY.md"
        index_text = index_path.read_text(errors="replace") if index_path.exists() else ""
        for md in sorted(d.glob("*.md")):
            e = _entry(md, index_text)
            if want_project and e["project"] != want_project and e["project_dir"] != want_project:
                continue
            if q and q not in json.dumps(e).lower():
                continue
            files.append(e)
            if not e["indexed"]:
                orphans.append(e["path"])
    files.sort(key=lambda e: -e["mtime_epoch"])
    projects: dict[str, int] = {}
    for e in files:
        projects[e["project"]] = projects.get(e["project"], 0) + 1
    return 200, {"ok": True, "count": len(files), "orphan_count": len(orphans), "orphans": orphans,
                 "projects": projects, "files": files,
                 "claude_md": [{"path": str(p), "bytes": p.stat().st_size, "scope": "global"}
                               for p in [HOME / ".claude/CLAUDE.md"] if p.exists()]
                 + [{"path": str(p), "bytes": p.stat().st_size, "scope": "project"}
                    for p in sorted((HOME / "Desktop").glob("*/CLAUDE.md"))]}


def get_file(query=None, body=None, match=None):
    query = query or {}
    p = _resolve(query.get("path", ""))
    if not p.exists():
        raise ApiError(404, f"no such file: {p}")
    text = p.read_text(errors="replace")
    return 200, {"ok": True, "path": str(p), "bytes": len(text.encode()), "content": text,
                 "frontmatter": parse_frontmatter(text),
                 "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(p.stat().st_mtime))}


def post_file(query=None, body=None, match=None):
    body = body or {}
    p = _resolve(body.get("path", ""))
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ApiError(400, "content must be a non-empty string")
    if len(content) > 400_000:
        raise ApiError(400, "content too large (>400 KB)")
    existed = p.exists()
    bak = None
    if existed:
        BAK_DIR.mkdir(parents=True, exist_ok=True)
        bak = BAK_DIR / f"{p.parent.parent.name}--{p.name}--{time.strftime('%Y%m%d-%H%M%S')}.bak"
        bak.write_text(p.read_text(errors="replace"))
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    con = db.connect()
    db.add_event(con, "cc", "action", {"module": "memory", "action": "write", "path": str(p),
                                       "existed": existed, "bytes": len(content)})
    return 200, {"ok": True, "path": str(p), "created": not existed, "backup": str(bak) if bak else None}


def post_reindex(query=None, body=None, match=None):
    """Append an index line to MEMORY.md for every memory file it does not mention. Append-only."""
    body = body or {}
    project = str(body.get("project") or "").strip()
    if not project:
        raise ApiError(400, "project (the project dir name or derived name) is required")
    target = None
    for d in memory_dirs():
        if d.parent.name == project or decode_project(d.parent.name) == project:
            target = d
            break
    if target is None:
        raise ApiError(404, f"no memory directory for project {project}")
    index_path = target / "MEMORY.md"
    text = index_path.read_text(errors="replace") if index_path.exists() else "# Memory Index\n\n"
    added = []
    for md in sorted(target.glob("*.md")):
        if md.name == "MEMORY.md" or md.name in text:
            continue
        e = _entry(md, text)
        line = f"- [{e['name']}]({md.name}) — {e['description'] or 'no description'}\n"
        text = text.rstrip("\n") + "\n" + line
        added.append(md.name)
    if added:
        BAK_DIR.mkdir(parents=True, exist_ok=True)
        if index_path.exists():
            (BAK_DIR / f"{target.parent.name}--MEMORY.md--{time.strftime('%Y%m%d-%H%M%S')}.bak").write_text(
                index_path.read_text(errors="replace"))
        index_path.write_text(text)
        con = db.connect()
        db.add_event(con, "cc", "action", {"module": "memory", "action": "reindex",
                                           "project": project, "added": added})
    return 200, {"ok": True, "project": project, "index": str(index_path), "added": added,
                 "added_count": len(added)}


ROUTES = [
    ("GET", r"/api/memory/index/?$", get_index),
    ("GET", r"/api/memory/file/?$", get_file),
    ("POST", r"/api/memory/file/?$", post_file),
    ("POST", r"/api/memory/reindex/?$", post_reindex),
]

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Command Center memory module — memory files and CLAUDE.md.")
    ap.add_argument("--project"); ap.add_argument("--file")
    a = ap.parse_args()
    res = get_file({"path": a.file})[1] if a.file else get_index({"project": a.project} if a.project else {})[1]
    print(json.dumps(res, indent=2, default=str))
