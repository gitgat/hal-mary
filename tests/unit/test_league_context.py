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
    assert league.roster_slots["FLEX"] == 1
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
