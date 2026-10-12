# AI Command Center

**A build ledger for AI coding tools.** It remembers what your AI sessions build (Claude Code, OpenAI
Codex and others), so the next session extends a thing instead of rebuilding it. It also tracks plan
limits and, if you want, brings a small team's work into one place.

- **Ledger.** Each thing an AI builds is logged with a name, what it is, why it exists and how to run
  it. `tracker search "invoice"` finds it months later.
- **Every AI tool, one ledger.** Each tool gets an adapter. Any AI that can run a shell command can
  keep the ledger through its instruction file. Adapters can also read the tool's own session logs
  and plan limits.
- **Stop gate (Claude Code).** When a session changed files and logged nothing, Claude is asked to log
  them before it hands back. It asks at most 3 times per session, and it switches off in one line.
- **Start-of-session memory.** Each new session is shown what was already built in that folder.
- **Usage.** Claude's 5-hour and weekly limits from its status line, and Codex's plan windows from
  its logs.
- **Web app.** A local page at `http://127.0.0.1:8790` for the ledger, sessions, usage, memory files,
  settings and projects.
- **Team (optional).** Each person's computer drops a daily file into a shared folder, and the
  owner's computer collects them into one ledger with a profile per person.

It is plain Python (standard library only) with one SQLite file. Nothing leaves your computer
unless you turn on team sync or the optional usage refresh.

> Not affiliated with or endorsed by Anthropic, OpenAI or Google. Product names belong to their owners.

## Supported AI tools

| Tool | Logs what it builds | Reads its sessions | Plan limits | Stop gate |
|---|---|---|---|---|
| Claude Code (Anthropic) | yes | yes, live through hooks | yes | yes |
| Codex CLI (OpenAI, GPT models) | yes | yes, from `~/.codex/sessions` | yes | not yet |
| Gemini CLI (Google) | yes | not yet | not yet | not yet |
| Any tool that reads `AGENTS.md` (Cursor, GitHub Copilot, ...) | yes | not yet | not yet | not yet |

```
tracker agents                     # which tools are installed and connected, and what each supports
tracker agents connect codex       # adds a short ledger block to ~/.codex/AGENTS.md (your text is kept)
tracker agents connect gemini      # ~/.gemini/GEMINI.md
tracker agents connect generic --project .   # <project>/AGENTS.md, for any other tool
tracker agents scan                # import sessions and plan limits from tools' own logs (also daily)
tracker agents disconnect codex    # removes only the block it added
```

### Adding another tool
Each tool is one file in `agents/`. Run `tracker agents new <name>`. That creates a ready-to-fill
adapter: set its command name, its instruction file and what it supports. To read its session
logs, add a `scan()` method; `agents/codex.py` is a complete example. A new file in `agents/` is
picked up automatically. You can also ask your AI to write the adapter: point it at `agents/base.py`
and `agents/codex.py`.

## Install

You need Claude Code, plus Python 3.12 or newer (`python --version`). On Windows, if you don't have
Python, get it from python.org and tick **"Add python.exe to PATH"**.

```
git clone https://github.com/Mayo217/ai-command-center.git
cd ai-command-center
python install.py              # add --autostart to start the app at login
tracker agents connect codex   # optional, for each other AI tool you use
```

Then restart Claude Code and check it:

```
tracker doctor
python tests/selftest.py
tracker open                   # or START.bat on Windows
```

What the installer changes, and how to undo it:
- `~/.claude/settings.json`: it **adds** 10 hooks and a status line next to yours, after making a
  timestamped backup. It never removes your entries, and if you already have a status line it
  leaves yours alone.
- It adds a `tracker` command (`~/.local/bin/tracker`, or `tracker.cmd` on Windows).
- It adds the `log-build` skill to `~/.claude/skills/`, plus a short "Build ledger" paragraph to
  `~/.claude/CLAUDE.md`.
- `python install.py --uninstall` takes the hooks and status line back out. Your ledger stays.

## Everyday use

You mostly don't run anything: Claude does the logging. Useful commands:

```
tracker recent --cwd .        # what was built in this folder before
tracker search "parser"       # full-text search over everything logged
tracker show 42               # one item, with file checks
tracker status                # this session: pending files, gate verdict
tracker export --md           # a markdown digest for a handoff
tracker usage                 # token and cost summary
```

## Settings

Your settings live in `config.json`, which the installer creates from `config.default.json`.

| Setting | Default | What it does |
|---|---|---|
| `gate.enabled` | `true` | `false` keeps recording but never blocks a session |
| `gate.max_blocks_per_session` | `3` | how often the gate may ask before it lets go |
| `timezone` | `"local"` | or an offset such as `"+05:30"` |
| `port` | `8790` | the web app's port |
| `usage.oauth_refresh` | `false` | `true` lets the app ask Anthropic for fresh usage numbers using your Claude Code login (macOS keychain, or `~/.claude/.credentials.json`). It is sent only to Anthropic's API, and never stored or shown. This uses an undocumented endpoint that may change. |

## Team: one ledger for a small team

Each person installs the Command Center on their own computer. Their computer writes one small file
a day into a shared folder (OneDrive, Google Drive, Dropbox or a network share), and the owner's
computer reads them in.

**What is shared:** the person's Claude login (email, name, organisation, plan), their latest usage
meter, the items they logged (title, summary, usage, file paths, links), per-session counts and
cost, and their computer name. Prompts, chats and file contents are never shared. **Tell your team
before you switch this on, and check your local employee-monitoring and privacy rules.**

```
# each person (once): send daily at 18:30 (--weekly for Fridays, --time HH:MM to change)
python team.py schedule send --name ravi --to "~/OneDrive/ClaudeTeam"

# the owner (once): collect daily at 19:30
python team.py schedule collect --from "~/OneDrive/ClaudeTeam" --time 19:30
```

Profiles live in `team.json` on the owner's computer.
- **Matching:** a file is matched to a person by the Claude login it was sent from. Only files that
  carry no login fall back to the sender or computer name, so two people sharing one computer stay
  separate.
- **New people:** the first file from someone new creates their profile automatically. For a strict
  roster, set `"auto_add": false` in `team.json`. Files from unknown people are then held until you
  add them.

```
python team.py add ravi --name "Ravi Kumar" --role Accounts --email ravi@company.com
python team.py people           # everyone: last file, 7-day work, usage meter
python team.py profile ravi     # one person: logins, computers, plan, 7/30-day work, recent items
```

Their work shows up in your ledger and web app with the person's id in front of the project
(`ravi/invoices`), tagged `team`.

## Platform support

| | macOS | Windows | Linux |
|---|---|---|---|
| Ledger, gate, hooks, search, web app, usage meter, team | yes | yes | yes |
| Start at login (`--autostart`) | launchd | Task Scheduler | do it yourself |
| Jobs page, live-session detection, memory pressure | yes | no (shows empty) | no (shows empty) |

CI runs the test suites on all three, on Python 3.12 and 3.13.

## How it works

- `hooks/`: the Claude Code hooks. They register sessions, capture writes, inject recent context and
  run the Stop gate. They fail open: a hook error never blocks Claude. See `hooks/HOOK_CONTRACT.md`.
- `tracker.py`: the CLI. `db.py` and `schema.sql` are the ledger. `backfill.py` and
  `jobs/reindex.py` mine past transcripts.
- `server.py`, `modules/` and `ui/index.html`: the local web app. It listens on 127.0.0.1 only and
  accepts writes only from its own page (see [SECURITY.md](SECURITY.md)).
- `agents/`: one adapter per AI tool (`base.py` is the contract).
- `team.py`: team send, collect and profiles. `oscompat.py`: the macOS/Windows/Linux differences.

## Licence

MIT. See [LICENSE](LICENSE).
