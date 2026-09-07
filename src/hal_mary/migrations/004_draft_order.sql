-- The draft order ESPN actually drew, as opposed to the one it was guessing.
--
-- `draftSettings.orderType` on this league is DRAFT_START: the order is assigned
-- when the draft opens, so the `pickOrder` a pre-draft sync stored is a
-- placeholder — and in this league it is the identity permutation [1..6], which
-- is a 1-in-720 coincidence as a real draw. Nothing re-runs `sync_league` during
-- a draft, so that placeholder would otherwise be frozen for the whole night
-- while the real order sat one HTTP read away.
--
-- The draft loop reads ESPN's own slot-to-team board on the first poll that sees
-- a real pick and writes round one's mapping here, once. `league._espn_order`
-- prefers it over `pickOrder`, so the draft page, the advisor and the loop all
-- derive her pick window from the same list — which is the point. Two sources
-- would not contradict each other visibly; they would agree and be wrong
-- together, because the page and the advisor run identical snake arithmetic.
--
-- **Why its own table rather than a column on league_settings.**
-- `sync._write_league_settings` is an INSERT OR REPLACE of the whole row, so a
-- column there is erased by the next `hal-mary sync` — and the very next thing
-- that sync writes is ESPN's stale pre-draft pickOrder. Someone tapping /sync
-- mid-draft would silently undo this. A separate table is also the honest shape:
-- this is not a league setting ESPN reports, it is an observation the loop made
-- at a particular moment, and recorded_at says when.
--
-- slot is the 1-based first-round pick number, so slot 1 picks first. Written
-- once and never updated: the order is drawn once, and a second read that
-- disagreed (ESPN is unofficial, and a restart mid-draft re-reads) must not move
-- her pick window while she is looking at it. INSERT OR IGNORE is what makes
-- that a property of the schema rather than of the caller.
CREATE TABLE draft_order (
    slot        INTEGER PRIMARY KEY,
    team_id     INT NOT NULL,
    recorded_at TEXT NOT NULL
);
