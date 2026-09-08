"""Tests for the lineup check — the highest-value job hal-mary has.

A started player on a bye week scores **zero**. Nobody ever means to do it, it
is the single most common mistake a beginner makes, and it is entirely
preventable with information hal-mary already has. So the properties under test
here are not "the mapping is right":

* **A starter on a bye is flagged, unmissably, in the headline.**
* **The flag survives a Claude failure.** The bye is arithmetic over a roster
  and a calendar; it does not need a model, and the one thing this job must
  never do is stay quiet about it because a research call timed out.
* **A flag is raised if *either* source says bye.** Over-flagging costs her ten
  seconds of checking; under-flagging costs the whole week.
"""

from __future__ import annotations

import json

import pytest
from draft_fixtures import (
    FakeRunner,
    failed_result,
    make_settings,
    ok_result,
    open_db,
    seed_board,
    seed_roster,
    seed_synced_league,
)

from hal_mary.jobs import lineup_check
from hal_mary.jobs.registry import JobFailed, run_job

WEEK = 6

#: Her roster: two of them are on bye in week 6, one starting and one benched.
ROSTER = [
    {"player_id": 101, "name": "Ja'Marr Chase", "position": "WR", "pro_team": "CIN", "slot": "WR"},
    {"player_id": 102, "name": "Bijan Robinson", "position": "RB", "pro_team": "ATL", "slot": "RB"},
    {"player_id": 103, "name": "Josh Allen", "position": "QB", "pro_team": "BUF", "slot": "QB"},
    {"player_id": 104, "name": "Trey McBride", "position": "TE", "pro_team": "ARI", "slot": "TE"},
    {"player_id": 105, "name": "Puka Nacua", "position": "WR", "pro_team": "LAR", "slot": "BE"},
]

BOARD = [
    {"player_id": 101, "name": "Ja'Marr Chase", "position": "WR", "tier": 1, "rank": 1,
     "bye_week": WEEK},
    {"player_id": 102, "name": "Bijan Robinson", "position": "RB", "tier": 1, "rank": 2,
     "bye_week": 9},
    {"player_id": 103, "name": "Josh Allen", "position": "QB", "tier": 2, "rank": 3,
     "bye_week": 12},
    {"player_id": 104, "name": "Trey McBride", "position": "TE", "tier": 3, "rank": 4,
     "bye_week": 11},
    {"player_id": 105, "name": "Puka Nacua", "position": "WR", "tier": 2, "rank": 5,
     "bye_week": WEEK},
]

ANSWER = {
    "week": WEEK,
    "starters": [
        {"slot": "WR", "player": "Ja'Marr Chase", "reason": "He is thrown to constantly."},
        {"slot": "RB", "player": "Bijan Robinson", "reason": "He plays every down."},
        {"slot": "QB", "player": "Josh Allen", "reason": "He runs as well as throws."},
        {"slot": "TE", "player": "Trey McBride", "reason": "He catches plenty."},
    ],
    "bench": [{"player": "Puka Nacua", "reason": "His team is not playing this week."}],
    "headline": "Start four, sit one.",
}


class SeasonClient:
    """The only three things the in-season jobs ask ESPN for."""

    def __init__(self, week=WEEK, roster=None, free_agents=None, fail: Exception | None = None):
        self.week = week
        self._roster = roster if roster is not None else ROSTER
        self._free_agents = free_agents or []
        self.fail = fail
        self.calls: list[str] = []

    def current_week(self):
        self.calls.append("current_week")
        return self.week

    def rosters(self):
        self.calls.append("rosters")
        if self.fail is not None:
            raise self.fail
        return [{"team_id": 6, **row} for row in self._roster]

    def free_agents(self, size=50, position=None):
        self.calls.append("free_agents")
        return list(self._free_agents)


def ready(tmp_path, results, *, roster=ROSTER, board=BOARD, week=WEEK):
    settings = make_settings(tmp_path)
    conn = open_db(tmp_path)
    seed_synced_league(conn)
    seed_roster(conn, roster, team_id=6)
    if board:
        seed_board(conn, board)
    return conn, settings, FakeRunner(settings, results), SeasonClient(week=week, roster=roster)


def advice_rows(conn):
    return conn.execute("SELECT * FROM advice ORDER BY id").fetchall()


# --- the bye week ------------------------------------------------------------


def test_a_starter_on_a_bye_is_flagged_in_a_headline_of_its_own(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)])

    summary = lineup_check.run(conn, settings, runner, client)

    rows = advice_rows(conn)
    alarms = [row for row in rows if "bye" in row["headline"].lower()]
    assert alarms, f"nothing in {[r['headline'] for r in rows]} warns about the bye"
    alarm = alarms[-1]
    assert "Ja'Marr Chase" in alarm["headline"]
    assert "zero" in (alarm["body"] or "").lower(), "she has to be told what it costs"
    assert "Ja'Marr Chase" in summary


def test_a_benched_player_on_a_bye_is_not_an_alarm(tmp_path):
    """Puka Nacua is on bye and on the bench. That is correct, not a problem."""
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)])

    lineup_check.run(conn, settings, runner, client)

    alarms = [row for row in advice_rows(conn) if "bye" in row["headline"].lower()]
    assert len(alarms) == 1
    assert "Puka Nacua" not in alarms[0]["headline"]


def test_the_bye_alarm_is_still_written_when_the_claude_call_fails(tmp_path):
    """The bye is arithmetic. It must not depend on a research call that timed out."""
    conn, settings, runner, client = ready(tmp_path, [failed_result(error="timed out")])

    with pytest.raises(JobFailed):
        lineup_check.run(conn, settings, runner, client)

    rows = advice_rows(conn)
    assert len(rows) == 1, "the alarm, and no half-finished lineup beside it"
    assert "Ja'Marr Chase" in rows[0]["headline"]
    assert rows[0]["kind"] == "lineup"


def test_a_failed_call_writes_no_lineup_advice(tmp_path):
    """No bye this week, so a failed call leaves nothing behind but the error."""
    healthy = [dict(row, bye_week=WEEK + 3) for row in BOARD]
    conn, settings, runner, client = ready(
        tmp_path, [failed_result(error="timed out")], board=healthy
    )

    with pytest.raises(JobFailed):
        lineup_check.run(conn, settings, runner, client)

    assert advice_rows(conn) == []


def test_the_model_saying_bye_is_enough_even_when_the_board_disagrees(tmp_path):
    """Fresher information wins, and a disagreement still raises the flag: the
    cost of checking is seconds and the cost of missing it is the week."""
    stale = [dict(row, bye_week=13) for row in BOARD]
    answer = dict(
        ANSWER,
        starters=[
            dict(ANSWER["starters"][0], bye_week=WEEK),
            *ANSWER["starters"][1:],
        ],
    )
    conn, settings, runner, client = ready(tmp_path, [ok_result(answer)], board=stale)

    lineup_check.run(conn, settings, runner, client)

    alarms = [row for row in advice_rows(conn) if "bye" in row["headline"].lower()]
    assert alarms and "Ja'Marr Chase" in alarms[0]["headline"]


def test_no_board_row_and_no_model_bye_means_no_false_alarm(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)], board=[])

    lineup_check.run(conn, settings, runner, client)

    assert not [row for row in advice_rows(conn) if "bye" in row["headline"].lower()]


def test_a_bye_is_matched_by_name_when_the_board_carries_a_research_id(tmp_path):
    """Board rows built before a sync carry synthetic negative ids, so the id
    join finds nothing and the name has to do the work."""
    unmatched = [dict(row, player_id=-1000 - index) for index, row in enumerate(BOARD, start=1)]
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)], board=unmatched)

    lineup_check.run(conn, settings, runner, client)

    alarms = [row for row in advice_rows(conn) if "bye" in row["headline"].lower()]
    assert alarms and "Ja'Marr Chase" in alarms[0]["headline"]


# --- the lineup itself -------------------------------------------------------


def test_a_successful_run_writes_one_lineup_advice_row_with_every_slot(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)])

    lineup_check.run(conn, settings, runner, client)

    lineup = [row for row in advice_rows(conn) if row["source_job"] == lineup_check.JOB_NAME]
    assert lineup
    row = lineup[0]
    assert row["kind"] == "lineup"
    payload = json.loads(row["payload_json"])
    assert [entry["player"] for entry in payload["starters"]] == [
        "Ja'Marr Chase",
        "Bijan Robinson",
        "Josh Allen",
        "Trey McBride",
    ]
    assert all(entry["reason"] for entry in payload["starters"])
    assert payload["week"] == WEEK


def test_the_prompt_carries_the_roster_and_the_week(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)])

    lineup_check.run(conn, settings, runner, client)

    prompt = runner.calls[0]["prompt"]
    assert "Ja'Marr Chase" in prompt
    assert str(WEEK) in prompt
    assert "point per catch" in prompt.lower() or "ppr" in prompt.lower()


def test_the_job_runs_with_web_tools_on(tmp_path):
    """An injury from Friday is not in any model's training data."""
    settings = make_settings(tmp_path)
    assert settings.job(lineup_check.JOB_NAME).tools, "the lineup check needs the live web"


def test_an_empty_roster_fails_loudly_rather_than_advising_about_nobody(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)], roster=[])

    with pytest.raises(JobFailed) as caught:
        lineup_check.run(conn, settings, runner, client)

    assert "roster" in str(caught.value).lower()
    assert runner.calls == [], "no point paying for a call about an empty roster"


def test_it_records_a_job_run_row_through_the_registry(tmp_path):
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)])

    outcome = run_job(lineup_check.JOB_NAME, conn, settings, runner, client)

    assert outcome.ok is True
    row = conn.execute(
        "SELECT * FROM job_runs WHERE job = ? ORDER BY id DESC LIMIT 1",
        (lineup_check.JOB_NAME,),
    ).fetchone()
    assert row["status"] == "ok"
    assert row["summary"] == outcome.summary


def test_an_espn_read_that_fails_still_produces_advice_from_the_stored_roster(tmp_path):
    """A stale roster beats no lineup at all on a Sunday morning."""
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)])
    client.fail = RuntimeError("ESPN said 401")

    summary = lineup_check.run(conn, settings, runner, client)

    assert summary
    assert advice_rows(conn)


def test_it_runs_with_no_espn_client_at_all(tmp_path):
    conn, settings, runner, _client = ready(tmp_path, [ok_result(ANSWER)])

    summary = lineup_check.run(conn, settings, runner, None)

    assert summary
    assert advice_rows(conn)


# --- saying so when it could not check --------------------------------------
#
# Both of these used to be silent, and silence here reads as "no byes this
# week" — which is exactly the sentence she must not be told wrongly.


def test_an_unknown_week_says_the_byes_were_not_checked(tmp_path):
    """No ESPN, nothing synced, and a model that did not name the week either.
    The job still gives a lineup; it must not imply the byes were checked."""
    answer = {key: value for key, value in ANSWER.items() if key != "week"}
    conn, settings, runner, _client = ready(tmp_path, [ok_result(answer)])

    summary = lineup_check.run(conn, settings, runner, None)

    assert "not check" in summary.lower() and "bye" in summary.lower(), summary
    card = advice_rows(conn)[0]
    assert "bye" in (card["body"] or "").lower()
    assert "could not work out which" in (card["body"] or "").lower()


def test_a_starter_with_no_bye_on_file_is_named_rather_than_skipped(tmp_path):
    """A player picked up off waivers in October was never on the board, so
    hal-mary has no bye week for him. Skipping him quietly is the failure."""
    partial = [row for row in BOARD if row["name"] != "Trey McBride"]
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)], board=partial)

    summary = lineup_check.run(conn, settings, runner, client)

    card = advice_rows(conn)[0]
    assert "Trey McBride" in card["body"]
    assert "no bye week on file" in card["body"].lower()
    assert "Trey McBride" in summary


def test_a_starter_the_model_gave_a_bye_for_counts_as_checked(tmp_path):
    """The board not knowing him is fine when this morning's research did."""
    partial = [row for row in BOARD if row["name"] != "Trey McBride"]
    answer = dict(
        ANSWER,
        starters=[
            *ANSWER["starters"][:3],
            dict(ANSWER["starters"][3], bye_week=11),
        ],
    )
    conn, settings, runner, client = ready(tmp_path, [ok_result(answer)], board=partial)

    lineup_check.run(conn, settings, runner, client)

    assert "no bye week on file" not in advice_rows(conn)[0]["body"].lower()


def test_a_clean_week_says_the_byes_were_checked_and_were_fine(tmp_path):
    """"Nothing to do" has to be distinguishable from "nothing was looked at"."""
    healthy = [dict(row, bye_week=WEEK + 3) for row in BOARD]
    conn, settings, runner, client = ready(tmp_path, [ok_result(ANSWER)], board=healthy)

    summary = lineup_check.run(conn, settings, runner, client)

    body = advice_rows(conn)[0]["body"].lower()
    assert "nobody in your lineup is on a bye" in body
    assert "not check" not in summary.lower()
