"""Tests for the ESPN -> SQLite sync.

Three properties matter more than the mapping, and each has its own tests:

* **``sync_draft`` returns only picks that were new to this database.** The
  draft loop triggers advice off that list; returning every pick on every poll
  would re-advise Caroline every five seconds.
* **A failed sync leaves the previous snapshot intact.** The web app showing
  stale data behind a banner beats it showing nothing while the pick clock runs.
* **``write_league_memory`` preserves everything below the sentinel.** The
  committed ``memory/league.md`` promises that in writing.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

# The pinned half of the contract, defined by Task 1 and imported rather than
# retyped: if either side moves the sentinel, this fails loudly instead of
# silently eating Caroline's notes.
from test_project_files import LEAGUE_PRESERVE_SENTINEL as PINNED_SENTINEL

from conftest import FIXTURE_ENV, draft_transport, load_espn_fixture
from hal_mary import db
from hal_mary.config import PathsConfig, load_settings
from hal_mary.espn import sync as espn_sync
from hal_mary.espn.client import EspnClient, EspnUnavailable
from hal_mary.espn.sync import (
    LEAGUE_PRESERVE_SENTINEL,
    last_sync,
    league_memory_path,
    sync_draft,
    sync_league,
    sync_players,
    write_league_memory,
)

LEAGUE_SETTINGS = {
    "season": 2025,
    "league_id": 1234567,
    "name": "The Gridiron Gauntlet",
    "team_count": 4,
    "scoring_type": "H2H_POINTS",
    "draft_type": "SNAKE",
    "draft_date": "2025-08-26T00:00:00+00:00",
    "roster_slots": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "BE": 6},
    "raw_json": json.dumps({"name": "The Gridiron Gauntlet"}),
}

TEAMS = [
    {"team_id": 1, "name": "Hail Mary", "owner": "Caroline Reed", "abbrev": "HAL",
     "draft_slot": 2},
    {"team_id": 2, "name": "Blitz Brigade", "owner": "Dana Whitlock", "abbrev": "BLZ",
     "draft_slot": 4},
]

ROSTERS = [
    {"team_id": 1, "player_id": 3139477, "name": "Patrick Mahomes", "position": "QB",
     "pro_team": "KC", "injury_status": "ACTIVE", "slot": "QB"},
    {"team_id": 1, "player_id": 4362628, "name": "Bijan Robinson", "position": "RB",
     "pro_team": "ATL", "injury_status": "QUESTIONABLE", "slot": "RB"},
    {"team_id": 2, "player_id": 4569618, "name": "Puka Nacua", "position": "WR",
     "pro_team": "LAR", "injury_status": "ACTIVE", "slot": "WR"},
]

FREE_AGENTS = [
    {"player_id": 4426515, "name": "Sam LaPorta", "position": "TE", "pro_team": "DET",
     "injury_status": "OUT", "percent_owned": 62.3},
]

PICKS = [
    {"overall_pick": 1, "round_num": 1, "round_pick": 1, "team_id": 2,
     "player_id": 4362628, "player_name": "Bijan Robinson"},
    {"overall_pick": 2, "round_num": 1, "round_pick": 2, "team_id": 1,
     "player_id": 3139477, "player_name": "Patrick Mahomes"},
    {"overall_pick": 3, "round_num": 1, "round_pick": 3, "team_id": 1,
     "player_id": 4569618, "player_name": "Puka Nacua"},
]

FOURTH_PICK = {
    "overall_pick": 4, "round_num": 1, "round_pick": 4, "team_id": 2,
    "player_id": 4426515, "player_name": "Sam LaPorta",
}


class StubClient:
    """A client that answers from memory, so sync tests are about sync."""

    def __init__(self, settings=None, **overrides):
        self.settings = settings
        self.data = {
            "league_settings": LEAGUE_SETTINGS,
            "teams": TEAMS,
            "rosters": ROSTERS,
            "free_agents": FREE_AGENTS,
            "draft_picks": PICKS,
        }
        self.data.update(overrides)
        self.fail_on: str | None = None
        self.calls: list[str] = []

    def _answer(self, name):
        self.calls.append(name)
        if self.fail_on == name:
            raise EspnUnavailable(f"ESPN returned HTTP 503 for {name}")
        return self.data[name]

    def league_settings(self):
        return self._answer("league_settings")

    def teams(self):
        return self._answer("teams")

    def rosters(self):
        return self._answer("rosters")

    def free_agents(self, size=200, position=None):
        return self._answer("free_agents")

    def draft_picks(self):
        return self._answer("draft_picks")


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "hal.db")
    db.migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def settings(tmp_path):
    """Settings whose memory dir is a scratch directory, not the repo's."""
    base = load_settings(env=FIXTURE_ENV)
    memory = tmp_path / "memory"
    memory.mkdir()
    return base.model_copy(
        update={"paths": PathsConfig(prompts_dir=base.paths.prompts_dir, memory_dir=str(memory))}
    )


def rows(conn, sql, *params):
    return [dict(row) for row in conn.execute(sql, params)]


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --- sync_league -----------------------------------------------------------


def test_sync_league_populates_every_table_and_reports_counts(conn, settings):
    summary = sync_league(conn, StubClient(settings))

    assert count(conn, "league_settings") == 1
    assert count(conn, "teams") == 2
    assert count(conn, "players") == 4  # three rostered plus one free agent
    assert count(conn, "roster_slots") == 3
    assert summary["teams"] == 2
    assert summary["players"] == 4
    assert summary["roster_slots"] == 3
    assert summary["free_agents"] == 1


def test_sync_league_writes_the_single_settings_row(conn, settings):
    sync_league(conn, StubClient(settings))

    row = rows(conn, "SELECT * FROM league_settings")[0]
    assert row["id"] == 1
    assert row["name"] == "The Gridiron Gauntlet"
    assert row["draft_date"] == "2025-08-26T00:00:00+00:00"
    assert json.loads(row["roster_slots_json"])["RB"] == 2
    assert row["updated_at"]


def test_sync_league_is_idempotent(conn, settings):
    sync_league(conn, StubClient(settings))
    sync_league(conn, StubClient(settings))

    assert count(conn, "league_settings") == 1
    assert count(conn, "teams") == 2
    assert count(conn, "players") == 4
    assert count(conn, "roster_slots") == 3


def test_sync_league_records_an_ok_sync_run(conn, settings):
    sync_league(conn, StubClient(settings))

    run = rows(conn, "SELECT * FROM sync_runs WHERE kind = 'league'")[0]
    assert run["status"] == "ok"
    assert run["started_at"] and run["finished_at"]
    assert run["error"] is None


def test_sync_league_drops_a_player_who_left_a_roster(conn, settings):
    sync_league(conn, StubClient(settings))

    shorter = [row for row in ROSTERS if row["player_id"] != 4362628]
    sync_league(conn, StubClient(settings, rosters=shorter))

    remaining = {row["player_id"] for row in rows(conn, "SELECT player_id FROM roster_slots")}
    assert 4362628 not in remaining
    # The player row itself survives: the board and old picks still reference it.
    assert count(conn, "players") == 4


def test_a_failed_sync_leaves_the_previous_snapshot_intact(conn, settings):
    sync_league(conn, StubClient(settings))
    before = rows(conn, "SELECT * FROM teams ORDER BY team_id")

    broken = StubClient(settings, teams=[{"team_id": 9, "name": "Ghost", "owner": None,
                                         "abbrev": "GHO", "draft_slot": None}])
    broken.fail_on = "rosters"

    with pytest.raises(EspnUnavailable):
        sync_league(conn, broken)

    assert rows(conn, "SELECT * FROM teams ORDER BY team_id") == before


def test_a_write_that_fails_mid_transaction_restores_the_old_roster(conn, settings):
    """The roster snapshot is replaced by a delete-then-insert; prove it rolls back.

    A roster row naming a team ESPN never sent violates roster_slots' foreign
    key, which is exactly the kind of surprise a real payload can spring. The
    delete that preceded it must be undone with it.
    """
    sync_league(conn, StubClient(settings))
    before = rows(conn, "SELECT * FROM roster_slots ORDER BY id")
    assert before

    orphaned = [{**ROSTERS[0], "team_id": 99}]

    with pytest.raises(sqlite3.IntegrityError):
        sync_league(conn, StubClient(settings, rosters=orphaned))

    assert rows(conn, "SELECT * FROM roster_slots ORDER BY id") == before


def test_a_failed_sync_records_the_error(conn, settings):
    broken = StubClient(settings)
    broken.fail_on = "teams"

    with pytest.raises(EspnUnavailable):
        sync_league(conn, broken)

    run = rows(conn, "SELECT * FROM sync_runs WHERE kind = 'league'")[0]
    assert run["status"] == "error"
    assert "503" in run["error"]
    assert run["finished_at"]


def test_sync_league_writes_the_league_memory_file(conn, settings):
    sync_league(conn, StubClient(settings))

    text = (league_memory_path(settings)).read_text(encoding="utf-8")
    assert "The Gridiron Gauntlet" in text


def test_sync_league_works_against_the_real_client(conn, settings, fake_espn):
    """The stub above pins shapes; this pins that the client actually makes them."""
    payload = load_espn_fixture("draft_detail_partial.json")
    client = EspnClient(settings, transport=draft_transport(payload))

    summary = sync_league(conn, client)

    assert summary["teams"] == 4
    assert summary["roster_slots"] == 6
    assert rows(conn, "SELECT name FROM league_settings")[0]["name"] == "The Gridiron Gauntlet"


# --- sync_draft ------------------------------------------------------------


def test_sync_draft_returns_every_pick_the_first_time(conn):
    new = sync_draft(conn, StubClient())

    assert [pick["overall_pick"] for pick in new] == [1, 2, 3]
    assert count(conn, "draft_picks") == 3


def test_sync_draft_returns_nothing_when_the_input_has_not_changed(conn):
    """The idempotency the draft loop is built on."""
    sync_draft(conn, StubClient())

    assert sync_draft(conn, StubClient()) == []
    assert count(conn, "draft_picks") == 3


def test_sync_draft_returns_only_the_pick_that_is_new(conn):
    sync_draft(conn, StubClient())

    new = sync_draft(conn, StubClient(draft_picks=[*PICKS, FOURTH_PICK]))

    assert [pick["overall_pick"] for pick in new] == [4]
    assert count(conn, "draft_picks") == 4


def test_sync_draft_keeps_the_moment_a_pick_was_first_seen(conn):
    sync_draft(conn, StubClient())
    first_seen = rows(conn, "SELECT seen_at FROM draft_picks WHERE overall_pick = 1")[0]

    corrected = [{**PICKS[0], "player_id": 999, "player_name": None}, *PICKS[1:]]
    sync_draft(conn, StubClient(draft_picks=corrected))

    row = rows(conn, "SELECT * FROM draft_picks WHERE overall_pick = 1")[0]
    assert row["seen_at"] == first_seen["seen_at"]
    assert row["player_id"] == 999


def test_sync_draft_names_a_pick_from_the_players_table(conn):
    sync_players(conn, [{"player_id": 4362628, "name": "Bijan Robinson", "position": "RB",
                         "pro_team": "ATL", "injury_status": None}])
    unnamed = [{**PICKS[0], "player_name": None}]

    new = sync_draft(conn, StubClient(draft_picks=unnamed))

    assert new[0]["player_name"] == "Bijan Robinson"
    assert rows(conn, "SELECT player_name FROM draft_picks")[0]["player_name"] == "Bijan Robinson"


def test_sync_draft_tolerates_a_player_nobody_can_name(conn):
    unknown = [{**PICKS[0], "player_id": 9999999, "player_name": None}]

    new = sync_draft(conn, StubClient(draft_picks=unknown))

    assert new[0]["player_name"] is None


def test_sync_draft_ignores_espns_prepopulated_board(conn, settings, fake_espn):
    """The bug this test exists for.

    ESPN writes all 96 slots of the board before the draft starts. The first real
    sync read them as 96 completed picks, which would have told the draft loop an
    entire draft happened in one tick.
    """
    payload = load_espn_fixture("draft_detail_prepopulated_real_league.json")
    assert len(payload["draftDetail"]["picks"]) == 96
    client = EspnClient(settings, transport=draft_transport(payload))

    assert sync_draft(conn, client) == []
    assert count(conn, "draft_picks") == 0


def test_sync_draft_sees_the_first_real_pick_land_on_that_board(conn, settings, fake_espn):
    payload = load_espn_fixture("draft_detail_prepopulated_real_league.json")
    client = EspnClient(settings, transport=draft_transport(payload))
    sync_draft(conn, client)

    payload["draftDetail"]["picks"][0]["playerId"] = 4362628
    new = sync_draft(conn, EspnClient(settings, transport=draft_transport(payload)))

    assert [pick["overall_pick"] for pick in new] == [1]
    assert new[0]["player_name"] == "Bijan Robinson"
    assert count(conn, "draft_picks") == 1


def test_sync_draft_records_a_sync_run(conn):
    sync_draft(conn, StubClient())

    run = rows(conn, "SELECT * FROM sync_runs WHERE kind = 'draft'")[0]
    assert run["status"] == "ok"


def test_a_failed_draft_sync_keeps_the_picks_it_already_had(conn):
    sync_draft(conn, StubClient())
    broken = StubClient()
    broken.fail_on = "draft_picks"

    with pytest.raises(EspnUnavailable):
        sync_draft(conn, broken)

    assert count(conn, "draft_picks") == 3
    statuses = [row["status"] for row in rows(conn, "SELECT status FROM sync_runs")]
    assert "error" in statuses


# --- sync_players ----------------------------------------------------------


def test_sync_players_upserts_and_counts(conn):
    assert sync_players(conn, ROSTERS) == 3
    assert count(conn, "players") == 3


def test_sync_players_updates_a_player_it_has_seen(conn):
    sync_players(conn, ROSTERS)
    sync_players(conn, [{**ROSTERS[0], "injury_status": "OUT"}])

    row = rows(conn, "SELECT * FROM players WHERE player_id = 3139477")[0]
    assert row["injury_status"] == "OUT"
    assert count(conn, "players") == 3


def test_sync_players_skips_rows_it_cannot_key_or_name(conn):
    assert sync_players(conn, [{"player_id": None, "name": "Nobody"},
                               {"player_id": 1, "name": None}]) == 0
    assert count(conn, "players") == 0


def test_sync_players_deduplicates_within_one_batch(conn):
    assert sync_players(conn, [ROSTERS[0], dict(ROSTERS[0])]) == 1


# --- last_sync -------------------------------------------------------------


def test_last_sync_is_none_before_anything_has_run(conn):
    assert last_sync(conn, "league") is None


def test_last_sync_returns_the_newest_run_of_that_kind(conn, settings):
    sync_league(conn, StubClient(settings))
    sync_draft(conn, StubClient())
    sync_draft(conn, StubClient())

    latest = last_sync(conn, "draft")
    assert latest["kind"] == "draft"
    assert latest["id"] == max(
        row["id"] for row in rows(conn, "SELECT id FROM sync_runs WHERE kind = 'draft'")
    )


# --- write_league_memory ---------------------------------------------------


def test_the_sentinel_matches_the_contract_task_one_pinned():
    assert LEAGUE_PRESERVE_SENTINEL == PINNED_SENTINEL


def test_write_league_memory_describes_the_league(conn, settings):
    sync_league(conn, StubClient(settings))

    text = (league_memory_path(settings)).read_text(encoding="utf-8")

    assert "The Gridiron Gauntlet" in text
    assert "H2H_POINTS" in text
    assert "2025-08-26" in text
    assert "SNAKE" in text.upper()


def test_write_league_memory_names_carolines_team(conn, settings):
    sync_league(conn, StubClient(settings))

    text = (league_memory_path(settings)).read_text(encoding="utf-8")
    assert "Hail Mary" in text


def test_write_league_memory_lists_the_roster_slots(conn, settings):
    sync_league(conn, StubClient(settings))

    text = (league_memory_path(settings)).read_text(encoding="utf-8")
    assert "QB" in text and "RB" in text


def test_write_league_memory_preserves_hand_written_notes(conn, settings):
    path = league_memory_path(settings)
    path.write_text(
        "# stale machine half\n\n---\n\n"
        f"{LEAGUE_PRESERVE_SENTINEL}\n\n"
        "## Notes added by hand\n\nDana always overpays for kickers.\n",
        encoding="utf-8",
    )

    sync_league(conn, StubClient(settings))

    text = path.read_text(encoding="utf-8")
    assert "Dana always overpays for kickers." in text
    assert "stale machine half" not in text
    assert text.count(LEAGUE_PRESERVE_SENTINEL) == 1


def test_write_league_memory_creates_the_file_when_it_is_missing(conn, settings):
    path = league_memory_path(settings)
    assert not path.exists()

    sync_league(conn, StubClient(settings))

    text = path.read_text(encoding="utf-8")
    assert LEAGUE_PRESERVE_SENTINEL in text
    assert text.split(LEAGUE_PRESERVE_SENTINEL)[1].strip(), "hand-written half must be seeded"


def test_a_generated_file_still_satisfies_task_ones_pins(conn, settings):
    """Regenerating memory/league.md must not break the tests that pin its shape."""
    sync_league(conn, StubClient(settings))
    text = (league_memory_path(settings)).read_text(encoding="utf-8")

    assert text.count(LEAGUE_PRESERVE_SENTINEL) == 1
    before, _, after = text.partition(LEAGUE_PRESERVE_SENTINEL)
    assert "---" in before
    assert after.strip()
    assert "hal-mary sync" in text
    assert "preserved" in text.lower()


def test_write_league_memory_says_so_when_nothing_has_synced(conn, settings):
    write_league_memory(conn, settings)

    text = (league_memory_path(settings)).read_text(encoding="utf-8")
    assert "not synced" in text.lower()


# --- transaction guards ----------------------------------------------------


def test_sync_league_refuses_to_run_inside_a_caller_transaction(conn, settings):
    """SQLite has no nested transactions, and the error row must survive a failure.

    A caller wrapping a sync in its own transaction would not only get "cannot
    start a transaction within a transaction" — the sync_runs error row would be
    rolled back with everything else, silently losing the record of the failure.
    """
    conn.execute("BEGIN")
    try:
        with pytest.raises(RuntimeError, match="transaction"):
            sync_league(conn, StubClient(settings))
    finally:
        conn.execute("ROLLBACK")


def test_sync_draft_refuses_to_run_inside_a_caller_transaction(conn):
    conn.execute("BEGIN")
    try:
        with pytest.raises(RuntimeError, match="transaction"):
            sync_draft(conn, StubClient())
    finally:
        conn.execute("ROLLBACK")


# --- memory write failures -------------------------------------------------


def test_a_memory_write_failure_is_recorded_rather_than_reported_as_success(conn, settings):
    """memory/league.md is standing context on every Claude call; losing it matters."""
    # A regular file where a directory has to be: mkdir raises NotADirectoryError.
    blocker = Path(settings.paths.memory_dir).parent / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    unwritable = settings.model_copy(
        update={
            "paths": PathsConfig(
                prompts_dir=settings.paths.prompts_dir,
                memory_dir=str(blocker / "memory"),
            )
        }
    )

    with pytest.raises(OSError):
        sync_league(conn, StubClient(unwritable))

    run = rows(conn, "SELECT * FROM sync_runs WHERE kind = 'league'")[0]
    assert run["status"] == "error"
    assert run["error"]


# --- sync_runs growth ------------------------------------------------------


@pytest.fixture
def small_retention(monkeypatch):
    """Shrink the retention so the pruning tests are about pruning, not patience."""
    monkeypatch.setattr(espn_sync, "SYNC_RUN_KEEP", 5)
    return 5


def test_sync_runs_are_pruned_so_a_draft_poll_cannot_grow_them_without_bound(
    conn, small_retention
):
    """A five-second poll writes ~720 rows an hour; the table has to be bounded."""
    for _ in range(small_retention + 4):
        sync_draft(conn, StubClient())

    assert count(conn, "sync_runs") == small_retention


def test_pruning_never_touches_another_kind(conn, settings, small_retention):
    sync_league(conn, StubClient(settings))
    for _ in range(small_retention + 4):
        sync_draft(conn, StubClient())

    assert count(conn, "sync_runs WHERE kind = 'league'") == 1


def test_pruning_keeps_the_newest_run_so_the_staleness_banner_still_works(
    conn, small_retention
):
    """A quiet draft and a dead loop must stay distinguishable."""
    for _ in range(small_retention + 4):
        sync_draft(conn, StubClient())

    latest = last_sync(conn, "draft")
    assert latest is not None
    assert latest["status"] == "ok"
    assert latest["finished_at"]


def test_the_shipped_retention_covers_a_live_draft(settings):
    """At a five-second poll, the window kept has to be worth looking at."""
    minutes = espn_sync.SYNC_RUN_KEEP * settings.draft.poll_seconds / 60
    assert minutes >= 15
