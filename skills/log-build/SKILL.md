---
name: log-build
description: The build-ledger protocol for this computer. Use whenever a session has created or changed a durable file (script, page, hook, doc, config, scheduled task, data pipeline, analysis output), whenever the Stop gate fires with "TRACKER GATE", and whenever the owner asks what was built, where something lives or whether a thing already exists. Covers `tracker log`, `tracker waive`, `tracker recent`, `tracker search` and what counts as an item.
---

# log-build: record what you built, so it is not rebuilt

This computer keeps a SQLite ledger of what Claude Code sessions build, in the `ai-command-center`
folder, wherever it was installed (CLI `tracker`, app `tracker open`, database `tracker.db`). Hooks register
each session and capture each file write. A Stop gate holds the handback while durable changes are
unlogged. This skill is the protocol.

## 0. Before you build
The SessionStart hook injects "Recently built in <project>". Read it. If what you are about to build
matches an entry there, extend that thing and log the change as `--kind enhancement`. To look further:

```
tracker recent --cwd .          # what exists in this folder/project
tracker search "invoice parser" # full text over titles, summaries, usage, paths
tracker show 42                 # one item, with file-existence checks
```

## 1. What is an item
One item per distinct thing you made or changed in a lasting way: scripts, pages, dashboards, hooks,
scheduled tasks, docs and handoffs, config changes, data pipelines, analyses that wrote a result file.

Not items: memory files, scratchpad files, throwaway one-liners, reading and searching, a message you
sent, tracker.db itself, anything in a temp folder (the hooks ignore those).

## 2. Log as you go
Log right after you finish a thing. The Stop gate is the backstop, not the plan.

```
tracker log --title "Invoice parser" --summary "Reads supplier PDF invoices from the inbox folder and writes one CSV row per line item, because month-end entry was being typed by hand." --usage "python invoices/parse.py inbox/  (writes out/invoices.csv)" --kind build --file invoices/parse.py:entry --file out/invoices.csv:output --tag finance
```

Field rules:
* `--title` 8 to 140 chars: the name of the thing, not the task. "Invoice parser" is good; "Fixed the thing I was asked about" is not.
* `--summary` at least 60 chars: what it is and why it exists, in plain words, without repeating the title.
* `--usage`: the command, URL or place to look, as you would type it.
* `--file PATH[:role]`, roles `entry|created|modified|doc|output`, entry file first. Relative and `~` paths are made absolute. Missing files are accepted but flagged.
* `--link URL[|label]` for dashboards, docs and other links.
* `--kind build|enhancement|fix|research|doc|config|automation|analysis|other`. Use `enhancement` when the thing already existed and say what changed; add `--supersedes <id>` when it replaces an item.
* Leave out `--file` and the CLI attaches every unlogged file this session touched. That is the quick path when the session built one thing.

A weak entry: title `Updates`, summary "made some changes", no usage, no files. A future session learns
nothing from it and rebuilds the thing. One item covering a whole day across unrelated projects is
also weak: split it, one item per thing.

### Several things at once
Call `tracker log` several times, or pass a JSON file:

```
tracker log --json-in items.json
```
with `{"items":[{"title":"...","summary":"...","usage":"...","kind":"build","files":[{"path":"...","role":"entry"}]}]}`.
Each item is checked like the flag form.

## 3. Waiving
Only when the session made nothing durable (research, conversation, running existing tools,
routine data churn):

```
tracker waive --reason "Answered questions about the schema and re-ran the existing report; no files written."
```

* The reason needs at least 20 real characters.
* Waive is refused when a pending file looks durable: `.py .sh .bat .ps1 .html .js .ts .tsx .css .md .sql .yaml .yml .toml`, or `.json` outside a `state` folder. Log those instead.

## 3b. Phantom pending files
The Bash parser is a regex, so a command can leave a path in the pending list that was never a real
file. Clear those without inventing an item:

```
tracker status
tracker dismiss --missing --reason "regex debris from a heredoc; no such files exist"
```
`dismiss` refuses files that exist and look durable, and records a `dismiss` event.

## 4. When the Stop gate fires
You will see `TRACKER GATE: you changed N files that are not logged: ...`. Then either run
`tracker log ...` for each thing you built, or `tracker waive --reason "..."` if it was routine churn,
and stop. The gate blocks at most 3 times a session and never inside its own loop. An unlogged
handback is recorded in the audit view and auto-captured as a low-quality `inferred` item.

Do not edit `~/.claude/settings.json`, the `hooks` scripts, `config.json` or the database to get past
the gate, and do not set `disableAllHooks`. The owner can switch the gate off in `config.json`
(`"gate": {"enabled": false}`); that choice is theirs.

## 5. Browsing
```
tracker status                  # this session: pending files, gate verdict
tracker list --since 2026-10-01
tracker edit 42 --summary "..." --status superseded
tracker export --md             # markdown digest for a handoff
tracker open                    # the app at http://127.0.0.1:8790
```
Every read command accepts `--json`.
