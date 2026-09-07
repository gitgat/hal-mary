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
  clock is 90 seconds. A slow sync (bounded at 25s by the ESPN timeouts) followed
  by two Claude attempts that time out (each costing its ``timeout_s`` *plus* the
  runner's kill-and-join teardown) would otherwise overrun the clock and the card
  would arrive after the pick was made. ``draft.advice_budget_s`` bounds the
  whole tick, and the advisor starts an attempt only if it can finish inside what
  is left — so a slow sync costs an attempt, never the card.

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
import time
from typing import Any

from hal_mary import db
from hal_mary.config import Settings
from hal_mary.draft import store
from hal_mary.draft.advisor import advise
from hal_mary.draft.board import apply_picks, normalize_name
from hal_mary.espn.sync import sync_draft
from hal_mary.league import LeagueUnknown, load_league_context

__all__ = ["DraftLoop", "apply_new_picks", "describe_pick", "pending_picks", "record_manual_pick"]

log = logging.getLogger(__name__)


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

    ``overall_pick`` is usually left out: SQLite assigns the next number, because
    ``draft_picks.overall_pick`` is an INTEGER PRIMARY KEY and a NULL insert
    takes one past the highest.
    """
    name = (player_name or "").strip()
    if not name:
        raise ValueError("a manual pick needs a player name")

    board = store.load_board(conn)
    if _pick_keys({"player_name": name}) & _claimed_keys(board):
        log.info("manual pick for %s ignored: the board already has him gone", name)
        return {"recorded": False, "reason": "already drafted", "player_name": name}

    with db.transaction(conn):
        cur = conn.execute(
            """
            INSERT INTO draft_picks
                (overall_pick, team_id, player_id, player_name, seen_at)
            VALUES (?, ?, NULL, ?, ?)
            """,
            (overall_pick, team_id, name, db.utc_now()),
        )
        assigned = overall_pick if overall_pick is not None else cur.lastrowid

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
        #: until then, because before the draft it is a provisional lie.
        self._schedule: list[dict[str, Any]] | None = None
        #: Which of *her* picks the advisor last ran for. The whole defence
        #: against advising a dozen times per turn.
        self._last_advised_pick: int | None = None
        self._stop = asyncio.Event()

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
        return result

    def _read_schedule(self, next_pick: int) -> None:
        """Read ESPN's own slot-to-team board, once, when the draft opens.

        ``draftSettings.orderType`` on this league is ``DRAFT_START``: ESPN
        assigns the real draft order at the moment the draft begins. The board it
        pre-populates before then is built from a provisional order, so reading
        it early and caching it would be a plausible-looking lie about who picks
        when — which is why this waits for the first real pick, and why it never
        reads it twice: once the draft is running, the order does not change.

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

    def _upcoming_from_schedule(self, my_team_id: int, next_pick: int) -> list[int] | None:
        """Her remaining pick numbers, straight from ESPN's board.

        ``None`` when there is no schedule to read them from, which is the
        caller's signal to fall back to the arithmetic.
        """
        if self._schedule is None:
            return None
        mine = [
            slot["overall_pick"]
            for slot in self._schedule
            if slot.get("team_id") == my_team_id and slot["overall_pick"] >= next_pick
        ]
        # A board that knows nothing about her team is not a board to trust.
        return mine or None

    def _maybe_advise(self, deadline: float | None = None) -> dict[str, Any]:
        try:
            league = load_league_context(self.conn, self.settings)
        except LeagueUnknown as exc:
            log.warning("cannot advise: %s", exc)
            return {"advised": False}

        next_pick = store.next_overall_pick(self.conn)
        self._read_schedule(next_pick)
        upcoming = self._upcoming_from_schedule(league.my_team_id, next_pick)
        if upcoming is None:
            upcoming = league.upcoming_picks(next_pick)
        if not upcoming:
            # The only end-of-draft signal the board arithmetic gives.
            # ``picks_until_mine`` would count down forever past pick 96.
            return {"advised": False, "draft_over": True}

        # From the schedule this is a subtraction; from the arithmetic it is a
        # snake walk. Both answer "how many teams pick before she does".
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
        """Poll until :meth:`stop`. One tick's failure never ends the loop."""
        log.info("draft loop starting; polling every %ss", self.settings.draft.poll_seconds)
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                log.exception("draft loop tick raised; continuing")
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=max(self.settings.draft.poll_seconds, 0)
                )
            except TimeoutError:
                pass
        log.info("draft loop stopped")

    def stop(self) -> None:
        """Ask :meth:`run_forever` to finish after the current tick."""
        self._stop.set()

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


def _publish(bus: Any, event: str, payload: dict[str, Any]) -> None:
    try:
        bus.publish(event, payload)
    except Exception:  # pragma: no cover - the bus does not raise
        log.exception("could not publish %s", event)
