-- 004_actions.sql — the plan hal-mary hands to Claude Cowork, and the log of
-- what Cowork did with it.
--
-- hal-mary decides, Cowork executes, Cowork reports back. Cowork holds none of
-- the season's context and is never asked to choose, because its browser reads
-- pages full of five other league members' text and an executor with discretion
-- is something injected text can redirect. Everything in this migration exists
-- to make an instruction concrete enough that there is nothing left to decide.
--
-- See docs/superpowers/specs/2026-09-07-cowork-manager-design.md.

-- One concrete instruction: a player, a slot, a click.
--
-- `sequence`, `depends_on`, `deadline` and `reversible` are here because roster
-- mechanics make order load-bearing:
--
--   * A roster has a fixed size, so adding usually implies dropping, and the
--     wrong order loses a player for nothing.
--   * Lineup changes lock per player at kickoff, not on one weekly deadline, so
--     a deadline belongs to the action rather than to the run.
--   * A waiver claim is a submitted request, not an acquisition. `done` on a
--     claim means "submitted"; hal-mary learns the result from the next sync.
--
-- `paired_player_name` is the other player in a two-player move: who takes the
-- slot on a `bench`, who leaves it on a `start`, who is dropped on a `claim`. A
-- swap is one action naming both, never two that briefly leave the lineup
-- invalid.
--
-- `player_name` is spelled exactly as ESPN spells it, because Cowork finds the
-- player by reading that string off the page.
--
-- status: pending | done | failed | skipped | expired.
--   pending  — issued, not yet reported.
--   done     — Cowork performed it. Never issued again.
--   failed   — Cowork tried and could not. hal-mary decides what to do next.
--   skipped  — Cowork did not attempt it, usually a failed dependency.
--   expired  — the deadline passed before anyone reached it. A stale
--              instruction executed three days late is worse than none.
CREATE TABLE actions (
    id                 INTEGER PRIMARY KEY,
    created_at         TEXT NOT NULL,
    kind               TEXT NOT NULL,
    player_name        TEXT NOT NULL,
    player_id          INT,
    slot               TEXT,
    paired_player_name TEXT,
    reason             TEXT NOT NULL,
    sequence           INT NOT NULL,
    depends_on         TEXT,
    deadline           TEXT,
    reversible         INT NOT NULL,
    source_job         TEXT,
    status             TEXT NOT NULL,
    outcome_detail     TEXT,
    reported_at        TEXT
);

-- The two reads. `pending` is "status = 'pending' ORDER BY sequence", called on
-- every Cowork run and answering "nothing to do" almost every time — so it must
-- be cheap. The second is the dashboard's history and the duplicate check.
CREATE INDEX idx_actions_status_sequence ON actions (status, sequence);
CREATE INDEX idx_actions_created_at ON actions (created_at);

-- Every MCP tool call, whoever made it.
--
-- Bryan chose to let irreversible actions run unattended, so this table is the
-- only way he learns that a drop happened. It is a feature of the product, not
-- instrumentation: it records the call that was made, the arguments it carried
-- and what came back, whether the call succeeded or raised.
--
-- `arguments_json` is truncated at the writer, because report_observation can
-- carry a whole page of someone else's text and this table is meant to stay
-- readable.
CREATE TABLE mcp_calls (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL,
    tool        TEXT NOT NULL,
    arguments_json TEXT,
    outcome     TEXT NOT NULL,   -- ok | error
    detail      TEXT,
    duration_ms INT
);

CREATE INDEX idx_mcp_calls_created_at ON mcp_calls (created_at);

-- Which NFL week ESPN thinks it is.
--
-- Nothing in the schema knew this before: rosters are stored as a snapshot with
-- a NULL week, and the board carries a bye week with nothing to compare it to.
-- "Is this player on bye right now" is the first question the action producer
-- asks, so the answer has to be a synced fact rather than arithmetic on a
-- calendar the code would have to hardcode.
ALTER TABLE league_settings ADD COLUMN current_week INT;
