# Claude Code hook contract — verified 2026-09-12, extended 2026-09-13

Sources, in order of authority:

1. **The installed binary's own embedded schemas** (2026-09-13). Claude Code ships its hook
   payloads as Zod schemas inside the bundle, which is ground truth rather than documentation:
   `strings -n 20 ~/.local/share/claude/versions/2.1.269 | grep -oE 'hook_event_name:R\("<Event>"\).{0,400}'`
   prints the field list for any event, and
   `grep -oE 'hookEventName:R\("<Event>"\).{0,400}'` prints what that event accepts back on stdout.
   Everything in §1a below was read that way on **2026-09-13** against **2.1.269** (the version
   named in the brief; `claude --version` on this Mac now reports 2.1.270, and the schemas for
   these events are byte-identical in the 2.1.270 bundle — checked).
2. <https://code.claude.com/docs/en/hooks> (the old `docs.claude.com/en/docs/claude-code/hooks`
   URL 301-redirects there) — the source for §1, §2, §3, §4 and §5.

Where the two disagree, the binary wins and the disagreement is written down.

## 1. stdin — fields we rely on

Common to every event:

```json
{ "session_id": "…", "transcript_path": "/Users/…/.claude/projects/<proj>/<sid>.jsonl",
  "cwd": "/Users/you/Desktop/x", "hook_event_name": "Stop",
  "permission_mode": "default|plan|acceptEdits|auto|dontAsk|bypassPermissions",
  "scratchpad_dir": "…(optional)", "prompt_id": "…(optional, 2.1.196+)",
  "effort": {"level": "low|medium|high|xhigh|max"} }
```

Subagent invocations additionally carry `agent_id` / `agent_type`. Verbatim from the 2.1.269
schema for `agent_id`: *"Subagent identifier. Present only when the hook fires from within a
subagent (e.g., a tool called by an AgentTool worker). Absent for the main thread, even in
`--agent` sessions. **Use this field (not agent_type) to distinguish subagent calls from
main-thread calls.**"* — and for `agent_type`: *"Agent type name (e.g., "general-purpose",
"code-reviewer"). Present when the hook fires from within a subagent (alongside agent_id), or on
the main thread of a session started with `--agent` (without agent_id)."*

That is the whole reason the gate could not see subagents: a subagent's `PostToolUse` carries the
**parent's** `session_id`, so its file writes landed on the parent and the parent was blocked for
work it had never done. `hooks/post_tool_use.py` now branches on `agent_id`.

| Event | Extra fields | Matcher values |
|---|---|---|
| `SessionStart` | `model` (optional, not always present) | `startup`, `resume`, `clear`, `compact`, `fork` |
| `UserPromptSubmit` | `prompt` (the raw user text) | — |
| `PreToolUse` | `tool_name`, `tool_input`, `tool_use_id` | tool name, e.g. `Bash`, `Edit`, `mcp__memory__.*` |
| `PostToolUse` | `tool_name`, `tool_input`, `tool_response`, `tool_use_id` | same as PreToolUse |
| `Stop` | `stop_hook_active` (boolean, 2.1.268+), `last_assistant_message` | — |
| `SessionEnd` | (docs list only the common fields; `reason` is read defensively) | `clear`, `resume`, `logout`, `prompt_input_exit`, `other` |

Notes we acted on:
* `tool_response` is typed "string" in the docs but is an object for several tools — the tracker
  stringifies whatever it gets before sniffing it for `created` / `updated`.
* `SessionEnd` has no documented `reason` field in 2.1.x even though the matcher is the end reason;
  `hooks/session_end.py` reads `reason` → `source` → `matcher` → `"other"`.
* `SessionStart` may omit `model`, so the model is recovered from the transcript's `cost-state`
  record at SessionEnd instead.


## 1a. The events wired on 2026-09-13 — read out of the 2.1.269 bundle

The bundle carries the full event list (one array, verbatim): `PreToolUse`, `PostToolUse`,
`PostToolUseFailure`, `PostToolBatch`, `Notification`, `UserPromptSubmit`, `UserPromptExpansion`,
`SessionStart`, `SessionEnd`, `Stop`, `StopFailure`, `SubagentStart`, `SubagentStop`,
`PreCompact`, `PostCompact`, `PreModelSwitch`, `PostModelSwitch`, `PermissionRequest`,
`PermissionDenied`, `Setup`, `TeammateIdle`, `TaskCreated`, `TaskCompleted`, `Elicitation`,
`ElicitationResult`, `ConfigChange`, `CwdChanged`, `FileChanged`, `MessageDisplay`.
So all four events named in the brief exist in 2.1.269 under those exact names.

Every payload below is the common block from §1 **plus** these fields.

| Event | Extra stdin fields (exact names) | Matcher matches | stdout it accepts | Wired as |
|---|---|---|---|---|
| `SubagentStop` | `stop_hook_active` (bool), `agent_id`, `agent_transcript_path`, `agent_type`, `last_assistant_message?`, `background_tasks`, `session_crons` | `agent_type` | `hookSpecificOutput: {hookEventName:"SubagentStop", additionalContext?}` | `hooks/subagent_stop.py` |
| `PreCompact` | `trigger: "manual"\|"auto"`, `custom_instructions: string\|null` | `trigger` | *(no entry in the hookSpecificOutput union — nothing to return)* | `hooks/pre_compact.py` |
| `Notification` | `message`, `title?`, `notification_type` | `notification_type` | `hookSpecificOutput: {hookEventName:"Notification", additionalContext?}` | `hooks/notification.py` |
| `PermissionRequest` | `tool_name`, `tool_input`, `permission_suggestions?[]` | `tool_name` | `hookSpecificOutput: {hookEventName:"PermissionRequest", decision: {behavior:"allow", updatedInput?, updatedPermissions?} \| {behavior:"deny", message?, interrupt?}}` | `hooks/permission_request.py` |

Confirmed and acted on:

* **`SubagentStop`'s `last_assistant_message` is documented as existing precisely to save you
  parsing the transcript** — verbatim: *"Text content of the last assistant message before
  stopping. Avoids the need to read and parse the transcript."* `subagent_stop.py` prefers it and
  falls back to `hook_common.transcript_meta(agent_transcript_path)`.
* **`SubagentStop` is a blocking-class event**, exactly like `Stop`: the bundle's
  `v = n!=="Stop" && n!=="SubagentStop" && n!=="StopFailure"` guard routes exit 2 on these three
  through the stop-decision path rather than the generic "blocking error" path. `subagent_stop.py`
  therefore **never** exits 2 — blocking a subagent on a tracker bug would leave an agent unable
  to finish, and the gate belongs at the parent's `Stop`.
* **`SubagentStop`'s `additionalContext` is delivered to the subagent, not the parent** —
  verbatim: *"additionalContext is non-error feedback delivered to the subagent; the subagent
  continues so it can act on it."* That is the one channel where a subagent can be told to log
  its own files with explicit `--file` paths, and `subagent_stop.py` uses it.
* **`PermissionRequest` can allow or deny a tool outright.** The tracker observes; it never
  decides. `permission_request.py` writes nothing to stdout at all — empty stdout with exit 0
  carries no decision and the normal permission flow proceeds.
* **`PreCompact` has no `hookSpecificOutput` member** in the 2.1.269 union, so there is no way to
  inject context at compaction. `pre_compact.py` records and stays silent.
* **Agent-level hooks get rewritten**: the bundle logs *"Converting Stop hook to SubagentStop for
  ${s} (subagents trigger SubagentStop)"* — a `Stop` hook declared inside an agent definition is
  registered as `SubagentStop`. Our hooks are user-level (`~/.claude/settings.json`), so they are
  not rewritten; a `Stop` entry there fires only for the main thread.
* **`PostToolUse` now also carries `duration_ms`** — *"Tool execution time in milliseconds.
  Excludes permission-prompt and hook time."* Not yet captured; see HANDOFF.md.

Not wired, and why:

* `PostToolUseFailure` (`tool_name`, `tool_input`, `tool_use_id`, `reason`) and `SubagentStart`
  (`agent_id`, `agent_type`) both exist and are both cheap. They are the next two to add; a
  failing tool is the signal that a session is stuck, and `SubagentStart` would give
  `session_agents` a real start time instead of first-write time.
* `PostCompact` (`trigger`, `compact_summary` — *"The conversation summary produced by
  compaction"*) is the obvious partner to `PreCompact` and would let the ledger store the summary
  that replaced the memory. Left out only to keep this change to the four events in the brief.

## 2. Exit codes

* **0** — success. stdout is parsed as JSON when it starts with `{` and ends with `}`, otherwise
  treated as text. Plain-text stdout is shown to Claude **only** for `UserPromptSubmit`,
  `UserPromptExpansion`, `SessionStart` and `PostModelSwitch`; for every other event stdout goes to
  the debug log.
* **2** — blocking error. Verbatim: *"Exit 2 means a blocking error. On events that can block, exit 2
  blocks whether or not you print JSON: even a JSON permissionDecision of "allow" can't override it."*
  stderr is shown in the transcript as the block reason (JSON's reason wins if valid JSON was printed).
* anything else — non-blocking error; stderr surfaces in the transcript, the action proceeds.

## 3. How a Stop hook blocks

The 2.1.x page documents two channels:

1. `hookSpecificOutput: { hookEventName: "Stop", shouldContinue: true, reason: "…" }` on stdout, exit 0.
2. exit 2 with the reason on stderr — *"Prevents Claude from stopping, continues the conversation."*

The long-standing top-level form `{"decision":"block","reason":"…"}` is
still accepted by the Stop decision model but is no longer the shape shown on the page.

**Decision:** `hooks/stop_gate.py` writes the
reason on *all* channels — the JSON object below on stdout **and** the plain reason on stderr — and
exits **2**. Exit 2 is the only unambiguously documented way to block a Stop in 2.1.x, and the extra
JSON keeps compatibility if a future build prefers the decision object. A block therefore looks like:

```
stdout: {"decision":"block","reason":"TRACKER GATE: …",
         "hookSpecificOutput":{"hookEventName":"Stop","shouldContinue":true,"reason":"TRACKER GATE: …"}}
stderr: TRACKER GATE: …
exit:   2
```

Every other hook path exits 0, always, even on an internal exception (`hook_common.run` swallows the
traceback into `logs/hooks.log`) — a tracker bug must never wedge a session.

## 4. How SessionStart injects context

```json
{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "…"}}
```

Verbatim from the docs: *"additionalContext: optional string, added to Claude's context for this
turn."* We cap ours at 1,800 characters (`hook_common.MAX_CONTEXT_CHARS`).

## 5. Matcher syntax

* Plain names, `|`-lists and `,`-lists (2.1.191+) are exact matches: `"Bash"`, `"Edit|Write"`, `"Edit, Write"`.
* Any other character makes it a regular expression, tested with JavaScript `RegExp.prototype.test`,
  i.e. it matches **anywhere** in the value — `Edit.*` matches `NotebookEdit` too; anchor with `^…$`
  for a whole-string match.
* `SessionStart` / `SessionEnd` matchers match the start/end reason, not a tool name. We register
  those two without a matcher so they fire for every reason.

## 6. settings.json shape

```json
{"hooks": {"<EventName>": [{"matcher": "Bash", "hooks": [{"type": "command",
   "command": "/abs/python3 /abs/script.py", "timeout": 10}]}]}}
```

`timeout` is in seconds. `~/.claude/settings.json` is the user-level file (all projects); project
files are `.claude/settings.json` / `.claude/settings.local.json`. A global kill switch
`"disableAllHooks": true` exists — the tracker never sets it, and sessions must not either.

**Editing that file.** Always load it with `json.load`, mutate, and `json.dump` it back — never
rewrite it from a template and never hand-edit it in a way that could drop a key. Back up the
exact bytes first (`settings.json.bak-tracker-hooks-<stamp>`). `tracker doctor` records a SHA-256
of the whole `hooks` block in `meta.hooks_block_sha256` and reports when it changes, which is how
"Never edit the hooks to escape the gate" stops being a rule nobody checks.

## 7. What the tracker registers, as of 2026-09-13

| Event | Script | Matcher | Timeout | Blocks? |
|---|---|---|---|---|
| `SessionStart` | `session_start.py` | *(none)* | 10 s | no — emits `additionalContext` |
| `UserPromptSubmit` | `user_prompt_submit.py` | *(none)* | 10 s | no |
| `PostToolUse` | `post_tool_use.py` | `Write\|Edit\|MultiEdit\|NotebookEdit` and `Bash` | 10 s | no |
| `PermissionRequest` | `permission_request.py` | *(none)* | 5 s | **never** — observe-only, no stdout |
| `Notification` | `notification.py` | *(none)* | 5 s | no |
| `PreCompact` | `pre_compact.py` | *(none)* | 10 s | no |
| `SubagentStop` | `subagent_stop.py` | *(none)* | 10 s | **never** — records, and speaks to the subagent |
| `Stop` | `stop_gate.py` | *(none)* | 10 s | **yes, deliberately** — the gate (§3) |
| `SessionEnd` | `session_end.py` | *(none)* | 10 s | no |

Nine events, one deliberate blocker.
