-- Picks the board could not place.
--
-- `apply_picks` returns an unmatched pick rather than guessing which board row a
-- name belongs to, which is the right call: guessing marks a player gone who is
-- still there, and the advisor then recommends someone who has been on another
-- team's roster for twenty minutes.
--
-- But an unmatched pick means the board and reality disagree about who is gone,
-- and that is the one thing that makes a recommendation actively wrong. A line
-- in a log file is not good enough while a 90-second clock is running, so the
-- draft loop writes them here and the draft page reads them back.
--
-- resolved_at is set when a human has dealt with it (marked the player gone by
-- hand, or decided the pick was a player the board never carried). Nothing in
-- Task 6b sets it; the column exists so the page can.
--
-- Note for whoever wires the page: setting it dismisses the warning and nothing
-- more. `loop.pending_picks` skips every filed row, resolved or not, so a
-- resolved pick is never re-applied to the board. That is the intended meaning
-- of "resolved" here — "I have dealt with this" rather than "try again" — but a
-- retry button would need that filter narrowed to unresolved rows.
CREATE TABLE unmatched_picks (
    id           INTEGER PRIMARY KEY,
    overall_pick INT,
    team_id      INT,
    player_id    INT,
    player_name  TEXT,
    seen_at      TEXT,
    noticed_at   TEXT NOT NULL,
    resolved_at  TEXT
);

-- One row per pick, not one per poll. A pick is only ever new to the database
-- once, but a re-applied board or a re-run loop must not stack duplicates in
-- front of Caroline. NULLs compare distinct in SQLite, so a hand-entered pick
-- with no number is never merged into another one.
CREATE UNIQUE INDEX idx_unmatched_picks_overall ON unmatched_picks (overall_pick);

CREATE INDEX idx_unmatched_picks_open ON unmatched_picks (resolved_at, id);
