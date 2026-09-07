-- 001_initial.sql — the whole hal-mary schema.
--
-- Conventions:
--   * timestamps are TEXT holding ISO-8601 (UTC) strings, so they sort lexically
--     and survive a sqlite3 shell session without a converter.
--   * every table that ESPN feeds carries updated_at, so a stale sync is visible.
--   * ids that come from ESPN are the primary key, which makes re-syncing the
--     same object an idempotent INSERT OR REPLACE rather than a diff.

-- The league, as ESPN describes it. Exactly one row, ever: the CHECK makes a
-- second row an error rather than a silent ambiguity.
CREATE TABLE league_settings (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    season            INT,
    league_id         INT,
    name              TEXT,
    team_count        INT,
    scoring_type      TEXT,
    draft_type        TEXT,
    draft_date        TEXT,
    roster_slots_json TEXT,
    raw_json          TEXT,
    updated_at        TEXT
);

CREATE TABLE teams (
    team_id    INT PRIMARY KEY,
    name       TEXT,
    owner      TEXT,
    abbrev     TEXT,
    draft_slot INT,
    updated_at TEXT
);

CREATE TABLE players (
    player_id     INT PRIMARY KEY,
    name          TEXT NOT NULL,
    position      TEXT,
    pro_team      TEXT,
    injury_status TEXT,
    updated_at    TEXT
);

-- Who is on whose roster, in which slot, in which week.
CREATE TABLE roster_slots (
    id         INTEGER PRIMARY KEY,
    team_id    INT NOT NULL REFERENCES teams(team_id),
    player_id  INT NOT NULL REFERENCES players(player_id),
    slot       TEXT,
    week       INT,
    updated_at TEXT,
    UNIQUE (team_id, player_id, week)
);

-- overall_pick is the primary key so re-reading the draft mid-draft and writing
-- the same pick again is a no-op instead of a duplicate.
CREATE TABLE draft_picks (
    overall_pick INTEGER PRIMARY KEY,
    round_num    INT,
    round_pick   INT,
    team_id      INT,
    player_id    INT,
    player_name  TEXT,
    seen_at      TEXT
);

-- The pre-draft ranked board. player_id may be negative: the board is built by
-- research before ESPN player ids are known, so those rows carry a synthetic id
-- until a sync matches them up by name.
CREATE TABLE board (
    player_id          INTEGER PRIMARY KEY,
    name               TEXT NOT NULL,
    position           TEXT,
    pro_team           TEXT,
    tier               INT,
    rank               INT,
    bye_week           INT,
    note               TEXT,
    drafted_by_team_id INT,
    drafted_at         TEXT,
    built_at           TEXT
);

-- Everything hal-mary has learned, one fact per row, with the URL it came from.
-- Football facts are never taken from training knowledge, so source_url is how a
-- claim gets audited later.
CREATE TABLE notes (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL,
    source_job  TEXT,
    topic       TEXT,
    player_name TEXT,
    team_abbr   TEXT,
    text        TEXT NOT NULL,
    source_url  TEXT,
    expires_at  TEXT
);

-- External-content FTS index over notes. The content lives in notes; this table
-- holds only the index, and the triggers below keep the two in step.
CREATE VIRTUAL TABLE notes_fts USING fts5 (
    text,
    player_name,
    topic,
    content = 'notes',
    content_rowid = 'id'
);

CREATE TRIGGER notes_fts_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts (rowid, text, player_name, topic)
    VALUES (new.id, new.text, new.player_name, new.topic);
END;

CREATE TRIGGER notes_fts_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts (notes_fts, rowid, text, player_name, topic)
    VALUES ('delete', old.id, old.text, old.player_name, old.topic);
END;

CREATE TRIGGER notes_fts_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts (notes_fts, rowid, text, player_name, topic)
    VALUES ('delete', old.id, old.text, old.player_name, old.topic);
    INSERT INTO notes_fts (rowid, text, player_name, topic)
    VALUES (new.id, new.text, new.player_name, new.topic);
END;

-- Actionable recommendations shown in the web app. kind is one of
-- 'draft', 'waiver', 'lineup', 'recap'. done is set when Caroline has acted.
CREATE TABLE advice (
    id           INTEGER PRIMARY KEY,
    created_at   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    headline     TEXT NOT NULL,
    body         TEXT,
    payload_json TEXT,
    source_job   TEXT,
    done         INT NOT NULL DEFAULT 0
);

-- One row per scheduled/on-demand job run. status is 'running', 'ok' or 'error'.
CREATE TABLE job_runs (
    id          INTEGER PRIMARY KEY,
    job         TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    summary     TEXT,
    error       TEXT
);

-- One row per ESPN sync.
CREATE TABLE sync_runs (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    error       TEXT
);

-- One row per subprocess call to the claude binary: what was asked, what it
-- cost, how long it took. This is the only place spend is visible.
CREATE TABLE claude_calls (
    id          INTEGER PRIMARY KEY,
    job         TEXT NOT NULL,
    model       TEXT,
    argv_json   TEXT,
    prompt_hash TEXT,
    started_at  TEXT NOT NULL,
    duration_ms INT,
    cost_usd    REAL,
    exit_code   INT,
    session_id  TEXT,
    output_path TEXT,
    error       TEXT
);

CREATE TABLE chat_sessions (
    id                INTEGER PRIMARY KEY,
    claude_session_id TEXT,
    title             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT
);

CREATE TABLE chat_messages (
    id         INTEGER PRIMARY KEY,
    session_id INT NOT NULL REFERENCES chat_sessions(id),
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Indexes for the read paths: newest-first lists, per-team lookups, job history.
CREATE INDEX idx_notes_created_at ON notes (created_at);
CREATE INDEX idx_advice_created_at ON advice (created_at);
CREATE INDEX idx_advice_done ON advice (done);
CREATE INDEX idx_draft_picks_team ON draft_picks (team_id);
CREATE INDEX idx_roster_slots_team ON roster_slots (team_id);
CREATE INDEX idx_job_runs_job_started ON job_runs (job, started_at);
