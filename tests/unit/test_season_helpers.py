"""Tests for the state the four in-season jobs share.

The week is the load-bearing one. Every bye check is measured against it, so a
week hal-mary cannot work out is a bye check that silently does not happen — and
the scenario that produces it is ordinary: ESPN cookies expire on Friday and
Sunday morning's call comes back with nothing.
"""

from __future__ import annotations

from draft_fixtures import make_settings, open_db, seed_synced_league

from hal_mary.jobs import season


def ready(tmp_path, **overrides):
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn, **overrides)
    return conn, settings


class Client:
    def __init__(self, week):
        self.week = week

    def current_week(self):
        if isinstance(self.week, Exception):
            raise self.week
        return self.week


def test_the_week_comes_from_espn_when_espn_will_say(tmp_path):
    conn, settings = ready(tmp_path, current_week=5)
    assert season.current_week(conn, settings, Client(9)) == 9


def test_the_week_falls_back_to_the_one_the_last_sync_stored(tmp_path):
    """The failure this exists for: cookies expire on Friday, Sunday's call
    returns nothing, and the week Thursday's sync wrote is still right."""
    conn, settings = ready(tmp_path, current_week=6)
    assert season.current_week(conn, settings, None) == 6


def test_an_espn_client_that_raises_falls_back_rather_than_failing(tmp_path):
    conn, settings = ready(tmp_path, current_week=6)
    assert season.current_week(conn, settings, Client(RuntimeError("401"))) == 6


def test_an_espn_client_that_answers_none_falls_back_too(tmp_path):
    conn, settings = ready(tmp_path, current_week=6)
    assert season.current_week(conn, settings, Client(None)) == 6


def test_with_nothing_anywhere_the_week_is_unknown_rather_than_guessed(tmp_path):
    """A week worked out from the calendar flags the wrong players, and a bye
    warning she learns to disbelieve is worse than none at all."""
    conn, settings = ready(tmp_path, current_week=None)
    assert season.current_week(conn, settings, None) is None


def test_it_does_not_raise_on_a_database_with_no_league_row(tmp_path):
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    assert season.current_week(conn, settings, None) is None
