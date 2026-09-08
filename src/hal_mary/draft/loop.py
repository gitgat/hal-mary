"""The draft loop: the thing that is actually running on draft night.

For three hours it polls ESPN every ``draft.poll_seconds``, marks the players who
are gone, and calls the advisor when Caroline's turn is close. Everything about
it is shaped by the fact that nobody is watching it and it cannot be restarted
mid-turn.

Four rules, each of which exists because breaking it is a specific, expensive
failure:

* **Warm the client before polling.** ``EspnClient.draft_picks()`` only builds
  its player-name map when the pick list is non-empty, so an unwarmed client does
  its slow full-league fetch on the very poll that first sees a pick — mid-draft,
  on the clock, which is the worst possible moment for it.
* **Advise at most once per upcoming pick.** A five-second poll and a naive
  "``picks_until_mine <= 2``" test runs the advisor a dozen times per turn:
  money burnt, and a page flooded with cards that contradict each other. The loop
  remembers which pick it last advised on.
* **Nothing escapes.** An ESPN failure is logged and the poll returns; the loop
  lives. An exception that kills the loop mid-draft has no recovery, because the
  person who would restart it is in a draft room on her phone.
* **Deduplicate before ``apply_picks``.** Two reports of one player in a single
  call leave the second one unmatched forever, because ``apply_picks`` refuses to
  claim a board row twice. That belongs here, upstream, not in the pure
  arithmetic.
* **One tick has one time budget, and it starts before the ESPN read.** The pick
  clock is 90 seconds. A slow sync followed by two Claude attempts that time out
  (each costing its ``timeout_s`` *plus* the runner's kill-and-join teardown)
  would otherwise overrun it, and the card would arrive after the pick was made.
  ``draft.advice_budget_s`` is fixed at the top of the tick, so a slow sync spends
  it like anything else, and the advisor starts an attempt only if it can finish
  inside what is left — a slow sync costs an attempt, never the card. What that
  does *not* do is bound the ESPN reads themselves: a tick makes up to three
  (:meth:`warm`, ``sync_draft``, :meth:`_read_schedule`), the budget cannot cancel
  a request already in flight, and httpx times out per operation rather than per
  request. So the honest bound on a tick is ``max(advice_budget_s, ESPN spend)``.
  The lever for the second half is ``espn.read_timeout_s``.

**Threading.** ``run_once`` does blocking SQLite and subprocess work on the
calling event loop, deliberately: ``db.connect`` leaves ``check_same_thread`` on,
so the connection can only be touched by the thread that opened it, and hopping
threads to keep the loop responsive is exactly how that becomes an intermittent
``ProgrammingError``. Run the draft loop on its own thread with its own
connection, and let the event bus carry its events across to the web loop — which
is what ``EventBus.publish`` is built for.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from typing import Any

from hal_mary import db
from hal_mary.config import Settings
from hal_mary.draft import store
from hal_mary.draft.advisor import advise
from hal_mary.draft.board import apply_picks, normalize_name
from hal_mary.espn.sync import sync_draft
from hal_mary.league import LeagueUnknown, load_league_context

__all__ = [
    "PHASE_DONE",
    "PHASE_IDLE",
    "PHASE_LIVE",
    "DraftLoop",
    "apply_new_picks",
    "cadence_words",
    "describe_pick",
    "draft_phase",
    "pending_picks",
    "record_manual_pick",
]

log = logging.getLogger(__name__)

#: Nothing has been drafted. The draft is one evening a year, so the loop only
#: has to notice one opening — ``draft.idle_poll_seconds``.
PHASE_IDLE = "idle"

#: A draft is running. ``draft.poll_seconds``, which is what a 90-second pick
#: clock needs and is unchanged from what draft night has always used.
PHASE_LIVE = "live"

#: Every slot on ESPN's board has a real player in it. The loop stops.
PHASE_DONE = "done"


def draft_phase(*, picks_made: int, total_slots: int | None) -> str:
    """Which of the three phases the draft is in, from the board alone.

    **The board decides, and only the board.** ``draftDetail`` also carries
    ``inProgress`` and ``drafted``, and :meth:`EspnClient.draft_status` reports
    both — but neither is an argument here, deliberately, because neither can be
    believed in the direction it would be used:

    * ``drafted`` **cannot stop the loop.** ``docs/DECISIONS.md`` records that
      ESPN may only set it once a draft is over, which is why ``draft_picks``
      reads the raw endpoint and ignores it. A detector that stopped polling
      because the flag said so would go quiet mid-draft — the one direction in
      which being wrong costs Caroline picks. It is corroboration and a log line
      (:meth:`DraftLoop._note_flag_disagreement`), never a control input.
    * ``inProgress`` **cannot start the fast clock.** ESPN pre-populates all 96
      slots from the day the league exists and answers this call about the draft
      *lobby*, not about picks; a loop that trusted it would poll every five
      seconds for months, which is the bug the phases exist to remove.

    What is left is arithmetic over facts: a slot with a real player in it is a
    pick that happened (``EspnClient.pick_is_made``), and a board whose every
    slot is filled is a draft with nothing left to watch.

    ``total_slots`` is ESPN's own row count, or the league's ``rounds x teams``
    when there is no ESPN. ``None`` or zero means nobody knows how long the draft
    is, and an unknowable end is never treated as a finished one.
    """
    if total_slots and picks_made >= total_slots:
        return PHASE_DONE
    if picks_made > 0:
        return PHASE_LIVE
    return PHASE_IDLE


def describe_pick(pick: dict[str, Any]) -> str:
    """How a pick is named on the page and in the log.

    A pick whose player ESPN could not name is shown by its id rather than left
    blank: "player 4262921 is gone" is still information, and a draft loop that
    raised because the name list was briefly unavailable would be a much worse
    failure than an ugly label.
    """
    name = pick.get("player_name") or pick.get("name")
    if name:
        return str(name)
    player_id = pick.get("player_id")
    return f"player {player_id}" if player_id is not None else "an unknown player"


# --- applying picks ----------------------------------------------------------


def _pick_keys(pick: dict[str, Any]) -> set[tuple[str, Any]]:
    """Everything that identifies the *player* in a pick or a board row.

    Two keys per name — suffix-preserving and suffix-stripped — for the same
    reason ``apply_picks`` matches in those two stages: it is what separates
    Michael Carter from Michael Carter II while still joining "Marvin Harrison
    Jr." to "Marvin Harrison".
    """
    keys: set[tuple[str, Any]] = set()
    player_id = pick.get("player_id")
    if player_id is not None:
        keys.add(("id", player_id))
    name = pick.get("player_name") or pick.get("name")
    if name:
        keys.add(("tight", normalize_name(str(name), strip_suffix=False)))
        keys.add(("loose", normalize_name(str(name))))
    return keys


def _claimed_keys(board: list[dict[str, Any]]) -> set[tuple[str, Any]]:
    keys: set[tuple[str, Any]] = set()
    for entry in board:
        if entry.get("drafted") or (entry.get("drafted_by_team_id") is not None):
            keys |= _pick_keys(entry)
    return keys


def _deduplicate(
    board: list[dict[str, Any]], picks: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split ``picks`` into the ones to apply and the ones that are repeats.

    A repeat is a second report of a player already named earlier in this batch,
    or a hand-entered pick for a player the board already has marked gone. An
    ESPN pick for an already-marked player is *kept*: re-applying it is a no-op
    for the drafted flag and it upgrades the attribution, which is how a pick
    Caroline entered by hand acquires the team that actually made it.
    """
    claimed = _claimed_keys(board)
    ordered = sorted(
        picks,
        key=lambda pick: (pick.get("overall_pick") is None, pick.get("overall_pick") or 0),
    )

    seen: set[tuple[str, Any]] = set()
    kept: list[dict[str, Any]] = []
    repeats: list[dict[str, Any]] = []
    for pick in ordered:
        keys = _pick_keys(pick)
        manual = pick.get("overall_pick") is None
        if (keys & seen) or (manual and (keys & claimed)):
            repeats.append(pick)
            continue
        seen |= keys
        kept.append(pick)
    return kept, repeats


def apply_new_picks(
    conn: sqlite3.Connection, picks: list[dict[str, Any]], bus: Any = None
) -> dict[str, Any]:
    """Mark ``picks`` on the board, store what did not match, and publish.

    The single path every pick takes, whether it came from ESPN or from
    Caroline's thumb. Returns ``{"applied", "unmatched", "duplicates", "picks",
    "board_missing"}`` — ``board_missing`` says the board is empty, which is why
    ``unmatched`` is empty too rather than holding one entry per pick.
    """
    board = store.load_board(conn)
    kept, repeats = _deduplicate(board, picks)
    if repeats:
        log.info(
            "ignored %d repeated pick(s): %s",
            len(repeats),
            ", ".join(describe_pick(pick) for pick in repeats),
        )

    updated, unmatched = apply_picks(board, kept)
    store.mark_drafted(conn, updated)

    # With no board at all, nothing can match and every pick is "unmatched" — up
    # to 95 warnings, none of which mean what the warning means. An unmatched
    # pick is meant to say "the board and reality disagree about this player";
    # here the board simply does not exist, which is one problem, not ninety-five.
    # The page is told that instead.
    board_missing = not board
    if board_missing and unmatched:
        log.warning(
            "the board is empty, so none of the %d pick(s) so far could be placed; "
            "run the board_build job",
            len(unmatched),
        )
        unmatched = []

    if unmatched:
        # The board and reality disagree about who is gone. That is the one
        # thing that makes a recommendation actively wrong, so it goes where the
        # page can read it, not only into a log file nobody opens mid-draft.
        log.warning(
            "%d pick(s) did not match the board: %s",
            len(unmatched),
            ", ".join(describe_pick(pick) for pick in unmatched),
        )
        store.record_unmatched(conn, unmatched)

    labelled = [
        {
            "overall_pick": pick.get("overall_pick"),
            "team_id": pick.get("team_id"),
            "player_id": pick.get("player_id"),
            "label": describe_pick(pick),
        }
        for pick in kept
    ]
    if bus is not None:
        _publish(
            bus,
            "board_updated",
            {
                "picks": labelled,
                "unmatched": [describe_pick(pick) for pick in unmatched],
                "board_missing": board_missing,
                "next_overall_pick": store.next_overall_pick(conn),
            },
        )
    return {
        "applied": len(kept),
        "unmatched": unmatched,
        "duplicates": len(repeats),
        "picks": labelled,
        "board_missing": board_missing,
    }


def pending_picks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Recorded picks the board does not yet agree with.

    The loop applies *this*, not the list ``sync_draft`` calls new, and that is a
    deliberate simplification with a real payoff: the board is reconciled against
    the whole pick list on every poll, so any divergence heals itself rather than
    persisting for the rest of the draft. ``sync_draft`` has already written its
    new picks by the time it returns, so they are in here too.

    Two kinds of divergence count:

    * a pick whose player is not marked gone on the board at all;
    * a pick that knows which team made it, for a player the board has marked
      gone with no team — which is exactly what a hand-entered pick leaves behind
      when ESPN catches up a minute later and says who actually took him.

    Picks already filed as unmatched are skipped. They will not match on this
    poll either, and re-applying them would republish the same warning every five
    seconds. The pass converges: once a divergence is applied, it is no longer
    pending.

    **Known limit, for Task 7.** That skip covers *resolved* unmatched picks too,
    so ``unmatched_picks.resolved_at`` can only dismiss a warning from the page —
    it can never send the pick back through here to be re-applied. Marking one
    resolved says "I have dealt with this", not "try again". If the page ever
    needs a retry button, this filter is the thing to narrow to unresolved rows,
    and the pick will then be re-applied on the next poll.
    """
    board = store.load_board(conn)
    claimed = _claimed_keys(board)
    unattributed: set[tuple[str, Any]] = set()
    for entry in board:
        drafted = entry.get("drafted") or (entry.get("drafted_by_team_id") is not None)
        if drafted and entry.get("drafted_by_team_id") is None:
            unattributed |= _pick_keys(entry)

    already_filed = {
        row["overall_pick"]
        for row in store.unmatched_picks(conn, limit=1000, include_resolved=True)
    }

    pending = []
    for pick in store.all_picks(conn):
        if pick.get("overall_pick") in already_filed:
            continue
        if not store.identifies_a_player(pick):
            # ESPN pre-fills every pick of the draft with playerId -1 and no
            # name. Such a pick can never match a board row, so applying it
            # would file a warning about a pick nobody has made.
            continue
        keys = _pick_keys(pick)
        if not (keys & claimed) or pick.get("team_id") is not None and (keys & unattributed):
            pending.append(pick)
    return pending


def record_manual_pick(
    conn: sqlite3.Connection,
    *,
    player_name: str,
    team_id: int | None = None,
    overall_pick: int | None = None,
    bus: Any = None,
) -> dict[str, Any]:
    """Record a pick Caroline entered by hand, through the same path as ESPN's.

    This is the answer to ESPN going down at the worst possible moment, and it is
    built rather than hoped for: with it and the ``[league]`` config fallback,
    hal-mary can run a whole draft with no ESPN access at all.

    A player the board already has marked gone is not recorded twice — a double
    tap on the button is one pick told twice, and the second one would otherwise
    come back unmatched on every poll for the rest of the draft.

    ``overall_pick`` is usually left out, and is then :func:`store.next_overall_pick`
    — one past the highest pick that actually names somebody.

    **Never the rowid.** ``draft_picks.overall_pick`` is an INTEGER PRIMARY KEY,
    so a NULL insert takes one past the highest *row number*, and any database
    that ran a sync before ``pick_is_made`` existed still holds ESPN's 96
    pre-populated placeholder rows. There the first hand-entered pick of the
    draft was numbered **97**, which every end-of-draft check in the project
    reads as "the draft is over": the loop moved to the ``done`` phase, stopped
    polling for good, and nothing on the page said so — on the night ESPN is
    down and picks are going in by hand, which is the one night this path
    exists for. The pick number has to come from the picks.

    The write is an upsert because the slot it lands on may be one of those
    placeholder rows. It can only ever be a placeholder or an empty slot:
    ``next_overall_pick`` is one past the last pick that names somebody, so a
    real pick is never overwritten.
    """
    name = (player_name or "").strip()
    if not name:
        raise ValueError("a manual pick needs a player name")

    board = store.load_board(conn)
    if _pick_keys({"player_name": name}) & _claimed_keys(board):
        log.info("manual pick for %s ignored: the board already has him gone", name)
        return {"recorded": False, "reason": "already drafted", "player_name": name}

    assigned = overall_pick if overall_pick is not None else store.next_overall_pick(conn)
    with db.transaction(conn):
        conn.execute(
            """
            INSERT INTO draft_picks
                (overall_pick, team_id, player_id, player_name, seen_at)
            VALUES (?, ?, NULL, ?, ?)
            ON CONFLICT (overall_pick) DO UPDATE SET
                team_id     = excluded.team_id,
                player_id   = excluded.player_id,
                player_name = excluded.player_name,
                seen_at     = excluded.seen_at
            """,
            (assigned, team_id, name, db.utc_now()),
        )

    pick = {
        "overall_pick": assigned,
        "team_id": team_id,
        "player_id": None,
        "player_name": name,
        "seen_at": db.utc_now(),
    }
    outcome = apply_new_picks(conn, [pick], bus)
    return {"recorded": True, "overall_pick": assigned, "player_name": name, **outcome}


# --- the loop ----------------------------------------------------------------


class DraftLoop:
    """Polls ESPN, keeps the board honest, and advises when her turn is close.

    Construct it with a connection opened on the thread that will run it (see the
    module docstring), the resolved settings, an ``EspnClient``, a
    ``ClaudeRunner`` and an ``EventBus``.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        client: Any,
        runner: Any,
        bus: Any,
    ) -> None:
        self.conn = conn
        self.settings = settings
        self.client = client
        self.runner = runner
        self.bus = bus
        self._warmed = False
        #: ESPN's own slot-to-team board, read once the draft opens. ``None``
        #: until then, because before the draft it is a provisional lie. Kept
        #: only as the "already read this process" guard: what the board is
        #: *for* is written to the ``draft_order`` table, where the page and the
        #: advisor can read it too.
        self._schedule: list[dict[str, Any]] | None = None
        #: Which of *her* picks the advisor last ran for. The whole defence
        #: against advising a dozen times per turn.
        self._last_advised_pick: int | None = None
        #: Which cadence the loop is on, and how it got there. ``phase`` starts
        #: idle rather than live: a loop that has read nothing has no evidence a
        #: draft is running, and guessing live is the expensive guess.
        self._phase = PHASE_IDLE
        #: When "The draft has started" was pressed, on :attr:`_clock`. Holds the
        #: loop at draft-night cadence across the gap between the draft opening
        #: and pick 1, and expires so a stray tap is not permanent.
        self._forced_live_at: float | None = None
        self._flagged_disagreement = False
        #: ``rounds x teams`` from the league, filled in by :meth:`_maybe_advise`.
        #: Only used when ESPN reports no board of its own, which is the no-ESPN
        #: contingency: without some total, a finished draft can never be told
        #: from one that is still running and the loop would poll forever.
        self._total_picks: int | None = None
        #: Swapped for a fake in tests; the loop reads no other clock.
        self._clock: Any = time
        # Two signals rather than one. `_stopping` is a threading.Event so
        # `stop()` — which is called from the web app's thread on shutdown — is
        # answered the moment it is set, with no loop involved. `_wake` is what
        # actually interrupts the idle wait, and is only ever set *on* the
        # loop's own thread, through `_wake_now`. Setting an asyncio.Event from
        # another thread appears to work and then does not: it resolves the
        # waiter's future through `call_soon`, which never writes the loop's
        # self-pipe, so a loop parked in select() stays parked until its timeout
        # — five minutes, on the idle cadence, for every deploy.
        self._stopping = threading.Event()
        self._wake = asyncio.Event()
        self._eventloop: asyncio.AbstractEventLoop | None = None

    # -- what the loop is doing, and how often -----------------------------

    @property
    def phase(self) -> str:
        """:data:`PHASE_IDLE`, :data:`PHASE_LIVE` or :data:`PHASE_DONE`."""
        return self._phase

    @property
    def poll_interval(self) -> int | None:
        """Seconds until the next poll, or ``None`` when there will not be one.

        Read fresh on every pass of :meth:`run_forever` rather than captured at
        startup, which is what lets a phase change take effect on the next tick
        instead of on the next restart.
        """
        if self._phase == PHASE_DONE:
            return None
        if self._phase == PHASE_LIVE:
            return self.settings.draft.poll_seconds
        return self.settings.draft.idle_poll_seconds

    def describe_cadence(self) -> str:
        """The cadence in the words the log and the page use."""
        interval = self.poll_interval
        if interval is None:
            return "not polling ESPN at all"
        return f"polling ESPN {cadence_words(interval)}"

    def watching(self) -> dict[str, Any]:
        """What the loop is doing, for the page to say out loud.

        The page asks rather than deriving it. The two would agree on every
        night but one — the night somebody presses "The draft has started",
        where the loop is on draft-night cadence and the board it would be
        derived from still shows no picks at all.
        """
        return {
            "phase": self._phase,
            "poll_seconds": self.poll_interval,
            "cadence": cadence_words(self.poll_interval),
        }

    def _forced_live(self) -> bool:
        """Is "The draft has started" still holding the loop at live cadence?"""
        if self._forced_live_at is None:
            return False
        window = self.settings.draft.live_override_seconds
        if self._clock.monotonic() - self._forced_live_at < window:
            return True
        log.info(
            "the 'draft has started' override has expired after %ss with no pick on "
            "ESPN's board; the board is back in charge of the cadence",
            window,
        )
        self._forced_live_at = None
        return False

    def _expire_override(self) -> None:
        """Let "The draft has started" lapse on a tick that never reached ESPN.

        Only ever falls back to idle, which is safe by construction: the
        override is cleared the moment the board itself justifies live or done,
        so a live override still standing means the board has never said
        anything else.
        """
        if self._forced_live_at is None or self._forced_live():
            return
        self._enter_phase(
            PHASE_IDLE, reason="the override expired and ESPN is not answering"
        )

    def _note_flag_disagreement(self, status: dict[str, Any], picks_made: int) -> None:
        """Say so, once, when ESPN's own flag disagrees with ESPN's own board.

        This is the whole job ``drafted`` is trusted with. It cannot stop the
        loop — see :func:`draft_phase` — but a flag saying the draft is over
        while the board still has empty slots is worth exactly one line in the
        log, because it is the sentence that explains why the loop is still
        polling when somebody thinks it should not be.
        """
        if not status.get("drafted") or self._phase == PHASE_DONE:
            self._flagged_disagreement = False
            return
        if self._flagged_disagreement:
            return
        self._flagged_disagreement = True
        log.warning(
            "ESPN says draftDetail.drafted is true, but only %s of %s slots on its own board "
            "have a player in them; still watching, because that flag is set late",
            picks_made,
            status.get("slots"),
        )

    def _update_phase(self, picks_made: int) -> str:
        """Recompute the cadence from what this tick already read.

        ``picks_made`` is a **count**, never the highest pick number. The two
        differ exactly when a row is numbered oddly, and one such row —
        a hand-entered pick that took a rowid past ESPN's placeholder slots —
        used to make a draft that had barely started read as finished. A count
        cannot exceed the number of slots unless there really are that many
        picks.
        """
        status = self._draft_status()
        picks_made = max(picks_made, (status or {}).get("picks_made") or 0)
        total = (status or {}).get("slots") or self._total_picks
        phase = draft_phase(picks_made=picks_made, total_slots=total)
        reason = f"{picks_made} of {total} slots on ESPN's board have a player in them"
        if phase == PHASE_IDLE and self._forced_live():
            # The override, and the only place it is applied. Between the draft
            # opening and pick 1 ESPN's board is genuinely empty, so the board
            # cannot yet tell the difference — that gap is what the button is
            # for.
            phase = PHASE_LIVE
            reason = "she said the draft has started and ESPN has no pick yet"
        elif phase != PHASE_IDLE:
            self._forced_live_at = None
        if status is not None:
            self._note_flag_disagreement(status, picks_made)
        self._enter_phase(phase, reason=reason)
        return phase

    def _draft_status(self) -> dict[str, Any] | None:
        """What the read this tick already made says about the draft.

        ``None`` from a client that does not report one — the loop then falls
        back to the league's own ``rounds x teams``, which is what the no-ESPN
        contingency runs on.
        """
        reader = getattr(self.client, "draft_status", None)
        if reader is None:
            return None
        try:
            return reader()
        except Exception:
            log.debug("could not read the draft status; using the board alone", exc_info=True)
            return None

    def _enter_phase(self, phase: str, *, reason: str) -> None:
        """Move to ``phase``, saying so in the log and on the page.

        Both are the answer to "was it watching?", which is the first thing
        anybody asks afterwards, so the line names the old cadence and the new
        one rather than the phase names alone.
        """
        if phase == self._phase:
            return
        previous, was, before = self._phase, self.describe_cadence(), self.poll_interval
        self._phase = phase
        log.info(
            "the draft looks %s rather than %s: hal-mary is now %s (%s), was %s (%s); %s",
            phase,
            previous,
            self.describe_cadence(),
            _interval_words(self.poll_interval),
            was,
            _interval_words(before),
            reason,
        )
        _publish(
            self.bus,
            "draft_phase",
            {**self.watching(), "reason": reason},
        )

    def draft_started(self) -> dict[str, Any]:
        """"The draft has started" — an override, from the page, on any thread.

        Two things happen: the loop goes to draft-night cadence at once rather
        than waiting out an idle interval, and it wakes up and polls now.

        It is **not** the mechanism. :meth:`_update_phase` reaches live on its
        own from the first pick ESPN reports, so a night nobody presses this is
        a night that costs one idle interval, not a night hal-mary sat out.
        """
        self._forced_live_at = self._clock.monotonic()
        self._enter_phase(PHASE_LIVE, reason="she said the draft has started")
        self._wake_now()
        return self.watching()

    # -- startup ----------------------------------------------------------

    def warm(self) -> bool:
        """Build the client's player-name map before the draft starts.

        Attempted once. A failure costs names on the first pick or two — the
        client retries on its own cooldown — and must not cost the draft, so it
        is logged and the loop carries on.
        """
        self._warmed = True
        try:
            names = self.client.player_name_map()
        except Exception as exc:  # noqa: BLE001 - any ESPN failure, before the draft
            log.warning(
                "could not warm the ESPN player list (%s); early picks may show ids "
                "instead of names",
                exc,
            )
            return False
        log.info("warmed the ESPN player list: %d players known before the draft", len(names))
        return True

    # -- one tick ---------------------------------------------------------

    async def run_once(self) -> dict[str, Any]:
        """One poll. Returns what happened; never raises."""
        result: dict[str, Any] = {
            "new_picks": 0,
            "applied": 0,
            "unmatched": [],
            "duplicates": 0,
            "advised": False,
            "draft_over": False,
            "error": None,
        }
        # Set before anything else in the tick. The ESPN read below is bounded
        # by espn.connect_timeout_s + read_timeout_s — 25 seconds — and it runs
        # *before* the advisor, so a budget started after it would let a slow
        # sync push the card past the end of the pick clock. Everything the tick
        # spends comes out of this one allowance.
        deadline = time.monotonic() + self.settings.draft.advice_budget_s

        if not self._warmed:
            self.warm()

        try:
            new = sync_draft(self.conn, self.client)
        except Exception as exc:  # noqa: BLE001 - ESPN is unofficial and flaky
            # Publish nothing: a stale board that says so is better than a page
            # that redraws with the same content every five seconds.
            log.warning("draft sync failed (%s); the loop continues", exc)
            result["error"] = str(exc)
            # The phase is not recomputed on a tick that never reached ESPN —
            # there is nothing fresh to recompute it from. The override is the
            # exception: it is bounded by the clock, not by the board, and
            # leaving it to lapse only on a *successful* sync means pressing
            # "The draft has started" with expired cookies hammers a failing
            # endpoint every five seconds for the life of the process.
            self._expire_override()
            result["phase"] = self._phase
            result["poll_seconds"] = self.poll_interval
            return result

        try:
            result["new_picks"] = len(new)
            pending = pending_picks(self.conn)
            if pending:
                applied = apply_new_picks(self.conn, pending, self.bus)
                result["applied"] = applied["applied"]
                result["unmatched"] = applied["unmatched"]
                result["duplicates"] = applied["duplicates"]
            result.update(self._maybe_advise(deadline))
        except Exception as exc:
            log.exception("draft loop tick failed after the sync")
            result["error"] = str(exc)
        # Last, and outside the try above only in the sense that it must happen
        # whatever the advisor did: the cadence is what decides whether there is
        # a next tick at all, and an advisor that raised must not leave the loop
        # polling a finished draft every five seconds forever.
        result["phase"] = self._update_phase(store.picks_made(self.conn))
        result["poll_seconds"] = self.poll_interval
        return result

    def _read_schedule(self, next_pick: int) -> None:
        """Read ESPN's own slot-to-team board, once, when the draft opens.

        ``draftSettings.orderType`` on this league is ``DRAFT_START``: ESPN
        assigns the real draft order at the moment the draft begins. The board it
        pre-populates before then is built from a provisional order, so reading
        it early and caching it would be a plausible-looking lie about who picks
        when — which is why this waits for the first real pick, and why it never
        reads it twice: once the draft is running, the order does not change.

        What it does with the board is the point of Task 15: round one's
        slot-to-team mapping is **persisted**, and ``league._espn_order`` prefers
        it over the pre-draft ``pickOrder`` from then on. So the loop does not
        keep a private view of who picks when — it corrects the one source the
        draft page, the advisor and this loop all read. The alternative, passing
        this loop's window into ``advise``, would label the card from the
        schedule while the page stayed on the arithmetic, and a card labelled
        from a different source than the page reads as stale on every turn.

        The write is once and only once (see :func:`store.store_draft_order`): a
        restart mid-draft re-reads the board, and a second answer that disagreed
        must not move her pick window while she is looking at it.

        A failure is not fatal. The snake arithmetic over the synced pick order
        is the fallback, and it is right whenever ESPN did not shuffle.
        """
        if self._schedule is not None or next_pick <= 1:
            return
        reader = getattr(self.client, "draft_schedule", None)
        if reader is None:  # pragma: no cover - every real client has one
            return
        try:
            schedule = reader()
        except Exception as exc:  # noqa: BLE001 - ESPN is unofficial and flaky
            log.warning("could not read the draft schedule (%s); using the snake order", exc)
            return
        rows = [slot for slot in schedule or [] if slot.get("overall_pick") is not None]
        if not rows:
            return
        self._schedule = sorted(rows, key=lambda slot: slot["overall_pick"])
        log.info("read ESPN's draft board: %d slots, order now final", len(self._schedule))
        self._store_order()

    def _store_order(self) -> None:
        """Persist round one of the board as *the* draft order, once.

        Round one is the whole order: every later round is that list snaked, and
        storing one list keeps the stored shape identical to the ``pickOrder`` it
        replaces — so every consumer of ``LeagueContext.draft_order`` gets the
        corrected value with no other change at all.

        A board whose first round is short or names a team twice is not an order.
        Storing it would put two of her picks in one round, or none; the snake
        arithmetic over the placeholder is wrong in a smaller way than that.

        **"Short" is measured against the league, not against two.** ESPN is
        unofficial and the first poll after pick 1 catches it mid-write: a round
        one whose last slots have no ``team_id`` yet reads as a perfectly
        well-formed four-team order for a six-team league. Distinct, more than
        two, and completely wrong. Storing it would be the worst outcome
        available — :func:`hal_mary.league._espn_order` discards a stored order
        of the wrong length on every load, and the write happens once, so the
        good board on the next poll is refused and the placeholder stands for the
        whole night behind a single log line. The count to beat is the number of
        distinct teams the board itself names across every round, which the later
        rounds carry even while round one is still filling in.

        Never fatal: this runs on the pick-clock path, and a card built on the
        old order beats no card.
        """
        if self._schedule is None:  # pragma: no cover - only called after a read
            return
        first_round = [
            slot["team_id"]
            for slot in self._schedule
            if slot.get("round_num") == 1 and slot.get("team_id") is not None
        ]
        on_the_board = {
            slot["team_id"] for slot in self._schedule if slot.get("team_id") is not None
        }
        if len(first_round) != len(set(first_round)):
            log.warning(
                "ESPN's draft board has an unusable first round: %d slot(s) naming only %d "
                "distinct team(s); keeping the pick order the sync stored",
                len(first_round),
                len(set(first_round)),
            )
            return
        if len(first_round) < max(2, len(on_the_board)):
            log.warning(
                "ESPN's draft board has an unusable first round: %d slot(s) attributed of the "
                "%d team(s) the board names; keeping the pick order the sync stored and "
                "leaving the write for a whole board",
                len(first_round),
                len(on_the_board),
            )
            return
        try:
            if store.store_draft_order(self.conn, first_round):
                log.info("stored the draft order ESPN drew: %s", first_round)
        except Exception:  # a locked database must not cost her the card
            log.exception("could not store the draft order; the placeholder stands")

    def _maybe_advise(self, deadline: float | None = None) -> dict[str, Any]:
        # The schedule read comes first, and it writes what it learns to the
        # database rather than keeping it here. Only then is the league loaded,
        # so ``league.draft_order`` is the order ESPN drew rather than the
        # placeholder — and the page and the advisor, which load the same league
        # the same way, agree with this loop by construction rather than by two
        # implementations of the same arithmetic happening to match.
        next_pick = store.next_overall_pick(self.conn)
        self._read_schedule(next_pick)

        try:
            league = load_league_context(self.conn, self.settings)
        except LeagueUnknown as exc:
            log.warning("cannot advise: %s", exc)
            return {"advised": False}

        # The only other thing that knows how long the draft is. ESPN's own row
        # count is preferred when there is one; this is what the no-ESPN path has.
        self._total_picks = league.total_picks

        upcoming = league.upcoming_picks(next_pick)
        if not upcoming:
            # The only end-of-draft signal the board arithmetic gives.
            # ``picks_until_mine`` would count down forever past pick 96.
            return {"advised": False, "draft_over": True}

        if upcoming[0] - next_pick > self.settings.draft.advise_within_picks:
            return {"advised": False}

        target = upcoming[0]
        if self._last_advised_pick == target:
            return {"advised": False}

        # Set before the call, not after: a call that fails halfway must not be
        # retried on the next five-second poll, and again on the one after that.
        self._last_advised_pick = target
        log.info("advising for pick %s (pick %s is on the clock)", target, next_pick)
        advice = advise(
            self.conn,
            self.settings,
            self.runner,
            self.bus,
            next_overall_pick=next_pick,
            deadline=deadline,
        )
        return {"advised": True, "advice": advice}

    # -- forever ----------------------------------------------------------

    async def run_forever(self) -> None:
        """Poll until :meth:`stop`, or until the draft is over. Never raises.

        The interval is read back from :attr:`poll_interval` after every tick,
        which is what lets a phase change take effect on the next poll rather
        than on the next restart — and what lets the loop end itself when
        ESPN's board is full, which is the only thing that ever told it to stop
        before.
        """
        self._eventloop = asyncio.get_running_loop()
        log.info("draft loop starting; %s", self.describe_cadence())
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception:
                log.exception("draft loop tick raised; continuing")
            if self._stopping.is_set():
                break
            interval = self.poll_interval
            if interval is None:
                log.info(
                    "every slot on ESPN's draft board has a player in it, so the draft is "
                    "over and the loop is stopping; nothing else will be read from ESPN"
                )
                break
            await self._wait_for_next_poll(interval)
        log.info("draft loop stopped")

    async def _wait_for_next_poll(self, seconds: float) -> None:
        """Sleep between polls, waking at once for :meth:`stop` or the button.

        ``asyncio.sleep`` would be wrong here in a way that only shows up on the
        idle cadence: a deploy stops the service with SIGTERM, and a loop that
        only noticed when its wait timed out would keep polling ESPN for up to
        five more minutes after the process looked stopped — once per deploy,
        accumulating.
        """
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=max(seconds, 0))
        except TimeoutError:
            pass

    def _wake_now(self) -> None:
        """Interrupt the wait, from whichever thread is calling.

        ``asyncio.Event.set`` is not thread-safe, and its failure is silent:
        off-loop it resolves the waiter through ``call_soon``, which does not
        write the loop's self-pipe, so a loop parked in ``select`` sleeps out its
        full timeout anyway. ``call_soon_threadsafe`` is the one documented way
        across, and it is the same reason :class:`hal_mary.events.EventBus`
        publishes the way it does.
        """
        loop = self._eventloop
        if loop is None:  # not running yet; nothing is asleep
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._wake.set()
            return
        try:
            loop.call_soon_threadsafe(self._wake.set)
        except RuntimeError:  # pragma: no cover - the loop is already closed
            pass

    def stop(self) -> None:
        """Ask :meth:`run_forever` to finish, from any thread, and wake it now.

        Called from the web app's shutdown on another thread. The flag is a
        ``threading.Event`` so it is true the instant this returns, whatever the
        loop is doing; the wake-up is what stops the process waiting out an idle
        interval before the flag is ever looked at.
        """
        self._stopping.set()
        self._wake_now()

    # -- manual entry -----------------------------------------------------

    def record_manual_pick(
        self,
        *,
        player_name: str,
        team_id: int | None = None,
        overall_pick: int | None = None,
    ) -> dict[str, Any]:
        """:func:`record_manual_pick` against this loop's connection and bus."""
        return record_manual_pick(
            self.conn,
            player_name=player_name,
            team_id=team_id,
            overall_pick=overall_pick,
            bus=self.bus,
        )


def cadence_words(seconds: int | None) -> str:
    """A poll interval as a person says it: "every 5 seconds", "every 5 minutes".

    Shared by the log line and the draft page so the two cannot drift, which
    matters because "was it watching?" is answered from whichever of them the
    person asking happens to be looking at.
    """
    if seconds is None:
        return "not at all"
    if seconds >= 60 and seconds % 60 == 0:
        minutes = seconds // 60
        return f"every {minutes} minute{'' if minutes == 1 else 's'}"
    return f"every {seconds} second{'' if seconds == 1 else 's'}"


def _interval_words(seconds: int | None) -> str:
    """A poll interval in the log's own words. ``None`` is a loop that has stopped."""
    return "never again" if seconds is None else f"{seconds}s"


def _publish(bus: Any, event: str, payload: dict[str, Any]) -> None:
    if bus is None:
        return
    try:
        bus.publish(event, payload)
    except Exception:  # pragma: no cover - the bus does not raise
        log.exception("could not publish %s", event)
