"""The one answer to "what league is this?", and where that answer comes from.

Every downstream decision hangs off this: how many rounds there are, which
overall picks are Caroline's, whether a catch is worth a point, which roster
slots are still open. Get it wrong and the advice is confidently wrong, which is
worse than no advice at all — a model handed no league context assumes a
twelve-team standard-scoring draft, and this league is neither.

**Why this module exists rather than a `.league_settings()` call on the client.**
The draft is close and ESPN credentials may not arrive in time. hal-mary must be
able to run a draft with *no ESPN access whatsoever*: pick discovery already has
a manual path in :func:`hal_mary.draft.loop.record_manual_pick`, and this closes
the other half. Precedence, applied field by field:

1. the ``league_settings`` row a successful ``hal-mary sync`` wrote — ESPN is
   the authority on its own league;
2. the ``[league]`` section of ``config.toml``, filled in by hand, which covers
   both "nothing has ever synced" and "the sync landed but ESPN omitted the
   roster slots".

The config section existing does not disable syncing, and a synced value is
never overridden by it. Neither source available is a loud error naming both
fixes — never a guessed default.

Nothing here touches :class:`~hal_mary.espn.client.EspnClient`. ``board_build``
and the advisor read the league through this function and only this function, so
there is exactly one place the precedence rule lives.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from hal_mary.config import LeagueConfig, Settings
from hal_mary.draft import board as board_math

__all__ = ["LeagueContext", "LeagueUnknown", "load_league_context"]

log = logging.getLogger(__name__)

#: Roster slots nobody drafts into. Injured reserve is stocked from the waiver
#: wire during the season, so counting it would add a seventeenth round to a
#: sixteen-round draft and put a pick number on every player that does not exist.
_UNDRAFTED_SLOTS = frozenset({"IR", "RES", "TAXI", "IR/RES"})

#: ESPN's stat id for "each reception". The one scoring line that reorders the
#: entire board, and the reason ``scoringType`` alone ("H2H_POINTS") is useless.
_RECEPTION_STAT_ID = 53

#: Draft types that run back and forth rather than in a fixed order.
_SNAKE_TYPES = frozenset({"SNAKE", "SNAKE_DRAFT", "SERPENTINE"})


class LeagueUnknown(RuntimeError):
    """Neither a synced row nor a ``[league]`` config section could be read.

    Deliberately fatal. Every caller would otherwise have to invent a default
    league, and a wrong default produces advice that looks right.
    """


@dataclass(frozen=True)
class LeagueContext:
    """Everything the draft code needs to know about the league, from one place.

    ``draft_order`` is team ids by first-round slot, so ``draft_order[0]`` picks
    first. ``draft_order_labels`` is the same list rendered for a human, which is
    what a prompt and the web page want.
    """

    source: str
    season: int | None
    league_id: int | None
    name: str | None
    team_count: int
    scoring_type: str | None
    points_per_reception: float | None
    scoring_summary: str
    draft_type: str | None
    snake: bool
    draft_date: str | None
    rounds: int
    my_team_id: int
    my_draft_slot: int
    roster_slots: dict[str, int] = field(default_factory=dict)
    draft_order: list[int] = field(default_factory=list)
    draft_order_labels: list[str] = field(default_factory=list)

    @property
    def total_picks(self) -> int:
        """The last overall pick number of the draft."""
        return self.rounds * self.team_count

    @property
    def starting_slots(self) -> dict[str, int]:
        """Starting slots only, bench and IR dropped — what "a need" means."""
        return {
            slot: count
            for slot, count in self.roster_slots.items()
            if slot.upper() not in _UNDRAFTED_SLOTS and slot.upper() not in {"BE", "BN", "BENCH"}
        }

    def upcoming_picks(self, next_overall_pick: int) -> list[int]:
        """Caroline's remaining overall pick numbers from ``next_overall_pick`` on.

        Empty means the draft is over — the only end-of-draft signal the board
        arithmetic gives, so check it before showing a countdown.
        """
        return board_math.my_upcoming_picks(
            draft_order=self.draft_order,
            my_team_id=self.my_team_id,
            next_overall_pick=next_overall_pick,
            total_teams=self.team_count,
            rounds=self.rounds,
            snake=self.snake,
        )

    def picks_until_mine(self, next_overall_pick: int) -> int:
        """How many picks happen before hers, counting from the one on the clock."""
        return board_math.picks_until_mine(
            draft_order=self.draft_order,
            my_team_id=self.my_team_id,
            next_overall_pick=next_overall_pick,
            total_teams=self.team_count,
            snake=self.snake,
        )


# --- reading the two sources -------------------------------------------------


def _synced_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    try:
        return conn.execute("SELECT * FROM league_settings WHERE id = 1").fetchone()
    except sqlite3.Error:  # pragma: no cover - an unmigrated database
        return None


def _loads(text: Any) -> dict[str, Any]:
    """Parse a JSON column, treating anything unreadable as absent.

    A corrupt ``raw_json`` must degrade to "we do not know the scoring", not
    take the draft loop down mid-draft.
    """
    if not text:
        return {}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        log.warning("league_settings holds unreadable JSON; ignoring it")
        return {}
    return value if isinstance(value, dict) else {}


def _points_per_reception(raw: dict[str, Any]) -> float | None:
    for item in (raw.get("scoringSettings", {}) or {}).get("scoringItems", []) or []:
        if item.get("statId") == _RECEPTION_STAT_ID:
            try:
                return float(item.get("points", 0))
            except (TypeError, ValueError):
                return None
    return None


def _scoring_summary(points: float | None) -> str:
    """One sentence, in words Caroline can act on.

    "H2H_POINTS" tells her nothing and tells the model less. Whether a catch is
    worth a point is what decides whether a pass-catching running back is a
    second-round pick or a sixth-round one.
    """
    if points is None:
        return (
            "Scoring is not known. Do not assume any particular scoring system; "
            "say which settings you would need instead."
        )
    if points >= 1:
        return (
            f"Every catch is worth {points:g} point{'' if points == 1 else 's'} on its own "
            "(this is called full PPR, points per reception). Players who catch a lot of "
            "passes are worth more here than their raw scoring suggests."
        )
    if points > 0:
        return (
            f"Every catch is worth {points:g} points on its own (this is called half PPR). "
            "Players who catch a lot of passes get a modest boost."
        )
    return (
        "Catches are worth nothing on their own — only yards and touchdowns score "
        "(this is called standard scoring)."
    )


def _rounds_from_slots(roster_slots: dict[str, int]) -> int:
    """How many rounds a draft with these roster slots runs.

    One round per drafted roster spot: starters plus bench, minus the slots
    nobody drafts into. Six teams with sixteen drafted spots each is a 96-pick
    draft, which is exactly what ESPN reports for this league.
    """
    return sum(
        count
        for slot, count in roster_slots.items()
        if slot.upper() not in _UNDRAFTED_SLOTS and count > 0
    )


def _config_order(config: LeagueConfig) -> tuple[list[int], list[str]]:
    """Turn ``[league].draft_order`` into (team ids, human labels).

    Entries may be ids (matching ESPN) or names (what the draft lobby shows).
    Names get the 1-based slot as their id, which is arbitrary but consistent:
    with no ESPN there are no real ids, and every consumer only ever compares
    these to each other. A mix of the two is refused rather than guessed.
    """
    entries = list(config.draft_order)
    if not entries:
        return [], []
    ints = [entry for entry in entries if isinstance(entry, int)]
    if ints and len(ints) != len(entries):
        raise LeagueUnknown(
            "[league].draft_order mixes team ids and team names; use one or the other"
        )
    if ints:
        return [int(entry) for entry in entries], [str(entry) for entry in entries]
    return list(range(1, len(entries) + 1)), [str(entry) for entry in entries]


def _espn_order(row: sqlite3.Row, raw: dict[str, Any], conn: sqlite3.Connection) -> list[int]:
    """Team ids by first-round slot, from ESPN, best source first.

    ``draftSettings.orderType`` on this league is ``DRAFT_START``, which suggests
    the order may only be assigned when the draft begins — so ``pickOrder`` is
    provisional and the draft loop re-reads it when the draft goes live. This is
    the pre-draft best guess, not a cached truth.
    """
    order = (raw.get("draftSettings", {}) or {}).get("pickOrder") or []
    ids = [int(team_id) for team_id in order if isinstance(team_id, int | str) and str(team_id).lstrip("-").isdigit()]
    if ids:
        return ids

    rows = conn.execute(
        "SELECT team_id FROM teams WHERE draft_slot IS NOT NULL ORDER BY draft_slot"
    ).fetchall()
    if rows:
        return [int(team["team_id"]) for team in rows]
    return []


def load_league_context(conn: sqlite3.Connection, settings: Settings) -> LeagueContext:
    """Resolve the league from the database, falling back to ``[league]`` config.

    Raises :class:`LeagueUnknown` when neither source can produce a team count, a
    draft order and a roster shape — the three things without which no pick
    number and no roster need can be computed.
    """
    config = settings.league
    row = _synced_row(conn)
    raw = _loads(row["raw_json"]) if row is not None else {}
    source = "espn" if row is not None else "config"

    team_count = (row["team_count"] if row is not None else None) or config.team_count
    roster_slots = _loads(row["roster_slots_json"]) if row is not None else {}
    roster_slots = {
        str(slot): int(count) for slot, count in roster_slots.items() if count
    } or dict(config.roster_slots)

    config_order, config_labels = _config_order(config)
    order = _espn_order(row, raw, conn) if row is not None else []
    labels = [str(team_id) for team_id in order]
    if not order:
        order, labels = config_order, config_labels
    if not order and team_count:
        # Last resort: assume ids 1..N in slot order. Correct for this league and
        # obviously wrong for any other, which is why it is logged.
        order = list(range(1, int(team_count) + 1))
        labels = [str(team_id) for team_id in order]
        log.warning("no draft order from ESPN or config; assuming team ids 1..%s", team_count)

    if not team_count and order:
        team_count = len(order)

    if not team_count or not order or not roster_slots:
        raise LeagueUnknown(
            "hal-mary does not know this league's size, draft order or roster slots. "
            "Run `hal-mary sync` to read them from ESPN, or fill in the `[league]` "
            "section of config.toml (team_count, roster_slots, draft_order, "
            "my_draft_slot) — that section is the no-ESPN fallback."
        )

    team_count = int(team_count)
    if len(order) != team_count:
        raise LeagueUnknown(
            f"the draft order has {len(order)} teams but the league has {team_count}; "
            "fix [league].draft_order in config.toml or re-run `hal-mary sync`"
        )

    my_team_id, my_slot = _resolve_me(settings, config, order)

    points = _points_per_reception(raw)
    if points is None:
        points = config.points_per_reception
    draft_type = (row["draft_type"] if row is not None else None) or config.draft_type
    rounds = config.rounds or _rounds_from_slots(roster_slots)

    return LeagueContext(
        source=source,
        season=(row["season"] if row is not None else None) or settings.season,
        league_id=(row["league_id"] if row is not None else None) or settings.league_id,
        name=(row["name"] if row is not None else None) or config.name,
        team_count=team_count,
        scoring_type=(row["scoring_type"] if row is not None else None) or config.scoring_type,
        points_per_reception=points,
        scoring_summary=_scoring_summary(points),
        draft_type=draft_type,
        # An unknown draft type is treated as a snake, because every default
        # ESPN league is one and the arithmetic differs only in even rounds.
        snake=draft_type is None or draft_type.upper() in _SNAKE_TYPES,
        draft_date=(row["draft_date"] if row is not None else None) or config.draft_date,
        rounds=rounds,
        my_team_id=my_team_id,
        my_draft_slot=my_slot,
        roster_slots=roster_slots,
        draft_order=order,
        draft_order_labels=labels,
    )


def _resolve_me(
    settings: Settings, config: LeagueConfig, order: list[int]
) -> tuple[int, int]:
    """Which team is Caroline's, and where it picks in round one.

    ``TEAM_ID`` from the environment is authoritative when it names a team in the
    draft order. Otherwise ``[league].my_draft_slot`` decides, which is the whole
    point of the fallback: with no ESPN there is no team id to know.
    """
    team_id = settings.team_id
    if team_id is not None and team_id in order:
        return int(team_id), order.index(int(team_id)) + 1

    slot = config.my_draft_slot
    if slot is not None:
        if not 1 <= slot <= len(order):
            raise LeagueUnknown(
                f"[league].my_draft_slot is {slot}, which is not a slot in a "
                f"{len(order)}-team draft"
            )
        return order[slot - 1], slot

    raise LeagueUnknown(
        "hal-mary does not know which team is Caroline's. Set TEAM_ID in .env to a "
        "team in the draft order, or set [league].my_draft_slot in config.toml."
    )
