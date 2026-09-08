"""Tests for the one accessor that answers "what league is this?".

hal-mary has to be able to run a draft with **no ESPN access at all**. Picks
already have a manual path; the league's own settings did not, and this is the
module that closes that gap. Everything downstream — how many rounds there are,
which picks are Caroline's, whether a catch is worth a point — is read through
here, so the precedence rule is worth pinning hard:

* a synced ``league_settings`` row wins;
* a ``[league]`` section in ``config.toml`` fills in for it when nothing has
  synced, and fills individual gaps a partial sync left;
* neither is a loud error naming both fixes, never a guessed twelve-team
  standard-scoring default.
"""

from __future__ import annotations

import json

import pytest
from draft_fixtures import (
    LEAGUE_TOML,
    REAL_RAW_SETTINGS,
    make_settings,
    open_db,
    seed_synced_league,
)

from hal_mary.league import LeagueUnknown, load_league_context


def test_settings_come_from_the_synced_row(tmp_path):
    settings = make_settings(tmp_path)  # no [league] section at all
    conn = open_db(tmp_path)
    seed_synced_league(conn)

    league = load_league_context(conn, settings)

    assert league.source == "espn"
    assert league.team_count == 6
    assert league.draft_order == [1, 2, 3, 4, 5, 6]
    assert league.my_team_id == 6
    assert league.my_draft_slot == 6
    assert league.roster_slots["RB/WR/TE"] == 1
    assert league.points_per_reception == 1.0


def test_settings_fall_back_to_config_when_nothing_has_synced(tmp_path):
    """The whole point of the fallback: an empty database still knows the league."""
    settings = make_settings(tmp_path, LEAGUE_TOML)
    conn = open_db(tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM league_settings").fetchone()[0] == 0

    league = load_league_context(conn, settings)

    assert league.source == "config"
    assert league.team_count == 6
    assert league.draft_order == [1, 2, 3, 4, 5, 6]
    assert league.my_draft_slot == 6
    assert league.my_team_id == 6
    assert league.rounds == 16
    assert league.points_per_reception == 1.0


def test_a_synced_row_beats_the_config_section(tmp_path):
    """Both present and disagreeing: ESPN is the authority on its own league."""
    settings = make_settings(tmp_path, LEAGUE_TOML.replace("team_count = 6", "team_count = 12"))
    conn = open_db(tmp_path)
    seed_synced_league(conn)

    league = load_league_context(conn, settings)

    assert league.source == "espn"
    assert league.team_count == 6


def test_a_partial_synced_row_borrows_the_missing_pieces_from_config(tmp_path):
    """A sync that landed without roster slots is not a reason to know nothing."""
    settings = make_settings(tmp_path, LEAGUE_TOML)
    conn = open_db(tmp_path)
    seed_synced_league(conn, roster_slots_json=json.dumps({}))

    league = load_league_context(conn, settings)

    assert league.source == "espn"
    assert league.roster_slots["RB"] == 2
    assert league.rounds == 16


def test_knowing_nothing_is_a_loud_error_naming_both_fixes(tmp_path):
    settings = make_settings(tmp_path)  # no [league] section, no synced row
    conn = open_db(tmp_path)

    with pytest.raises(LeagueUnknown) as excinfo:
        load_league_context(conn, settings)

    message = str(excinfo.value)
    assert "hal-mary sync" in message
    assert "[league]" in message


def test_rounds_exclude_the_injured_reserve_slot(tmp_path):
    """Sixteen rounds, not seventeen: nobody drafts into an IR slot."""
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn)

    league = load_league_context(conn, settings)

    assert league.rounds == 16
    assert league.total_picks == 96


def test_scoring_summary_says_full_ppr_in_words_a_beginner_understands(tmp_path):
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn)

    summary = load_league_context(conn, settings).scoring_summary.lower()

    assert "catch" in summary
    assert "point" in summary


def test_standard_scoring_is_described_as_such(tmp_path):
    """The summary is computed, not assumed: a league with no reception points
    must not be described as PPR."""
    raw = json.loads(json.dumps(REAL_RAW_SETTINGS))
    raw["scoringSettings"]["scoringItems"] = [{"statId": 53, "points": 0.0}]
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn, raw=raw)

    league = load_league_context(conn, settings)

    assert league.points_per_reception == 0.0
    assert "ppr" not in league.scoring_summary.lower()


def test_the_last_slot_picks_back_to_back(tmp_path):
    """Caroline picks 6 and 7. Her first two picks are one decision, not two, and
    the advisor cannot know that unless this accessor gets the order right."""
    settings = make_settings(tmp_path, LEAGUE_TOML)
    conn = open_db(tmp_path)

    league = load_league_context(conn, settings)

    assert league.upcoming_picks(1)[:2] == [6, 7]


def test_config_draft_order_may_name_teams_instead_of_ids(tmp_path):
    """Caroline can fill the fallback in from the ESPN draft lobby, which shows
    names and no ids at all."""
    toml = """
[league]
team_count = 4
my_draft_slot = 3
draft_order = ["Dana", "Priya", "Caroline", "Sam"]

[league.roster_slots]
QB = 1
RB = 2
BE = 3
"""
    settings = make_settings(tmp_path, toml)
    conn = open_db(tmp_path)

    league = load_league_context(conn, settings)

    assert league.draft_order_labels == ["Dana", "Priya", "Caroline", "Sam"]
    assert league.my_draft_slot == 3
    assert league.my_team_id == league.draft_order[2]
    assert league.rounds == 6


def test_config_toml_ships_the_fallback_section_commented_out():
    """Caroline's operator has to be able to find this without reading the source.

    The `[league]` block is the difference between hal-mary being useful on draft
    night with no ESPN credentials and being unable to help at all, so it ships
    in the file, commented out, saying what it is for.
    """
    from draft_fixtures import REPO

    text = (REPO / "config.toml").read_text(encoding="utf-8")
    body = "\n".join(line.lstrip("# ") for line in text.splitlines() if line.startswith("#"))
    assert "[league]" in body
    assert "fallback" in body.lower()
    for key in ("team_count", "scoring_type", "roster_slots", "draft_order", "my_draft_slot"):
        assert key in body, f"the commented [league] section does not mention {key}"


def test_the_pick_clock_comes_from_espn(tmp_path):
    """`draftSettings.timePerSelection`. Every timing decision in this project is
    sized against it, so it is read rather than assumed — a league that shortens
    its clock must move those budgets, not silently break them."""
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn)

    assert load_league_context(conn, settings).pick_clock_s == 90


def test_the_pick_clock_falls_back_to_config_with_no_espn(tmp_path):
    # Into the [league] table, not the [league.roster_slots] one below it.
    toml = LEAGUE_TOML.replace("my_draft_slot = 6", "my_draft_slot = 6\npick_clock_s = 60")
    settings = make_settings(tmp_path, toml)
    conn = open_db(tmp_path)

    assert load_league_context(conn, settings).pick_clock_s == 60


def test_the_playoff_shape_comes_from_espn(tmp_path):
    """`scheduleSettings` decides what the season is actually a race for.

    A board built for "win each week" and a board built for "score the most
    points over fourteen weeks" are different boards, and which one is right is
    a league setting rather than a matter of taste. It is read for the same
    reason the pick clock is: a league that reseeds by record has to move the
    reasoning, not silently inherit the wrong one.
    """
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn)

    league = load_league_context(conn, settings)

    assert league.playoff_team_count == 4
    assert league.playoff_seeding_rule == "TOTAL_POINTS_SCORED"
    assert league.regular_season_weeks == 14


def test_the_playoff_shape_falls_back_to_config_with_no_espn(tmp_path):
    toml = LEAGUE_TOML.replace(
        "my_draft_slot = 6",
        'my_draft_slot = 6\nplayoff_team_count = 4\n'
        'playoff_seeding_rule = "TOTAL_POINTS_SCORED"\nregular_season_weeks = 14',
    )
    settings = make_settings(tmp_path, toml)
    conn = open_db(tmp_path)

    league = load_league_context(conn, settings)

    assert league.playoff_team_count == 4
    assert league.regular_season_weeks == 14


def test_playoff_summary_says_in_words_what_the_season_is_a_race_for(tmp_path):
    """`TOTAL_POINTS_SCORED` is a database value, not a sentence.

    The whole summary is asserted rather than a slice of it: a test that checks
    only the half its author was thinking about reads as though it checked the
    sentence and did not.
    """
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn)

    summary = load_league_context(conn, settings).playoff_summary

    assert "TOTAL_POINTS_SCORED" not in summary
    assert "4 of the 6" in summary
    assert "total points" in summary
    assert "14" in summary, "how long the race is, not only who wins it"
    assert "most of the league" in summary, "two thirds getting in changes the strategy"


def test_playoff_summary_says_so_when_nothing_has_been_read(tmp_path):
    """Silence would read as "there are no playoffs", which is never true."""
    raw = {key: value for key, value in REAL_RAW_SETTINGS.items() if key != "scheduleSettings"}
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn, raw=raw)

    league = load_league_context(conn, settings)

    assert league.playoff_team_count is None
    assert "not known" in league.playoff_summary
