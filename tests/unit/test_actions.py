"""Tests for the actions store: the ordered plan hal-mary hands to Cowork.

Everything here is about a promise made to an executor that has no judgement.
Cowork performs what it is given, in the order it is given, and reports back. So
the store has to be exact about four things: what is still pending, what order it
goes in, what has already been done, and what has gone stale. Getting any of them
wrong means a browser performing a move that hal-mary did not intend *now* — and
one of the kinds this table carries is `drop`, which cannot be undone.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import FIXTURE_ENV
from hal_mary import actions, db
from hal_mary.config import load_settings


@pytest.fixture
def settings():
    return load_settings(env=FIXTURE_ENV)


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "hal.db")
    db.migrate(connection)
    yield connection
    connection.close()


def bench(**overrides) -> actions.Action:
    """A bye-week bench, the first action hal-mary ever emits."""
    fields = {
        "kind": "bench",
        "player_name": "Bijan Robinson",
        "slot": "RB",
        "paired_player_name": "Rhamondre Stevenson",
        "reason": "Atlanta is on bye in week 5, so he scores nothing if he starts.",
        "sequence": 1,
        "reversible": True,
        "source_job": "lineup_actions",
    }
    fields.update(overrides)
    return actions.Action(**fields)


def statuses(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (row["player_name"], row["status"])
        for row in conn.execute("SELECT player_name, status FROM actions ORDER BY id")
    ]


# --- emit --------------------------------------------------------------------


def test_emit_stores_every_field_the_plan_carries(conn: sqlite3.Connection):
    action_id = actions.emit(conn, bench(depends_on=(), deadline="2026-10-05T17:00:00+00:00"))

    row = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
    assert row["kind"] == "bench"
    assert row["player_name"] == "Bijan Robinson"
    assert row["slot"] == "RB"
    assert row["paired_player_name"] == "Rhamondre Stevenson"
    assert row["reason"].startswith("Atlanta is on bye")
    assert row["sequence"] == 1
    assert row["deadline"] == "2026-10-05T17:00:00+00:00"
    assert row["reversible"] == 1
    assert row["source_job"] == "lineup_actions"
    assert row["status"] == "pending"
    assert row["outcome_detail"] is None
    assert row["reported_at"] is None
    assert row["created_at"]


def test_emit_stores_depends_on_as_a_json_array(conn: sqlite3.Connection):
    first = actions.emit(conn, bench(player_name="Puka Nacua", slot="WR"))
    second = actions.emit(conn, bench(player_name="Jaylen Waddle", depends_on=(first,)))

    row = conn.execute("SELECT depends_on FROM actions WHERE id = ?", (second,)).fetchone()
    assert json.loads(row["depends_on"]) == [first]


def test_emit_rejects_a_kind_cowork_has_no_click_for(conn: sqlite3.Connection):
    with pytest.raises(ValueError) as excinfo:
        actions.emit(conn, bench(kind="trade"))
    assert "trade" in str(excinfo.value)


def test_emit_rejects_an_action_with_no_reason(conn: sqlite3.Connection):
    """The reason is what Bryan reads in the log after an unattended drop."""
    with pytest.raises(ValueError):
        actions.emit(conn, bench(reason="   "))


def test_emit_rejects_a_player_nobody_can_click_on(conn: sqlite3.Connection):
    with pytest.raises(ValueError):
        actions.emit(conn, bench(player_name=""))


# --- pending -----------------------------------------------------------------


def test_pending_returns_the_plan_in_sequence_order(conn: sqlite3.Connection):
    actions.emit(conn, bench(player_name="Third", sequence=3))
    actions.emit(conn, bench(player_name="First", sequence=1))
    actions.emit(conn, bench(player_name="Second", sequence=2))

    assert [row["player_name"] for row in actions.pending(conn)] == ["First", "Second", "Third"]


def test_pending_breaks_a_sequence_tie_by_id_so_two_reads_agree(conn: sqlite3.Connection):
    actions.emit(conn, bench(player_name="Earlier", sequence=1))
    actions.emit(conn, bench(player_name="Later", sequence=1))

    once = [row["id"] for row in actions.pending(conn)]
    twice = [row["id"] for row in actions.pending(conn)]
    assert once == twice
    assert [row["player_name"] for row in actions.pending(conn)] == ["Earlier", "Later"]


def test_pending_is_empty_on_a_fresh_database(conn: sqlite3.Connection):
    """The normal case. Most runs have nothing to do."""
    assert actions.pending(conn) == []


def test_a_done_action_is_never_returned_again(conn: sqlite3.Connection):
    action_id = actions.emit(conn, bench())
    actions.report(conn, action_id, "done", "Moved to bench.")

    assert actions.pending(conn) == []


def test_a_failed_action_is_not_reissued_either(conn: sqlite3.Connection):
    """hal-mary decides what to do about a failure, not Cowork on its next run."""
    action_id = actions.emit(conn, bench())
    actions.report(conn, action_id, "failed", "The lineup was locked.")

    assert actions.pending(conn) == []


# --- deadlines ---------------------------------------------------------------


def test_an_action_past_its_deadline_is_not_pending(conn: sqlite3.Connection):
    now = datetime(2026, 10, 5, 17, 0, tzinfo=UTC)
    actions.emit(conn, bench(player_name="Kicked off", deadline="2026-10-05T16:59:00+00:00"))
    actions.emit(conn, bench(player_name="Still playable", deadline="2026-10-05T20:00:00+00:00"))

    assert [row["player_name"] for row in actions.pending(conn, now=now)] == ["Still playable"]


def test_expire_stale_marks_the_row_expired_rather_than_leaving_it_pending(
    conn: sqlite3.Connection,
):
    now = datetime(2026, 10, 5, 17, 0, tzinfo=UTC)
    actions.emit(conn, bench(player_name="Kicked off", deadline="2026-10-05T16:59:00+00:00"))
    actions.emit(conn, bench(player_name="Still playable", deadline="2026-10-05T20:00:00+00:00"))
    actions.emit(conn, bench(player_name="No deadline", deadline=None))

    assert actions.expire_stale(conn, now) == 1
    assert statuses(conn) == [
        ("Kicked off", "expired"),
        ("Still playable", "pending"),
        ("No deadline", "pending"),
    ]
    # Running it again changes nothing: expiry is not a repeated write.
    assert actions.expire_stale(conn, now) == 0


def test_expire_stale_records_when_it_happened(conn: sqlite3.Connection):
    now = datetime(2026, 10, 5, 17, 0, tzinfo=UTC)
    action_id = actions.emit(conn, bench(deadline="2026-10-05T16:00:00+00:00"))
    actions.expire_stale(conn, now)

    row = conn.execute("SELECT reported_at FROM actions WHERE id = ?", (action_id,)).fetchone()
    assert row["reported_at"] == "2026-10-05T17:00:00+00:00"


# --- idempotency -------------------------------------------------------------


def test_emitting_the_same_action_twice_is_a_no_op(conn: sqlite3.Connection):
    first = actions.emit(conn, bench())
    second = actions.emit(conn, bench())

    assert first == second
    assert conn.execute("SELECT count(*) FROM actions").fetchone()[0] == 1


def test_a_duplicate_of_a_done_action_is_not_re_emitted(conn: sqlite3.Connection):
    """Cowork already benched him. Emitting it again would have it click twice."""
    first = actions.emit(conn, bench())
    actions.report(conn, first, "done", "Moved to bench.")

    assert actions.emit(conn, bench()) == first
    assert conn.execute("SELECT count(*) FROM actions").fetchone()[0] == 1
    assert actions.pending(conn) == []


def test_equivalence_ignores_how_the_name_is_punctuated(conn: sqlite3.Connection):
    first = actions.emit(conn, bench(player_name="Ja'Marr Chase", slot="WR"))
    second = actions.emit(conn, bench(player_name="JaMarr Chase", slot="WR"))

    assert first == second


def test_the_same_player_at_a_different_slot_is_a_different_action(conn: sqlite3.Connection):
    first = actions.emit(conn, bench(player_name="Puka Nacua", slot="WR"))
    second = actions.emit(conn, bench(player_name="Puka Nacua", slot="RB/WR/TE"))

    assert first != second


def test_a_different_kind_for_the_same_player_is_a_different_action(conn: sqlite3.Connection):
    first = actions.emit(conn, bench(player_name="Puka Nacua", slot="WR"))
    second = actions.emit(conn, bench(player_name="Puka Nacua", slot="WR", kind="start"))

    assert first != second


def test_a_failed_action_may_be_emitted_again(conn: sqlite3.Connection):
    """Only pending and done block a duplicate. A failure is hal-mary's to retry."""
    first = actions.emit(conn, bench())
    actions.report(conn, first, "failed", "ESPN would not load.")

    second = actions.emit(conn, bench())
    assert second != first


def test_the_same_bench_next_month_is_a_new_action(conn: sqlite3.Connection):
    """Equivalence is scoped to the current week; a later bye is its own move."""
    then = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    later = then + timedelta(days=35)
    first = actions.emit(conn, bench(), now=then)
    second = actions.emit(conn, bench(), now=later)

    assert first != second


# --- dependencies ------------------------------------------------------------


def test_a_dependency_that_failed_makes_its_dependent_skippable(conn: sqlite3.Connection):
    claim = actions.emit(
        conn,
        actions.Action(
            kind="claim",
            player_name="Tank Bigsby",
            reason="He is the best back on waivers and she has an open bench slot.",
            sequence=1,
            reversible=False,
        ),
    )
    dependent = actions.emit(
        conn,
        actions.Action(
            kind="start",
            player_name="Tank Bigsby",
            slot="RB",
            paired_player_name="Zach Charbonnet",
            reason="Start him once the claim lands.",
            sequence=2,
            depends_on=(claim,),
        ),
    )

    actions.report(conn, claim, "failed", "The claim window had closed.")
    assert actions.unmet_dependencies(conn, dependent) == [claim]
    # It stays on the list: Cowork skips it and reports that, so hal-mary learns.
    assert [row["id"] for row in actions.pending(conn)] == [dependent]


def test_a_dependency_that_is_done_unblocks_its_dependent(conn: sqlite3.Connection):
    first = actions.emit(conn, bench(player_name="Puka Nacua", slot="WR", sequence=1))
    second = actions.emit(conn, bench(player_name="Jaylen Waddle", sequence=2, depends_on=(first,)))

    actions.report(conn, first, "done", "Benched.")
    assert actions.unmet_dependencies(conn, second) == []


def test_a_dependency_still_pending_is_unmet(conn: sqlite3.Connection):
    first = actions.emit(conn, bench(player_name="Puka Nacua", slot="WR", sequence=1))
    second = actions.emit(conn, bench(player_name="Jaylen Waddle", sequence=2, depends_on=(first,)))

    assert actions.unmet_dependencies(conn, second) == [first]


# --- report ------------------------------------------------------------------


def test_report_records_the_outcome_and_when_it_landed(conn: sqlite3.Connection):
    action_id = actions.emit(conn, bench())
    actions.report(conn, action_id, "done", "Bench slot 3.")

    row = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
    assert row["status"] == "done"
    assert row["outcome_detail"] == "Bench slot 3."
    assert row["reported_at"]


def test_report_refuses_an_outcome_that_is_not_one_of_the_three(conn: sqlite3.Connection):
    action_id = actions.emit(conn, bench())
    with pytest.raises(ValueError) as excinfo:
        actions.report(conn, action_id, "maybe", None)
    assert "maybe" in str(excinfo.value)


def test_report_on_an_unknown_id_is_a_lookup_error(conn: sqlite3.Connection):
    with pytest.raises(LookupError):
        actions.report(conn, 4242, "done", None)


# --- the NFL week boundary ---------------------------------------------------
#
# Two different bugs are fixed by the same clock, so they are tested together.
#
# The first is that an instruction with no deadline never goes stale. Cowork's
# Sunday task does not run in week 5 because Claude Desktop was closed; week 7
# arrives, `pending_actions` still says bench Bijan Robinson, the bye is three
# weeks gone, and a healthy starter is benched unattended. The design's line —
# "a stale instruction executed three days late is worse than none" — is inert
# unless something actually sets a deadline.
#
# The second is that "within the current week" was implemented as a rolling
# seven days, so a genuinely new decision made five days later was silently
# swallowed and the caller could not tell.


def test_the_week_boundary_is_the_next_rollover_after_now(settings):
    # A Sunday morning, mid-season.
    sunday = datetime(2026, 10, 4, 10, 30, tzinfo=UTC)
    assert actions.end_of_nfl_week(sunday, settings) == "2026-10-06T11:00:00+00:00"


def test_a_moment_just_before_the_rollover_still_belongs_to_the_old_week(settings):
    late_monday = datetime(2026, 10, 6, 10, 59, tzinfo=UTC)
    assert actions.end_of_nfl_week(late_monday, settings) == "2026-10-06T11:00:00+00:00"


def test_a_moment_just_after_the_rollover_belongs_to_the_new_one(settings):
    tuesday = datetime(2026, 10, 6, 11, 1, tzinfo=UTC)
    assert actions.end_of_nfl_week(tuesday, settings) == "2026-10-13T11:00:00+00:00"


def test_an_action_deadlined_to_the_week_stops_being_pending_when_the_week_ends(
    conn: sqlite3.Connection, settings
):
    """The concrete failure: a bye-week bench still being issued two weeks later."""
    emitted = datetime(2026, 10, 4, 10, 30, tzinfo=UTC)
    actions.emit(
        conn,
        bench(deadline=actions.end_of_nfl_week(emitted, settings)),
        now=emitted,
    )

    still_this_week = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    assert len(actions.pending(conn, now=still_this_week)) == 1

    two_weeks_later = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    assert actions.pending(conn, now=two_weeks_later) == []
    assert actions.expire_stale(conn, two_weeks_later) == 1
    assert statuses(conn) == [("Bijan Robinson", "expired")]


# --- equivalence is scoped to the week, not to a rolling window ---------------


def test_the_same_move_twice_in_one_week_is_one_action(conn: sqlite3.Connection):
    tuesday = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    friday = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

    first = actions.emit(conn, bench(), now=tuesday)
    second = actions.emit(conn, bench(), now=friday)
    assert first == second


def test_the_same_move_in_the_next_week_is_a_new_action(conn: sqlite3.Connection):
    """Five days apart but a different week: on bye then, injured now."""
    monday = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    saturday = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

    first = actions.emit(conn, bench(), now=monday)
    actions.report(conn, first, "done", "Benched.", now=monday)

    second = actions.emit(conn, bench(), now=saturday)
    assert second != first
    assert [row["id"] for row in actions.pending(conn, now=saturday)] == [second]


def test_a_caller_can_tell_already_queued_from_newly_decided(conn: sqlite3.Connection):
    """`emit` returning an id says nothing about whether it wrote one."""
    assert actions.find_equivalent(conn, bench()) is None

    first = actions.emit(conn, bench())
    assert actions.find_equivalent(conn, bench()) == first
