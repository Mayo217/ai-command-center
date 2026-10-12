-- AI Command Center schema v1 (2026-09-12). Applied idempotently by db.py.migrate().
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS sessions (
  session_id     TEXT PRIMARY KEY,
  agent          TEXT NOT NULL DEFAULT 'claude',    -- which AI: claude | codex | gemini | cursor | ...
  source         TEXT NOT NULL DEFAULT 'live',      -- live | backfill | manual
  cwd            TEXT,
  project        TEXT,
  git_branch     TEXT,
  cc_version     TEXT,
  title          TEXT,                              -- ai-title or derived
  first_prompt   TEXT,
  model          TEXT,
  started_at     TEXT,                              -- ISO8601 UTC
  ended_at       TEXT,
  end_reason     TEXT,
  last_activity  TEXT,
  user_turns     INTEGER NOT NULL DEFAULT 0,
  assistant_turns INTEGER NOT NULL DEFAULT 0,
  tool_calls     INTEGER NOT NULL DEFAULT 0,
  writes         INTEGER NOT NULL DEFAULT 0,        -- Write tool uses
  edits          INTEGER NOT NULL DEFAULT 0,        -- Edit/MultiEdit/NotebookEdit
  bash_calls     INTEGER NOT NULL DEFAULT 0,
  cost_usd       REAL,
  lines_added    INTEGER,
  lines_removed  INTEGER,
  model_usage    TEXT,                              -- JSON from cost-state
  transcript_path TEXT,
  substantive    INTEGER NOT NULL DEFAULT 0,
  logged         INTEGER NOT NULL DEFAULT 0,        -- >=1 confirmed item
  waived_at      TEXT,
  waived_reason  TEXT,
  gate_blocks    INTEGER NOT NULL DEFAULT 0,
  gate_bypassed  INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);

CREATE TABLE IF NOT EXISTS items (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  agent          TEXT NOT NULL DEFAULT 'claude',    -- which AI built it
  session_id     TEXT REFERENCES sessions(session_id),
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  built_at       TEXT,                              -- when the thing was actually built (backfill: session end)
  kind           TEXT NOT NULL DEFAULT 'build',     -- build|enhancement|fix|research|doc|config|automation|analysis|other
  title          TEXT NOT NULL,
  summary        TEXT NOT NULL,                     -- what + why
  usage          TEXT,                              -- how to run / where
  project        TEXT,
  status         TEXT NOT NULL DEFAULT 'active',    -- active|wip|superseded|abandoned|deleted
  confidence     TEXT NOT NULL DEFAULT 'confirmed', -- confirmed|inferred
  source         TEXT NOT NULL DEFAULT 'cli',       -- cli|backfill|auto|ui
  supersedes_id  INTEGER REFERENCES items(id),
  tags           TEXT NOT NULL DEFAULT '[]',        -- JSON array of strings
  model          TEXT,
  cost_usd       REAL,
  verified_at    TEXT,
  extra          TEXT                               -- JSON blob for anything else (recap, first_prompt, etc.)
);
CREATE INDEX IF NOT EXISTS idx_items_session ON items(session_id);
CREATE INDEX IF NOT EXISTS idx_items_project ON items(project);
CREATE INDEX IF NOT EXISTS idx_items_built ON items(built_at);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);

CREATE TABLE IF NOT EXISTS item_files (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id   INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  path      TEXT NOT NULL,
  role      TEXT NOT NULL DEFAULT 'modified',       -- created|modified|entry|doc|output
  exists_at_log INTEGER,
  size_bytes INTEGER,
  UNIQUE(item_id, path)
);
CREATE INDEX IF NOT EXISTS idx_item_files_path ON item_files(path);

CREATE TABLE IF NOT EXISTS item_links (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id   INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  url       TEXT NOT NULL,
  label     TEXT,
  UNIQUE(item_id, url)
);

CREATE TABLE IF NOT EXISTS session_files (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id     TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  path           TEXT NOT NULL,
  op             TEXT NOT NULL DEFAULT 'modify',    -- create|modify|bash
  tool           TEXT,
  first_ts       TEXT,
  last_ts        TEXT,
  count          INTEGER NOT NULL DEFAULT 1,
  ignored        INTEGER NOT NULL DEFAULT 0,
  logged_item_id INTEGER REFERENCES items(id),
  UNIQUE(session_id, path)
);
CREATE INDEX IF NOT EXISTS idx_session_files_session ON session_files(session_id);

CREATE TABLE IF NOT EXISTS session_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  ts         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  event      TEXT NOT NULL,                         -- session_start|prompt|tool|gate_block|gate_bypass|waive|log|session_end|backfill
  payload    TEXT                                   -- JSON
);
CREATE INDEX IF NOT EXISTS idx_session_events_session ON session_events(session_id);

-- Full-text search over items (external content table pattern)
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  title, summary, usage, tags, paths, project,
  content='', contentless_delete=1, tokenize='porter unicode61'
);
-- items_fts is maintained by db.py (reindex_item(item_id)) after any insert/update of items/item_files,
-- because paths come from a second table. db.py.rebuild_fts() rebuilds everything.

-- ---------------------------------------------------------------------------
-- Append-only; all IF NOT EXISTS.
-- ---------------------------------------------------------------------------

-- One row per assistant API response (message.usage), from backfill --usage and
-- from modules.usage.index_session() at session end. event_key makes it idempotent:
-- resumed/forked transcripts replay the same message ids and must not double count.
CREATE TABLE IF NOT EXISTS usage_events (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key      TEXT NOT NULL UNIQUE,             -- message.id, else record uuid
  session_id     TEXT,
  ts             TEXT,                             -- ISO8601 UTC
  model          TEXT,
  family         TEXT,                             -- fable|opus|sonnet|haiku|other
  input_tokens   INTEGER NOT NULL DEFAULT 0,
  output_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_read     INTEGER NOT NULL DEFAULT 0,
  cache_create   INTEGER NOT NULL DEFAULT 0,
  cost_usd_est   REAL,                             -- ESTIMATE from modules/usage.py PRICES
  project        TEXT,
  sidechain      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events(ts);
CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_events(session_id);
CREATE INDEX IF NOT EXISTS idx_usage_family_ts ON usage_events(family, ts);

-- Transcript text, chunked. Content table for transcript_fts (external content).
CREATE TABLE IF NOT EXISTS transcript_chunks (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  uuid       TEXT,                                 -- source record uuid
  seq        INTEGER NOT NULL DEFAULT 0,           -- chunk index within that record
  ts         TEXT,
  role       TEXT NOT NULL,                        -- user | assistant
  project    TEXT,
  text       TEXT NOT NULL,
  UNIQUE(session_id, uuid, seq)
);
CREATE INDEX IF NOT EXISTS idx_tchunks_session ON transcript_chunks(session_id);
CREATE INDEX IF NOT EXISTS idx_tchunks_ts ON transcript_chunks(ts);

CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
  text, session_id UNINDEXED, role UNINDEXED, project UNINDEXED, ts UNINDEXED,
  content='transcript_chunks', content_rowid='id', tokenize='porter unicode61'
);

-- Every claude.ai artifact link and every other http(s) URL seen in assistant text.
CREATE TABLE IF NOT EXISTS artifacts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  url        TEXT NOT NULL,
  label      TEXT,
  kind       TEXT NOT NULL DEFAULT 'link',         -- artifact | link
  session_id TEXT,
  ts         TEXT,
  project    TEXT,
  count      INTEGER NOT NULL DEFAULT 1,
  UNIQUE(session_id, url)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_url ON artifacts(url);
CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts(kind, ts);

-- Plan rate limits as reported by Claude Code's statusLine payload (hooks/statusline.py) or by
-- an explicit OAuth refresh. One row per observation; the hook samples at most once a minute
-- per session and skips samples identical to the previous one.
CREATE TABLE IF NOT EXISTS rate_limit_samples (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                  TEXT NOT NULL,                -- ISO8601 UTC, when we observed it
  session_id          TEXT,
  model               TEXT,
  five_hour_pct       REAL,
  five_hour_resets_at INTEGER,                      -- unix epoch seconds
  seven_day_pct       REAL,
  seven_day_resets_at INTEGER,
  spend_pct           REAL,
  spend_resets_at     INTEGER,
  context_pct         REAL,
  cost_usd            REAL,                         -- session cost, when the payload carries it
  source              TEXT NOT NULL DEFAULT 'statusline',   -- statusline | oauth
  raw                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_rls_ts ON rate_limit_samples(ts);
CREATE INDEX IF NOT EXISTS idx_rls_session ON rate_limit_samples(session_id, ts);

-- ---------------------------------------------------------------------------
-- -- The PostToolUse hook already parses every Bash command to find written paths and
-- then throws the command away. This keeps it. Append-only; one row per Bash tool
-- call, written by hooks/post_tool_use.py live and by backfill.py over the corpus.
-- event_key (the tool_use id, else session+hash+ts) makes both sources idempotent.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS session_commands (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key   TEXT NOT NULL UNIQUE,
  session_id  TEXT NOT NULL,
  ts          TEXT NOT NULL,                        -- ISO8601 UTC
  cwd         TEXT,
  project     TEXT,
  command     TEXT NOT NULL,                        -- verbatim, capped at 8000 chars
  description TEXT,                                 -- the tool call's own description, when present
  wrote       INTEGER NOT NULL DEFAULT 0,           -- durable paths this command produced
  source      TEXT NOT NULL DEFAULT 'hook'          -- hook | backfill
);
CREATE INDEX IF NOT EXISTS idx_session_commands_session ON session_commands(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_session_commands_ts ON session_commands(ts);
CREATE INDEX IF NOT EXISTS idx_session_commands_project ON session_commands(project, ts);

-- ---------------------------------------------------------------------------
-- the subagent hole in the gate.
-- A tool call made inside a subagent carries the PARENT's session_id and its own
-- `agent_id`, so until now a subagent's file writes were recorded as the parent's and
-- the gate named the wrong author. These two tables keep the authorship.
--   session_agents  — one row per subagent run, closed by hooks/subagent_stop.py
--   agent_files     — which agent touched which path (the parent still owns the gate:
--                     By design, a subagent's files count as the parent's, but the
--                     block message must say who actually wrote them)
-- Both are append-only and additive: no existing table changes shape.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS session_agents (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_session_id TEXT NOT NULL,
  agent_id          TEXT NOT NULL,
  agent_type        TEXT,
  started_at        TEXT,
  ended_at          TEXT,
  transcript_path   TEXT,
  cwd               TEXT,
  project           TEXT,
  last_message      TEXT,                            -- last_assistant_message, capped
  recap             TEXT,                            -- what the agent said it did
  files             INTEGER NOT NULL DEFAULT 0,      -- durable paths attributed to it
  stops             INTEGER NOT NULL DEFAULT 0,      -- SubagentStop events seen
  item_id           INTEGER REFERENCES items(id),    -- set when its work gets logged
  created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE(parent_session_id, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_session_agents_parent ON session_agents(parent_session_id);

CREATE TABLE IF NOT EXISTS agent_files (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_session_id TEXT NOT NULL,
  agent_id          TEXT NOT NULL,
  agent_type        TEXT,
  path              TEXT NOT NULL,
  op                TEXT NOT NULL DEFAULT 'modify',  -- create|modify|bash
  tool              TEXT,
  first_ts          TEXT,
  last_ts           TEXT,
  count             INTEGER NOT NULL DEFAULT 1,
  UNIQUE(agent_id, path)
);
CREATE INDEX IF NOT EXISTS idx_agent_files_session ON agent_files(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_agent_files_path ON agent_files(path);

-- Plan limits read from other AI tools' logs (Claude's own live in rate_limit_samples).
CREATE TABLE IF NOT EXISTS agent_usage (
  agent          TEXT NOT NULL,
  window         TEXT NOT NULL,                    -- primary | secondary | ...
  ts             TEXT NOT NULL,                    -- when the tool reported it (ISO8601 UTC)
  used_pct       REAL,
  window_minutes INTEGER,
  resets_at      INTEGER,                          -- unix epoch seconds
  plan           TEXT,
  PRIMARY KEY (agent, window)
);
