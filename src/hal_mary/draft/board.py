"""Draft-board arithmetic: plain data in, plain data out.

Nothing in this module reads the database, the network or the clock. That is
deliberate and load-bearing: these functions decide what Caroline sees while a
60-second pick clock is running, and a wrong answer here is silent -- a bad pick
number or an unmatched name produces a confident recommendation for a player who
is already gone. Purity is what makes them cheap to test exhaustively.

Shapes this module assumes (all plain dicts; unknown keys are ignored and
preserved):

``board`` entry
    ``{"player_id": int | None, "name": str, "position": str | None,
    "tier": int | None, "rank": int | None, ...}`` plus the drafted markers
    ``drafted`` (bool), ``drafted_by_team_id`` (int | None) and ``drafted_at``
    (ISO-8601 str | None). A row read straight from the ``board`` table has no
    ``drafted`` key, so a non-``None`` ``drafted_by_team_id`` counts as drafted
    too. ``player_id`` may be negative: the board is researched before ESPN ids
    are known and carries a synthetic id until a sync matches it up.

``pick`` entry
    ``{"overall_pick": int, "team_id": int | None, "player_id": int | None,
    "player_name": str | None, "seen_at": str | None}``, i.e. a row of the
    ``draft_picks`` table. ``team_id`` is ``None`` for a pick entered by hand on
    the draft page, where the player is known and the team is not.

``draft_order``
    Team ids by first-round slot: ``draft_order[0]`` picks first. Slots are
    1-based in prose below, indices are 0-based in the code.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "apply_picks",
    "available",
    "my_upcoming_picks",
    "normalize_name",
    "pick_slot",
    "picks_until_mine",
    "roster_needs",
    "scarcity",
    "slot_positions",
]

#: Sorts entries with no tier/rank behind everything that has one, without
#: needing a separate branch in every comparison.
_UNRANKED = 10**9

#: Suffixes stripped only at the last, most permissive matching stage. Stripping
#: them is what lets "Marvin Harrison Jr." match "Marvin Harrison", and it is
#: also what would merge Michael Carter with Michael Carter II -- which is why
#: the suffix-preserving comparison runs first and an ambiguous suffix-stripped
#: match is refused rather than guessed.
_NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})

#: Roster slots that are not starting slots and therefore never a "need".
_NON_STARTING_SLOTS = frozenset({"BE", "BN", "BENCH", "IR", "RES", "TAXI"})

#: Multi-position slots, by the names ESPN uses. A slot not listed here accepts
#: exactly the position it is named after.
_MULTI_POSITION_SLOTS: dict[str, frozenset[str]] = {
    "FLEX": frozenset({"RB", "WR", "TE"}),
    "RB/WR": frozenset({"RB", "WR"}),
    "RB/WR/TE": frozenset({"RB", "WR", "TE"}),
    "WR/TE": frozenset({"WR", "TE"}),
    "OP": frozenset({"QB", "RB", "WR", "TE"}),
    "SUPERFLEX": frozenset({"QB", "RB", "WR", "TE"}),
    "QB/RB/WR/TE": frozenset({"QB", "RB", "WR", "TE"}),
    "DP": frozenset({"DL", "LB", "DB"}),
}


# ---------------------------------------------------------------------------
# Pick numbering
# ---------------------------------------------------------------------------


def pick_slot(overall_pick: int, draft_order: list[int], snake: bool = True) -> int:
    """Return the team id that owns 1-based ``overall_pick``.

    ``draft_order`` is team ids by first-round slot, so its length is the number
    of teams. In a snake draft even-numbered rounds run backwards; in a linear
    draft every round runs in the same direction.

    With ``N = len(draft_order)``, round ``r = (p - 1) // N + 1`` and index
    ``k = (p - 1) % N + 1``, the slot is ``k`` in an odd round (or any linear
    round) and ``N + 1 - k`` in an even snake round. In a 10-team snake draft
    that gives slot 1 the picks 1, 20, 21 and slot 3 the picks 3, 18, 23.

    Rounds are unbounded: the caller decides where the draft ends.

    Raises ``ValueError`` if ``overall_pick`` is below 1 or ``draft_order`` is
    empty.
    """
    teams = len(draft_order)
    if teams == 0:
        raise ValueError("draft_order is empty; there is nobody to own a pick")
    if overall_pick < 1:
        raise ValueError(f"overall_pick is 1-based, got {overall_pick}")

    round_index = (overall_pick - 1) // teams  # 0-based
    within = (overall_pick - 1) % teams  # 0-based position in the round
    if snake and round_index % 2 == 1:
        within = teams - 1 - within
    return draft_order[within]


def picks_until_mine(
    *,
    draft_order: list[int],
    my_team_id: int,
    next_overall_pick: int,
    total_teams: int,
    snake: bool = True,
) -> int:
    """Return how many picks happen before ours, counting from the pick on the
    clock.

    ``next_overall_pick`` is the 1-based overall pick about to be made. The
    result is 0 when that pick is ours, 1 when exactly one team picks first, and
    so on -- so the draft loop's "advise when within N picks" test is a plain
    ``<=``.

    The draft is treated as continuing indefinitely, so an answer always exists;
    in both a snake and a linear draft every team picks at least once in any
    window of ``2 * total_teams`` picks, which bounds the search. Use
    :func:`my_upcoming_picks` when the end of the draft matters.

    Raises ``ValueError`` if ``total_teams`` disagrees with ``len(draft_order)``,
    if ``my_team_id`` is not in ``draft_order``, or if ``next_overall_pick`` is
    below 1.
    """
    _validate_order(draft_order, my_team_id, total_teams)
    if next_overall_pick < 1:
        raise ValueError(f"next_overall_pick is 1-based, got {next_overall_pick}")

    for offset in range(2 * total_teams):
        if pick_slot(next_overall_pick + offset, draft_order, snake) == my_team_id:
            return offset
    # Unreachable: every team owns a pick in any window of 2 * total_teams.
    raise AssertionError("no pick found for this team within a full snake cycle")


def my_upcoming_picks(
    *,
    draft_order: list[int],
    my_team_id: int,
    next_overall_pick: int,
    total_teams: int,
    rounds: int,
    snake: bool = True,
) -> list[int]:
    """Return our remaining overall pick numbers, ascending.

    Counts from ``next_overall_pick`` inclusive (so the pick on the clock is in
    the list when it is ours) through the last pick of the draft,
    ``rounds * total_teams``. Returns ``[]`` once the draft is over.

    The advisor reasons about the wait with this: in a 10-team snake draft the
    team in slot 1 holds 1, 20, 21 -- an eighteen-pick gap and then two picks
    back to back, which changes which player is worth taking now.

    Raises ``ValueError`` on the same conditions as :func:`picks_until_mine`, and
    if ``rounds`` is below 1.
    """
    _validate_order(draft_order, my_team_id, total_teams)
    if next_overall_pick < 1:
        raise ValueError(f"next_overall_pick is 1-based, got {next_overall_pick}")
    if rounds < 1:
        raise ValueError(f"rounds must be at least 1, got {rounds}")

    last_pick = rounds * total_teams
    return [
        pick
        for pick in range(next_overall_pick, last_pick + 1)
        if pick_slot(pick, draft_order, snake) == my_team_id
    ]


def _validate_order(draft_order: list[int], my_team_id: int, total_teams: int) -> None:
    if len(draft_order) != total_teams:
        raise ValueError(
            f"draft_order has {len(draft_order)} teams but total_teams is {total_teams}"
        )
    if my_team_id not in draft_order:
        raise ValueError(f"team {my_team_id} is not in the draft order")


# ---------------------------------------------------------------------------
# Applying picks to the board
# ---------------------------------------------------------------------------


def normalize_name(name: str, *, strip_suffix: bool = True) -> str:
    """Return a comparison key for a player name.

    Casefolds, drops every character that is not a letter or a digit (so
    ``Ja'Marr`` and ``JaMarr``, ``Amon-Ra`` and ``Amon Ra``, ``St.`` and ``St``
    all agree), and with ``strip_suffix`` drops trailing ``Jr``/``Sr``/``II``/
    ``III``/``IV``.

    Word boundaries are dropped rather than collapsed to single spaces because
    the two sides disagree about hyphens as often as they disagree about spaces,
    and a full name is long enough that joining its parts creates no realistic
    collision. What it deliberately does *not* do is expand nicknames: Michael
    Thomas and Mike Thomas are different men, and merging them would mark the
    wrong player gone.
    """
    tokens = [re.sub(r"[^0-9a-z]", "", token) for token in name.casefold().split()]
    tokens = [token for token in tokens if token]
    if strip_suffix:
        while len(tokens) > 1 and tokens[-1] in _NAME_SUFFIXES:
            tokens.pop()
    return "".join(tokens)


def apply_picks(board: list[dict], picks: list[dict]) -> tuple[list[dict], list[dict]]:
    """Mark drafted players on the board.

    Returns ``(updated_board, unmatched_picks)``. Neither argument is mutated:
    every returned board entry is a fresh shallow copy in the original order,
    and the unmatched list holds the caller's own pick dicts, in the order they
    were given, so the loop can log exactly what it failed to place.

    A pick is matched against the board in three stages, most confident first:

    1. ``player_id`` equality, when both sides carry an id;
    2. the name normalized but *keeping* any suffix, which is what separates
       Michael Carter from Michael Carter II;
    3. the name normalized with the suffix stripped, which is what joins
       "Marvin Harrison Jr." to "Marvin Harrison".

    Every stage requires a *unique* hit. Two board rows that a stage cannot tell
    apart make the pick unmatched rather than a guess -- guessing marks a player
    gone who is still there, which is the worst failure this system has.

    A board row already claimed by a *different* pick in the same call is not
    claimed again: the second pick comes back unmatched. Two picks are the same
    pick when they carry the same ``overall_pick``, or -- for hand-entered picks
    that have none -- when they name the same player, so a double tap on the
    manual button is not a false alarm. See :func:`_pick_identity`.

    Re-applying the same picks to an already-updated board changes nothing,
    which is what lets the draft loop re-read the whole draft on every poll.

    Matched entries get ``drafted=True``, ``drafted_by_team_id`` from the pick
    and ``drafted_at`` from ``seen_at``. Neither is overwritten with ``None``:
    a hand-entered pick, which knows the player but not the team, leaves an
    attribution ESPN already supplied intact.
    """
    updated = [dict(entry) for entry in board]

    by_id: dict[Any, list[int]] = {}
    by_tight: dict[str, list[int]] = {}
    by_loose: dict[str, list[int]] = {}
    for index, entry in enumerate(updated):
        player_id = entry.get("player_id")
        if player_id is not None:
            by_id.setdefault(player_id, []).append(index)
        name = entry.get("name")
        if name:
            by_tight.setdefault(normalize_name(name, strip_suffix=False), []).append(index)
            by_loose.setdefault(normalize_name(name), []).append(index)

    claimed: dict[int, tuple[str, Any]] = {}
    unmatched: list[dict] = []
    for pick in picks:
        index = _match_pick(pick, by_id, by_tight, by_loose)
        identity = _pick_identity(pick)
        if index is None or (index in claimed and claimed[index] != identity):
            unmatched.append(pick)
            continue
        claimed[index] = identity
        entry = updated[index]
        team_id = pick.get("team_id")
        seen_at = pick.get("seen_at")
        updated[index] = {
            **entry,
            "drafted": True,
            # A hand-entered pick knows the player and not the team. It must not
            # blank an attribution ESPN already supplied for the same row.
            "drafted_by_team_id": (
                team_id if team_id is not None else entry.get("drafted_by_team_id")
            ),
            "drafted_at": seen_at if seen_at is not None else entry.get("drafted_at"),
        }
    return updated, unmatched


def _pick_identity(pick: dict) -> tuple[str, Any]:
    """What makes two entries in the picks list *the same pick*.

    The pick number when there is one. For a hand-entered pick there is none, so
    the player's name stands in: tapping the same player twice is one pick told
    twice, while two taps naming different players are two picks and the second
    one landing on an already-claimed row is a real signal.

    Judging this by ``team_id`` instead is how a second pick disappears without
    a trace: one team's two picks compare equal, and so do two hand-entered
    picks, whose team ids are both ``None``.
    """
    overall_pick = pick.get("overall_pick")
    if overall_pick is not None:
        return ("overall_pick", overall_pick)
    name = pick.get("player_name") or pick.get("name") or ""
    return ("name", normalize_name(name, strip_suffix=False))


def _match_pick(
    pick: dict,
    by_id: dict[Any, list[int]],
    by_tight: dict[str, list[int]],
    by_loose: dict[str, list[int]],
) -> int | None:
    player_id = pick.get("player_id")
    if player_id is not None and player_id in by_id:
        # Ambiguous here too: two board rows sharing an id make the first one a
        # coin flip, exactly as for a shared name.
        candidates = by_id[player_id]
        return candidates[0] if len(candidates) == 1 else None

    name = pick.get("player_name") or pick.get("name")
    if not name:
        return None
    for index_by_key, strip in ((by_tight, False), (by_loose, True)):
        candidates = index_by_key.get(normalize_name(name, strip_suffix=strip), [])
        if not candidates:
            continue
        if len(candidates) == 1:
            return candidates[0]
        # Ambiguous: board rows this stage cannot tell apart. Refuse to guess.
        return None
    return None


def _is_drafted(entry: dict) -> bool:
    """A row is drafted if it says so, or if a team owns it. A row read straight
    from SQLite has the second and not the first."""
    return bool(entry.get("drafted")) or entry.get("drafted_by_team_id") is not None


# ---------------------------------------------------------------------------
# Roster needs, scarcity, availability
# ---------------------------------------------------------------------------


def roster_needs(roster: list[dict], roster_slots: dict[str, int]) -> dict[str, int]:
    """Return open starting slots by slot name, in ``roster_slots`` order.

    ``roster`` is the players already on the team, each a dict with a
    ``position``. ``roster_slots`` is the league's slot counts, e.g.
    ``{"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "D/ST": 1, "K": 1,
    "BE": 7}``. Bench and IR slots are dropped: they are not needs.

    Slots are filled dedicated-first, then multi-position slots from the most
    restrictive to the least. That is optimal, because a dedicated slot can only
    ever be filled by its own position, and it is what keeps the FLEX honest: a
    roster holding exactly one TE still needs a flex body, because that TE is
    filling the TE slot, not the FLEX.

    Every starting slot appears in the result, including those with 0 open, so a
    caller can read a need without a ``.get`` default. Neither argument is
    mutated. Slot names and positions are compared case-insensitively.
    """
    open_slots = {
        slot: count
        for slot, count in roster_slots.items()
        if slot.upper() not in _NON_STARTING_SLOTS and count > 0
    }
    remaining = dict(open_slots)

    # Most restrictive slot first: a QB may only fill OP, while an RB may fill
    # RB, FLEX or OP, so spending the OP on an RB before the QB is seen would
    # understate the need.
    order = sorted(remaining, key=lambda slot: len(_slot_positions(slot)))

    for player in roster:
        position = (player.get("position") or "").upper()
        if not position:
            continue
        for slot in order:
            if remaining[slot] > 0 and position in _slot_positions(slot):
                remaining[slot] -= 1
                break
    return {slot: remaining[slot] for slot in open_slots}


def slot_positions(slot: str) -> frozenset[str]:
    """Which positions may fill roster slot ``slot`` (case-insensitively).

    ``"QB"`` accepts only quarterbacks; ``"FLEX"`` accepts a running back, a
    receiver or a tight end. Public because the advisor's deterministic fallback
    has to turn "which slots are still open" into "which players could fill
    them", and a second copy of this mapping in another module is a second copy
    that can disagree with this one about what a FLEX accepts.
    """
    key = slot.upper()
    return _MULTI_POSITION_SLOTS.get(key, frozenset({key}))


#: Internal alias kept so the rest of this module reads as it always did.
_slot_positions = slot_positions


def scarcity(board: list[dict], *, within_tiers: int = 2) -> dict[str, dict[str, int | None]]:
    """Return, per position, how deep the cliff is below the best player left.

    ``{position: {"best_tier": int | None, "count": int}}`` -- ``best_tier`` is
    the best tier still undrafted at that position and ``count`` is how many
    undrafted players sit within ``within_tiers`` tiers of it.

    The window is measured from each position's *best remaining* tier, not from
    tier 1. Absolute tiers answer "how many elite players are left", which is
    zero forever from the fourth round on and cannot drive a decision; measuring
    from the best that is left answers "how deep is the cliff below the player I
    would take right now", which is the question the advisor asks.

    The count alone is not enough to act on -- two left at tier 1 and two left
    at tier 6 call for opposite decisions -- so ``best_tier`` travels with it.

    Every position present on the board appears in the result, including
    positions with none left, which report ``{"best_tier": None, "count": 0}``.
    Rows with no ``tier`` are treated as worse than any tiered row, so they are
    counted only when nothing tiered remains; a position whose best remaining
    row is untiered reports ``best_tier`` as ``None`` rather than inventing one.

    Raises ``ValueError`` if ``within_tiers`` is below 1.
    """
    if within_tiers < 1:
        raise ValueError(f"within_tiers must be at least 1, got {within_tiers}")

    tiers_by_position: dict[str, list[int]] = {}
    for entry in board:
        position = (entry.get("position") or "").upper()
        if not position:
            continue
        tiers_by_position.setdefault(position, [])
        if not _is_drafted(entry):
            tiers_by_position[position].append(_tier_of(entry))

    result: dict[str, dict[str, int | None]] = {}
    for position, tiers in tiers_by_position.items():
        if not tiers:
            result[position] = {"best_tier": None, "count": 0}
            continue
        best = min(tiers)
        result[position] = {
            "best_tier": None if best == _UNRANKED else best,
            "count": sum(1 for tier in tiers if tier <= best + within_tiers - 1),
        }
    return result


def available(
    board: list[dict], *, limit: int = 40, positions: list[str] | None = None
) -> list[dict]:
    """Return undrafted board entries ordered by tier, then rank, then name.

    ``positions`` filters by position (case-insensitive; ``None`` means all) and
    ``limit`` caps the length. Entries with no tier or rank sort last rather
    than raising, because a research-built board can be missing either.

    The result is a list of shallow copies, so a caller cannot corrupt the board
    by editing what it hands back. The ordering is total, so the same board
    always produces the same list -- the advisor's prompt has to be
    reproducible.
    """
    wanted = {position.upper() for position in positions} if positions is not None else None
    rows = [
        entry
        for entry in board
        if not _is_drafted(entry)
        and (wanted is None or (entry.get("position") or "").upper() in wanted)
    ]
    rows.sort(key=lambda entry: (_tier_of(entry), _rank_of(entry), entry.get("name") or ""))
    return [dict(entry) for entry in rows[:limit]]


def _tier_of(entry: dict) -> int:
    tier = entry.get("tier")
    return _UNRANKED if tier is None else int(tier)


def _rank_of(entry: dict) -> int:
    rank = entry.get("rank")
    return _UNRANKED if rank is None else int(rank)
