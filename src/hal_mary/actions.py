"""The plan hal-mary hands to Claude Cowork, and the record of what came back.

hal-mary decides, Cowork executes, Cowork reports back. Cowork is a browser with
no context and no discretion: it receives an ordered list of concrete
instructions — this player, this slot, this click — performs exactly those, and
says what happened. That is a security boundary. Cowork's browser reads league
pages carrying five other people's team names and message-board text, which is
the classic prompt-injection surface, and an executor with nothing to choose
gives injected text nothing to redirect.

This module is the store behind that list. Four things it has to be exact about:

**What is still pending.** An action reported ``done`` is never returned again.
Without that, hal-mary re-issues the same instruction forever and Cowork performs
it on every run.

**What order it goes in.** ``sequence`` is not cosmetic. A roster has a fixed
size, so adding usually implies dropping, and the wrong order loses a player for
nothing.

**What has gone stale.** A lineup change is worthless once that player's game has
kicked off, and lineups lock per player rather than on one weekly deadline — so
``deadline`` belongs to the action, and an action past it is ``expired``, not
pending.

**What must not be attempted.** ``depends_on`` names the actions that had to
report ``done`` first. A dependency that failed makes its dependent skippable;
it stays on the list so Cowork reports the skip and hal-mary learns, rather than
Cowork improvising.

Emission is idempotent. A job that runs twice, or two jobs that reach the same
conclusion, must not put the same click in front of Cowork twice — see
:func:`emit`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from . import db
from .draft.board import normalize_name

__all__ = [
    "KINDS",
    "OUTCOMES",
    "STATUSES",
    "WEEKDAYS",
    "Action",
    "depends_on_ids",
    "emit",
    "end_of_nfl_week",
    "expire_stale",
    "find_equivalent",
    "pending",
    "report",
    "unmet_dependencies",
]

#: The clicks Cowork knows how to make. Anything else is a decision, and
#: decisions do not leave this process.
KINDS = ("bench", "start", "claim", "drop")

#: What Cowork may report. ``expired`` is not here: only hal-mary decides that an
#: instruction went stale, because only hal-mary knows what the deadline meant.
OUTCOMES = ("done", "failed", "skipped")

STATUSES = ("pending", *OUTCOMES, "expired")

#: Statuses that make a further emission of the same action a duplicate.
#: ``failed`` and ``skipped`` are deliberately absent — those are hal-mary's to
#: retry — and so is ``expired``, which means the move is worth re-deciding.
_BLOCKING_STATUSES = ("pending", "done")

#: ``datetime.weekday()`` order, for reading the configured week boundary.
WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

_COLUMNS = (
    "id, created_at, kind, player_name, player_id, slot, paired_player_name, reason, "
    "sequence, depends_on, deadline, reversible, source_job, status, outcome_detail, "
    "reported_at"
)


@dataclass(frozen=True)
class Action:
    """One instruction, concrete enough that there is nothing left to decide.

    ``player_name`` is spelled exactly as ESPN spells it: Cowork finds the player
    by reading that string off the page, so "Marvin Harrison" where ESPN says
    "Marvin Harrison Jr." is an instruction it cannot carry out.

    ``reason`` is one sentence written for a person. It is what Bryan reads in
    the log after an unattended action, so it is required and may not be blank.
    It is never reasoning for Cowork to interpret.

    ``paired_player_name`` is the other player in a two-player move: who takes
    the slot on a ``bench``, who leaves it on a ``start``, who is dropped on a
    ``claim``.

    ``deadline`` is the wall-clock time after which the action is pointless or
    harmful, ISO-8601; ``None`` means it does not go stale on a clock.

    ``reversible`` is whether hal-mary can undo it. Drops are not.
    """

    kind: str
    player_name: str
    reason: str
    slot: str | None = None
    player_id: int | None = None
    paired_player_name: str | None = None
    sequence: int = 0
    depends_on: Sequence[int] = field(default_factory=tuple)
    deadline: str | None = None
    reversible: bool = True
    source_job: str | None = None


def _moment(value: datetime | str | None) -> str:
    """Normalise a caller's clock to the one timestamp format the table holds."""
    if value is None:
        return db.utc_now()
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat(timespec="seconds")
    return value


def end_of_nfl_week(now: datetime | str | None, settings: Any) -> str:
    """The moment the NFL week containing ``now`` rolls over, ISO-8601 UTC.

    The one clock two separate things hang off, which is why it is here rather
    than in the job that emits actions.

    **It is every action's deadline.** A lineup change is worthless once the week
    it was reasoned about is over. Without one, an instruction emitted in week 5
    is still pending in week 7 and an executor that was offline in between comes
    back and benches a healthy starter — and because ``expire_stale`` only touches
    rows that have a deadline, nothing would ever revoke it either.

    **It is also the equivalence window.** "The same bench, already queued this
    week" is a duplicate; the same bench next week is a different decision about
    a different situation.

    Boundaries are exclusive at the start and inclusive at the end: a moment
    exactly on the rollover still belongs to the week that is closing, so an
    action emitted at 10:59 gets 11:00 rather than a week and a minute.
    """
    moment = datetime.fromisoformat(_moment(now))
    weekday = getattr(settings.actions, "week_boundary_weekday", "tuesday").strip().lower()
    try:
        target = WEEKDAYS.index(weekday)
    except ValueError:
        raise ValueError(
            f"[actions].week_boundary_weekday is {weekday!r}; expected a weekday name"
        ) from None
    hour = int(getattr(settings.actions, "week_boundary_hour_utc", 11))

    candidate = moment.replace(hour=hour, minute=0, second=0, microsecond=0)
    candidate += timedelta(days=(target - moment.weekday()) % 7)
    if candidate < moment:
        candidate += timedelta(days=7)
    return candidate.astimezone(UTC).isoformat(timespec="seconds")


def _week_started(boundary: str) -> str:
    """When the week ending at ``boundary`` began."""
    return (datetime.fromisoformat(boundary) - timedelta(days=7)).isoformat(timespec="seconds")


def _validate(action: Action) -> tuple[str, str]:
    """Check the fields that make an instruction performable; return the two."""
    if action.kind not in KINDS:
        raise ValueError(f"unknown action kind {action.kind!r}; expected one of {', '.join(KINDS)}")
    player_name = (action.player_name or "").strip()
    if not player_name:
        raise ValueError("an action needs a player name spelled as ESPN spells it")
    reason = (action.reason or "").strip()
    if not reason:
        # A blank reason is always a bug upstream, and this is the sentence Bryan
        # reads in the log when he finds out a drop happened. Refuse at the door.
        raise ValueError(f"action for {player_name!r} has no reason; one sentence is required")
    return player_name, reason


def _equivalent_id(
    conn: sqlite3.Connection, action: Action, player_name: str, now: str, cutoff: str
) -> int | None:
    """The id of an equivalent live action, or None.

    Equivalent means: same ``kind``, same player, same ``slot``, still pending or
    already done, and emitted since ``cutoff`` — the start of the NFL week
    containing ``now``, not a rolling seven days. The distinction is not
    academic: benching a player who is on bye in one week and benching the same
    player for an injury in the next are different decisions five days apart, and
    a rolling window would swallow the second one silently.

    Names are compared through :func:`~hal_mary.draft.board.normalize_name` so
    ``Ja'Marr Chase`` from one job and ``JaMarr Chase`` from another are one
    action, not two clicks.
    """
    key = normalize_name(player_name)
    placeholders = ", ".join("?" * len(_BLOCKING_STATUSES))
    rows = conn.execute(
        f"""
        SELECT id, player_name FROM actions
         WHERE kind = ?
           AND slot IS ?
           AND status IN ({placeholders})
           AND created_at >= ?
         ORDER BY id
        """,
        (action.kind, action.slot, *_BLOCKING_STATUSES, cutoff),
    ).fetchall()
    for row in rows:
        if normalize_name(row["player_name"]) == key:
            return int(row["id"])
    return None


def find_equivalent(
    conn: sqlite3.Connection,
    action: Action,
    *,
    now: datetime | str | None = None,
    settings: Any = None,
) -> int | None:
    """The id of an equivalent action already queued or done this week, or None.

    Public because :func:`emit` returning an id says nothing about whether it
    wrote one, and a caller that cannot tell "already queued" from "newly
    decided" cannot report honestly about what a run did. Ask this first when the
    difference matters.

    Without ``settings`` the week is bounded by the built-in default boundary,
    which is what a caller doing a bare existence check wants.
    """
    player_name, _ = _validate(action)
    stamp = _moment(now)
    return _equivalent_id(conn, action, player_name, stamp, _cutoff(stamp, settings))


def _cutoff(now: str, settings: Any) -> str:
    """The start of the NFL week containing ``now``."""
    return _week_started(end_of_nfl_week(now, settings or _DEFAULT_SETTINGS))


class _DefaultActionsConfig:
    """The built-in week boundary, for a caller with no ``Settings`` in hand.

    Matches ``ActionsConfig``'s defaults. It exists so ``emit`` can be called
    without threading configuration through every intermediate — the boundary
    only ever *narrows* what counts as a duplicate, so a caller that does not
    supply one still gets a week-aligned window rather than a rolling one.
    """

    week_boundary_weekday = "tuesday"
    week_boundary_hour_utc = 11


class _DefaultSettings:
    actions = _DefaultActionsConfig()


_DEFAULT_SETTINGS = _DefaultSettings()


def emit(
    conn: sqlite3.Connection,
    action: Action,
    *,
    now: datetime | str | None = None,
    settings: Any = None,
) -> int:
    """Add ``action`` to the plan, or return the id of the one already there.

    **Idempotency is the point.** Jobs run on a schedule and reach the same
    conclusion twice; a duplicate in the plan is Cowork performing the same click
    twice, and for a ``drop`` that is unrecoverable. So an action equivalent to
    one already pending or done is not written, and the existing id comes back —
    which means a caller can still hang a ``depends_on`` off it.

    Equivalence is scoped to the NFL week containing ``now`` — see
    :func:`end_of_nfl_week`. The same bench twice on a Sunday is one click; the
    same bench next Saturday is a new decision about a new situation.

    Nothing here decides. Whether an action should exist is the calling job's
    judgement; this only refuses to write one twice.
    """
    player_name, reason = _validate(action)
    stamp = _moment(now)

    existing = _equivalent_id(conn, action, player_name, stamp, _cutoff(stamp, settings))
    if existing is not None:
        return existing

    depends_on = [int(value) for value in action.depends_on]
    cur = conn.execute(
        """
        INSERT INTO actions
            (created_at, kind, player_name, player_id, slot, paired_player_name, reason,
             sequence, depends_on, deadline, reversible, source_job, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """,
        (
            stamp,
            action.kind,
            player_name,
            action.player_id,
            action.slot,
            action.paired_player_name,
            reason,
            int(action.sequence),
            json.dumps(depends_on) if depends_on else None,
            action.deadline,
            1 if action.reversible else 0,
            action.source_job,
        ),
    )
    action_id = cur.lastrowid
    if action_id is None:  # pragma: no cover - sqlite always reports a rowid here
        raise RuntimeError("sqlite did not report a row id for the new actions row")
    return int(action_id)


def pending(conn: sqlite3.Connection, *, now: datetime | str | None = None) -> list[sqlite3.Row]:
    """The plan, in the order it must be performed.

    Pending, deadline not passed, ordered by ``sequence`` then ``id`` — a total
    order, so two reads of an unchanged table agree and Cowork never sees the
    same plan in a different order.

    An empty list is the normal case and is one indexed lookup.
    """
    stamp = _moment(now)
    return list(
        conn.execute(
            f"""
            SELECT {_COLUMNS} FROM actions
             WHERE status = 'pending'
               AND (deadline IS NULL OR deadline > ?)
             ORDER BY sequence, id
            """,
            (stamp,),
        )
    )


def depends_on_ids(row: sqlite3.Row) -> list[int]:
    """The ``depends_on`` column as a list of ids; ``[]`` when it is empty."""
    raw = row["depends_on"]
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):  # pragma: no cover - only a hand-edited row
        return []
    return [int(value) for value in parsed] if isinstance(parsed, list) else []


def unmet_dependencies(conn: sqlite3.Connection, action_id: int) -> list[int]:
    """Dependencies of ``action_id`` that have not reported ``done``, in order.

    A non-empty answer means the action must be skipped rather than attempted:
    "drop then add" with a failed drop leaves the roster in a state worse than
    doing nothing. Cowork reports the skip; hal-mary decides what happens next.
    """
    row = conn.execute("SELECT depends_on FROM actions WHERE id = ?", (action_id,)).fetchone()
    if row is None:
        raise LookupError(f"no actions row with id {action_id}")
    required = depends_on_ids(row)
    if not required:
        return []
    placeholders = ", ".join("?" * len(required))
    done = {
        int(found["id"])
        for found in conn.execute(
            f"SELECT id FROM actions WHERE id IN ({placeholders}) AND status = 'done'",
            required,
        )
    }
    return [value for value in required if value not in done]


def report(
    conn: sqlite3.Connection,
    action_id: int,
    outcome: str,
    detail: str | None = None,
    *,
    now: datetime | str | None = None,
) -> None:
    """Close the loop on one action.

    ``outcome`` is ``done``, ``failed`` or ``skipped``. ``detail`` is what the
    browser saw, in Cowork's own words — data, never an instruction, and never
    interpolated into a later prompt as one.

    Reporting is what stops an instruction being re-issued, so an unknown id is a
    ``LookupError`` rather than a silent no-op: an action nobody can close is one
    Cowork performs on every run for the rest of the season.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}; expected one of {', '.join(OUTCOMES)}")
    cur = conn.execute(
        "UPDATE actions SET status = ?, outcome_detail = ?, reported_at = ? WHERE id = ?",
        (outcome, detail, _moment(now), action_id),
    )
    if cur.rowcount == 0:
        raise LookupError(f"no actions row with id {action_id}")


def expire_stale(conn: sqlite3.Connection, now: datetime | str | None = None) -> int:
    """Mark every pending action whose deadline has passed ``expired``.

    Returns how many were expired, so a second call over an unchanged table
    returns 0. :func:`pending` already hides them; this makes the reason visible
    on the row rather than leaving something that looks issued but never was.
    """
    stamp = _moment(now)
    cur = conn.execute(
        """
        UPDATE actions
           SET status = 'expired', reported_at = ?
         WHERE status = 'pending' AND deadline IS NOT NULL AND deadline <= ?
        """,
        (stamp, stamp),
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
