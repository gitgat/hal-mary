"""Tests for the pure draft-board arithmetic.

Every expected pick number in this file was derived by hand from the snake rule
and written here as a literal. None of it was produced by calling the functions
under test -- a table generated from the implementation only proves the
implementation agrees with itself.

The hand derivation, for N teams and a 1-based overall pick p:

    round r = (p - 1) // N + 1
    index k = (p - 1) %  N + 1        # position within the round
    slot    = k                       # odd round, or a linear (non-snake) draft
    slot    = N + 1 - k               # even round of a snake draft

which inverts to p = (r - 1) * N + (slot if odd/linear else N + 1 - slot):

    10-team snake, slot  1 ->  1, 20, 21, 40, 41   (back-to-back across the turn)
    10-team snake, slot  3 ->  3, 18, 23, 38, 43
    10-team snake, slot  5 ->  5, 16, 25, 36, 45
    10-team snake, slot 10 -> 10, 11, 30, 31, 50   (back-to-back across the turn)
    10-team linear, slot 3 ->  3, 13, 23, 33, 43
    12-team snake, slot  7 ->  7, 18, 31
    12-team snake, slot 12 -> 12, 13
"""

import pytest

from hal_mary.draft import board
from hal_mary.draft.board import (
    apply_picks,
    available,
    my_upcoming_picks,
    pick_slot,
    picks_until_mine,
    roster_needs,
    scarcity,
)

# Team ids are deliberately not 1..10 so that an off-by-one that returns a slot
# number instead of a team id cannot pass by coincidence.
TEN = [101, 102, 103, 104, 105, 106, 107, 108, 109, 110]
TWELVE = [201, 202, 203, 204, 205, 206, 207, 208, 209, 210, 211, 212]


# --------------------------------------------------------------------------
# pick_slot
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overall_pick", "expected_team"),
    [
        # Round 1, straight down the order.
        (1, 101),
        (2, 102),
        (3, 103),
        (10, 110),
        # Round 2 reverses: 11 is the last slot again, so slots 10 and 1 both
        # own back-to-back pairs (10-11 and 20-21).
        (11, 110),
        (12, 109),
        (18, 103),
        (19, 102),
        (20, 101),
        # Round 3 turns back over.
        (21, 101),
        (22, 102),
        (23, 103),
        (30, 110),
        # Round 4 reverses again.
        (31, 110),
        (38, 103),
        (40, 101),
        # Round 5.
        (41, 101),
        (43, 103),
        (50, 110),
    ],
)
def test_pick_slot_snake_ten_teams(overall_pick, expected_team):
    assert pick_slot(overall_pick, TEN) == expected_team


@pytest.mark.parametrize(
    ("overall_pick", "expected_team"),
    [
        (1, 101),
        (10, 110),
        (11, 101),
        (13, 103),
        (20, 110),
        (21, 101),
        (43, 103),
        (50, 110),
    ],
)
def test_pick_slot_linear_ten_teams(overall_pick, expected_team):
    assert pick_slot(overall_pick, TEN, snake=False) == expected_team


@pytest.mark.parametrize(
    ("overall_pick", "expected_team"),
    [(7, 207), (12, 212), (13, 212), (18, 207), (24, 201), (25, 201), (31, 207)],
)
def test_pick_slot_snake_twelve_teams(overall_pick, expected_team):
    assert pick_slot(overall_pick, TWELVE) == expected_team


def test_pick_slot_rejects_a_pick_before_the_draft_starts():
    with pytest.raises(ValueError):
        pick_slot(0, TEN)


def test_pick_slot_rejects_an_empty_order():
    with pytest.raises(ValueError):
        pick_slot(1, [])


# --------------------------------------------------------------------------
# picks_until_mine
# --------------------------------------------------------------------------


def _until(team, next_pick, order=TEN, snake=True):
    return picks_until_mine(
        draft_order=order,
        my_team_id=team,
        next_overall_pick=next_pick,
        total_teams=len(order),
        snake=snake,
    )


@pytest.mark.parametrize(
    ("team", "next_pick", "expected"),
    [
        # Slot 1 owns 1, 20, 21, 40, 41.
        (101, 1, 0),
        (101, 2, 18),  # 20 - 2
        (101, 11, 9),  # 20 - 11
        (101, 19, 1),  # 20 - 19
        (101, 20, 0),
        (101, 21, 0),  # the second half of the back-to-back pair
        (101, 22, 18),  # 40 - 22
        # Slot 3 owns 3, 18, 23, 38, 43.
        (103, 1, 2),  # 3 - 1
        (103, 4, 14),  # 18 - 4
        (103, 18, 0),
        (103, 19, 4),  # 23 - 19
        (103, 24, 14),  # 38 - 24
        # Slot 10 owns 10, 11, 30, 31, 50.
        (110, 1, 9),  # 10 - 1
        (110, 10, 0),
        (110, 11, 0),
        (110, 12, 18),  # 30 - 12
        (110, 31, 0),
        (110, 32, 18),  # 50 - 32
        # Slot 5 owns 5, 16, 25, 36, 45.
        (105, 1, 4),
        (105, 6, 10),  # 16 - 6
        (105, 17, 8),  # 25 - 17
    ],
)
def test_picks_until_mine_snake(team, next_pick, expected):
    assert _until(team, next_pick) == expected


@pytest.mark.parametrize(
    ("team", "next_pick", "expected"),
    [
        # Linear: slot 1 owns 1, 11, 21...; slot 3 owns 3, 13, 23...
        (101, 1, 0),
        (101, 2, 9),  # 11 - 2
        (101, 11, 0),
        (103, 1, 2),
        (103, 4, 9),  # 13 - 4
        (110, 11, 9),  # 20 - 11
    ],
)
def test_picks_until_mine_linear(team, next_pick, expected):
    assert _until(team, next_pick, snake=False) == expected


def test_picks_until_mine_rejects_a_team_that_is_not_in_the_order():
    with pytest.raises(ValueError):
        _until(999, 1)


def test_picks_until_mine_rejects_a_team_count_that_contradicts_the_order():
    with pytest.raises(ValueError):
        picks_until_mine(
            draft_order=TEN,
            my_team_id=101,
            next_overall_pick=1,
            total_teams=12,
            snake=True,
        )


# --------------------------------------------------------------------------
# my_upcoming_picks
# --------------------------------------------------------------------------


def _upcoming(team, next_pick, rounds, order=TEN, snake=True):
    return my_upcoming_picks(
        draft_order=order,
        my_team_id=team,
        next_overall_pick=next_pick,
        total_teams=len(order),
        rounds=rounds,
        snake=snake,
    )


@pytest.mark.parametrize(
    ("team", "expected"),
    [
        (101, [1, 20, 21, 40, 41]),
        (103, [3, 18, 23, 38, 43]),
        (105, [5, 16, 25, 36, 45]),
        (110, [10, 11, 30, 31, 50]),
    ],
)
def test_my_upcoming_picks_snake_from_the_top_of_the_draft(team, expected):
    assert _upcoming(team, 1, 5) == expected


def test_my_upcoming_picks_starts_from_the_pick_on_the_clock():
    # Slot 1 is on the clock at 21, the second of its back-to-back pair.
    assert _upcoming(101, 21, 5) == [21, 40, 41]
    # Slot 10 is on the clock at 11, likewise.
    assert _upcoming(110, 11, 5) == [11, 30, 31, 50]
    # A pick that has just passed is not ours to make again.
    assert _upcoming(110, 12, 5) == [30, 31, 50]


def test_my_upcoming_picks_linear():
    assert _upcoming(103, 1, 5, snake=False) == [3, 13, 23, 33, 43]
    assert _upcoming(101, 1, 5, snake=False) == [1, 11, 21, 31, 41]


def test_my_upcoming_picks_twelve_team_snake():
    assert _upcoming(207, 1, 3, order=TWELVE) == [7, 18, 31]
    assert _upcoming(212, 1, 2, order=TWELVE) == [12, 13]


def test_my_upcoming_picks_is_empty_once_the_draft_is_over():
    assert _upcoming(101, 51, 5) == []


def test_my_upcoming_picks_rejects_a_nonsense_round_count():
    with pytest.raises(ValueError):
        _upcoming(101, 1, 0)


# --------------------------------------------------------------------------
# apply_picks -- name normalization
# --------------------------------------------------------------------------


def _board_row(player_id, name, position="WR", tier=1, rank=1):
    return {
        "player_id": player_id,
        "name": name,
        "position": position,
        "pro_team": "XXX",
        "tier": tier,
        "rank": rank,
        "note": "",
        "drafted": False,
        "drafted_by_team_id": None,
        "drafted_at": None,
    }


def _pick(overall, name, team_id=101, player_id=None, seen_at="2026-09-07T12:00:00Z"):
    return {
        "overall_pick": overall,
        "round_num": 1,
        "round_pick": overall,
        "team_id": team_id,
        "player_id": player_id,
        "player_name": name,
        "seen_at": seen_at,
    }


def _by_name(board, name):
    return next(row for row in board if row["name"] == name)


def test_apply_picks_matches_on_player_id_when_both_sides_have_one():
    board = [_board_row(4262921, "Ja'Marr Chase"), _board_row(4430737, "Bijan Robinson", "RB")]
    picks = [_pick(1, "Chase, Ja'Marr", player_id=4262921)]

    updated, unmatched = apply_picks(board, picks)

    assert unmatched == []
    assert _by_name(updated, "Ja'Marr Chase")["drafted"] is True
    assert _by_name(updated, "Ja'Marr Chase")["drafted_by_team_id"] == 101
    assert _by_name(updated, "Ja'Marr Chase")["drafted_at"] == "2026-09-07T12:00:00Z"
    assert _by_name(updated, "Bijan Robinson")["drafted"] is False


def test_apply_picks_matches_across_punctuation_and_suffix_spelling():
    board = [
        _board_row(-1, "Ja'Marr Chase"),
        _board_row(-2, "Marvin Harrison Jr."),
        _board_row(-3, "Amon-Ra St. Brown"),
    ]
    picks = [
        _pick(1, "JaMarr Chase"),
        _pick(2, "Marvin Harrison Jr"),
        _pick(3, "Amon Ra St Brown"),
    ]

    updated, unmatched = apply_picks(board, picks)

    assert unmatched == []
    assert all(row["drafted"] for row in updated)


def test_apply_picks_matches_when_espn_drops_the_suffix_entirely():
    board = [_board_row(-1, "Brian Thomas Jr.", "WR")]

    updated, unmatched = apply_picks(board, [_pick(1, "Brian Thomas")])

    assert unmatched == []
    assert updated[0]["drafted"] is True


def test_michael_thomas_and_mike_thomas_do_not_collide():
    """Two genuinely different receivers. A normalizer that expands nicknames
    would merge them and mark the wrong man gone."""
    board = [_board_row(-1, "Michael Thomas"), _board_row(-2, "Mike Thomas")]

    updated, unmatched = apply_picks(board, [_pick(1, "Mike Thomas")])

    assert unmatched == []
    assert _by_name(updated, "Mike Thomas")["drafted"] is True
    assert _by_name(updated, "Michael Thomas")["drafted"] is False


def test_michael_carter_and_michael_carter_ii_do_not_collide():
    """A second real pair, and the one that makes naive suffix-stripping unsafe:
    Michael Carter (RB) and Michael Carter II (DB) were team-mates on the Jets.
    Stripping ``II`` before comparing merges two distinct people, so an exact
    (suffix-preserving) comparison has to be tried before the suffix-stripped
    one."""
    board = [_board_row(-1, "Michael Carter", "RB"), _board_row(-2, "Michael Carter II", "DB")]

    updated, unmatched = apply_picks(board, [_pick(1, "Michael Carter II")])

    assert unmatched == []
    assert _by_name(updated, "Michael Carter II")["drafted"] is True
    assert _by_name(updated, "Michael Carter")["drafted"] is False

    updated2, unmatched2 = apply_picks(board, [_pick(1, "Michael Carter")])

    assert unmatched2 == []
    assert _by_name(updated2, "Michael Carter")["drafted"] is True
    assert _by_name(updated2, "Michael Carter II")["drafted"] is False


def test_apply_picks_refuses_to_guess_when_a_name_is_ambiguous():
    """Board rows that differ only by a suffix, and a pick carrying no suffix:
    stripping suffixes makes both rows equally good. Guessing marks a player
    gone who is still on the board, so the pick comes back unmatched instead."""
    board = [_board_row(-1, "Michael Carter II", "DB"), _board_row(-2, "Michael Carter Jr.", "RB")]

    updated, unmatched = apply_picks(board, [_pick(1, "Michael Carter")])

    assert [p["overall_pick"] for p in unmatched] == [1]
    assert not any(row["drafted"] for row in updated)


def test_apply_picks_returns_unmatched_picks_rather_than_swallowing_them():
    board = [_board_row(-1, "Ja'Marr Chase")]
    picks = [_pick(1, "Ja'Marr Chase"), _pick(2, "Somebody Notonourboard")]

    updated, unmatched = apply_picks(board, picks)

    assert updated[0]["drafted"] is True
    assert unmatched == [picks[1]]


def test_apply_picks_never_mutates_its_inputs():
    board = [_board_row(-1, "Ja'Marr Chase")]
    picks = [_pick(1, "Ja'Marr Chase")]
    board_before = [dict(row) for row in board]
    picks_before = [dict(p) for p in picks]

    updated, _ = apply_picks(board, picks)

    assert board == board_before
    assert picks == picks_before
    assert updated is not board
    assert updated[0] is not board[0]


def test_apply_picks_is_idempotent():
    board = [_board_row(-1, "Ja'Marr Chase"), _board_row(-2, "Bijan Robinson", "RB")]
    picks = [_pick(1, "Ja'Marr Chase")]

    once, _ = apply_picks(board, picks)
    twice, unmatched = apply_picks(once, picks)

    assert once == twice
    assert unmatched == []


def test_apply_picks_will_not_claim_one_board_row_for_two_different_picks():
    board = [_board_row(-1, "Ja'Marr Chase")]
    picks = [_pick(1, "Ja'Marr Chase", team_id=101), _pick(2, "JaMarr Chase", team_id=102)]

    updated, unmatched = apply_picks(board, picks)

    assert updated[0]["drafted_by_team_id"] == 101
    assert [p["overall_pick"] for p in unmatched] == [2]


def test_a_second_pick_on_one_row_is_reported_even_when_the_same_team_made_both():
    """Two picks by one team landing on the same board row is a real signal --
    a missing board entry, or a name our matching cannot separate. Judging
    'same pick' by team id makes it vanish with no trace at all, which is worse
    than a plain miss: the unmatched list exists precisely to catch this."""
    board = [_board_row(-1, "Michael Carter", "RB")]
    picks = [
        _pick(5, "Michael Carter", team_id=5, seen_at="2026-09-07T12:00:00Z"),
        _pick(60, "Michael Carter II", team_id=5, seen_at="2026-09-07T13:00:00Z"),
    ]

    updated, unmatched = apply_picks(board, picks)

    assert [p["overall_pick"] for p in unmatched] == [60]
    assert updated[0]["drafted_by_team_id"] == 5
    assert updated[0]["drafted_at"] == "2026-09-07T12:00:00Z", "the later pick overwrote the row"


def test_two_hand_entered_picks_on_one_row_are_not_conflated():
    """Neither carries a pick number and neither carries a team, so a team-id
    comparison says None == None and swallows the second. This is the manual
    fallback path, the one we rely on when ESPN is unavailable."""
    board = [_board_row(-1, "Michael Carter", "RB")]
    picks = [{"player_name": "Michael Carter"}, {"player_name": "Michael Carter II"}]

    updated, unmatched = apply_picks(board, picks)

    assert unmatched == [picks[1]]
    assert updated[0]["drafted"] is True


def test_tapping_the_same_player_twice_by_hand_is_still_one_pick():
    """A double tap on the manual button is the same pick told twice, not a
    second player, so it must not raise a false alarm."""
    board = [_board_row(-1, "Michael Carter", "RB")]
    picks = [{"player_name": "Michael Carter"}, {"player_name": "Michael Carter"}]

    _updated, unmatched = apply_picks(board, picks)

    assert unmatched == []


def test_apply_picks_refuses_a_player_id_that_two_board_rows_share():
    """Stage 1 refuses an ambiguous id for the same reason stages 2 and 3 refuse
    an ambiguous name: matching the first row is a coin flip."""
    board = [_board_row(4262921, "Ja'Marr Chase"), _board_row(4262921, "Someone Else")]

    updated, unmatched = apply_picks(board, [_pick(1, "Ja'Marr Chase", player_id=4262921)])

    assert [p["overall_pick"] for p in unmatched] == [1]
    assert not any(row["drafted"] for row in updated)


def test_a_hand_entered_pick_does_not_erase_a_known_team_attribution():
    """The manual fallback knows the player and not the team. Re-applying it
    over a row ESPN already attributed must not blank the attribution."""
    board = [_board_row(-1, "Ja'Marr Chase")]
    from_espn, _ = apply_picks(board, [_pick(1, "Ja'Marr Chase", team_id=107)])

    updated, unmatched = apply_picks(from_espn, [{"player_name": "Ja'Marr Chase"}])

    assert unmatched == []
    assert updated[0]["drafted_by_team_id"] == 107
    assert updated[0]["drafted_at"] == "2026-09-07T12:00:00Z"


def test_apply_picks_handles_a_manual_pick_with_no_team_id():
    """The web page's 'they took him' fallback knows the player, not the team."""
    board = [_board_row(-1, "Ja'Marr Chase")]

    updated, unmatched = apply_picks(board, [{"player_name": "Ja'Marr Chase"}])

    assert unmatched == []
    assert updated[0]["drafted"] is True
    assert updated[0]["drafted_by_team_id"] is None


def test_apply_picks_reports_a_pick_with_no_usable_identity():
    board = [_board_row(-1, "Ja'Marr Chase")]
    picks = [{"overall_pick": 1, "team_id": 101}]

    updated, unmatched = apply_picks(board, picks)

    assert unmatched == picks
    assert updated[0]["drafted"] is False


def test_apply_picks_on_an_empty_board_returns_every_pick_unmatched():
    picks = [_pick(1, "Ja'Marr Chase")]
    assert apply_picks([], picks) == ([], picks)


# --------------------------------------------------------------------------
# roster_needs
# --------------------------------------------------------------------------

SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "D/ST": 1, "K": 1, "BE": 7}


def _player(position):
    return {"player_id": None, "name": f"a {position}", "position": position}


def test_roster_needs_on_an_empty_roster_is_every_starting_slot():
    assert roster_needs([], SLOTS) == {
        "QB": 1,
        "RB": 2,
        "WR": 2,
        "TE": 1,
        "FLEX": 1,
        "D/ST": 1,
        "K": 1,
    }


def test_flex_stays_open_when_the_last_starter_at_a_position_fills_that_position():
    """The case the brief calls out: one TE on the roster fills the TE slot, not
    the FLEX. FLEX is still open, so the roster needs one more flex-eligible
    body than a naive 'count the positions' answer would say."""
    roster = [_player("QB"), _player("RB"), _player("RB"), _player("WR"), _player("WR"),
              _player("TE")]

    needs = roster_needs(roster, SLOTS)

    assert needs["TE"] == 0
    assert needs["FLEX"] == 1
    assert needs["RB"] == 0
    assert needs["WR"] == 0


def test_a_spare_flex_eligible_player_fills_the_flex():
    roster = [_player("QB"), _player("RB"), _player("RB"), _player("RB"), _player("WR"),
              _player("WR"), _player("TE")]

    needs = roster_needs(roster, SLOTS)

    assert needs["RB"] == 0
    assert needs["FLEX"] == 0


def test_dedicated_slots_are_filled_before_the_flex():
    """Two RBs and no TE: the RBs must land in RB, leaving TE and FLEX open.
    Spending an RB on the FLEX here would understate the need by one."""
    roster = [_player("RB"), _player("RB")]

    needs = roster_needs(roster, SLOTS)

    assert needs["RB"] == 0
    assert needs["TE"] == 1
    assert needs["FLEX"] == 1


def test_a_player_who_fits_no_starting_slot_does_not_reduce_any_need():
    needs = roster_needs([_player("QB"), _player("QB")], SLOTS)
    assert needs["QB"] == 0
    assert needs["FLEX"] == 1


def test_bench_and_injured_reserve_slots_are_not_needs():
    needs = roster_needs([], {"QB": 1, "BE": 7, "IR": 2})
    assert needs == {"QB": 1}


def test_a_superflex_slot_accepts_a_quarterback():
    slots = {"QB": 1, "RB": 1, "OP": 1, "BE": 5}
    assert roster_needs([_player("QB"), _player("QB")], slots)["OP"] == 0
    assert roster_needs([_player("QB")], slots)["OP"] == 1


def test_roster_needs_does_not_mutate_its_inputs():
    roster = [_player("RB")]
    slots = dict(SLOTS)
    roster_needs(roster, slots)
    assert roster == [{"player_id": None, "name": "a RB", "position": "RB"}]
    assert slots == SLOTS


# --------------------------------------------------------------------------
# scarcity
# --------------------------------------------------------------------------


def _tiered(name, position, tier, rank, drafted=False):
    row = _board_row(-abs(hash(name)) % 100000, name, position, tier, rank)
    row["drafted"] = drafted
    return row


def test_scarcity_counts_undrafted_players_in_the_best_remaining_tiers():
    board = [
        _tiered("rb a", "RB", 1, 1),
        _tiered("rb b", "RB", 1, 2, drafted=True),
        _tiered("rb c", "RB", 2, 3),
        _tiered("rb d", "RB", 3, 4),
        _tiered("rb e", "RB", 4, 5),
        _tiered("wr a", "WR", 1, 1),
        _tiered("wr b", "WR", 1, 2),
        _tiered("wr c", "WR", 5, 3),
    ]

    assert scarcity(board) == {
        "RB": {"best_tier": 1, "count": 2},
        "WR": {"best_tier": 1, "count": 2},
    }


def test_scarcity_slides_down_as_the_best_tiers_empty():
    """Once every tier-1 RB is gone the question is how many of the *next* two
    tiers are left, not how many tier-1s are left (zero, forever)."""
    board = [
        _tiered("rb a", "RB", 1, 1, drafted=True),
        _tiered("rb b", "RB", 1, 2, drafted=True),
        _tiered("rb c", "RB", 2, 3),
        _tiered("rb d", "RB", 3, 4),
        _tiered("rb e", "RB", 4, 5),
    ]

    assert scarcity(board) == {"RB": {"best_tier": 2, "count": 2}}


def test_scarcity_says_which_tier_the_window_starts_at():
    """A count on its own cannot drive a decision: two left at tier 1 and two
    left at tier 6 call for opposite ones. The best remaining tier is half the
    answer, so it travels with the count."""
    elite = [_tiered("rb a", "RB", 1, 1), _tiered("rb b", "RB", 2, 2)]
    picked_over = [
        _tiered("rb a", "RB", 1, 1, drafted=True),
        _tiered("rb b", "RB", 2, 2, drafted=True),
        _tiered("rb c", "RB", 6, 3),
        _tiered("rb d", "RB", 7, 4),
    ]

    assert scarcity(elite)["RB"]["count"] == scarcity(picked_over)["RB"]["count"] == 2
    assert scarcity(elite)["RB"]["best_tier"] == 1
    assert scarcity(picked_over)["RB"]["best_tier"] == 6


def test_scarcity_within_one_tier():
    board = [
        _tiered("rb a", "RB", 1, 1),
        _tiered("rb b", "RB", 2, 2),
        _tiered("rb c", "RB", 2, 3),
    ]

    assert scarcity(board, within_tiers=1) == {"RB": {"best_tier": 1, "count": 1}}
    assert scarcity(board, within_tiers=2) == {"RB": {"best_tier": 1, "count": 3}}


def test_scarcity_reports_zero_for_a_position_that_is_gone():
    board = [_tiered("k a", "K", 1, 1, drafted=True), _tiered("rb a", "RB", 1, 1)]
    assert scarcity(board) == {
        "K": {"best_tier": None, "count": 0},
        "RB": {"best_tier": 1, "count": 1},
    }


def test_scarcity_reports_no_best_tier_for_a_position_with_no_tiers():
    """An untiered row sorts behind every tiered one, but it must not be
    reported as if it were the best tier in the league."""
    row = _tiered("rb a", "RB", 1, 1)
    row["tier"] = None

    assert scarcity([row]) == {"RB": {"best_tier": None, "count": 1}}


def test_scarcity_rejects_a_nonsense_tier_window():
    with pytest.raises(ValueError):
        scarcity([], within_tiers=0)


# --------------------------------------------------------------------------
# available
# --------------------------------------------------------------------------


def test_available_orders_by_tier_then_rank_and_skips_the_drafted():
    board = [
        _tiered("third", "WR", 2, 1),
        _tiered("first", "RB", 1, 4),
        _tiered("gone", "RB", 1, 1, drafted=True),
        _tiered("second", "TE", 1, 9),
    ]

    assert [row["name"] for row in available(board)] == ["first", "second", "third"]


def test_available_respects_the_limit():
    board = [_tiered(f"p{i}", "WR", 1, i) for i in range(10)]
    assert len(available(board, limit=3)) == 3


def test_available_filters_by_position():
    board = [_tiered("rb", "RB", 1, 1), _tiered("wr", "WR", 1, 2), _tiered("te", "TE", 1, 3)]
    assert [row["name"] for row in available(board, positions=["RB", "TE"])] == ["rb", "te"]


def test_available_sorts_untiered_players_last_rather_than_crashing():
    row = _tiered("no tier", "WR", 1, 1)
    row["tier"] = None
    row["rank"] = None
    board = [row, _tiered("tiered", "WR", 3, 1)]

    assert [r["name"] for r in available(board)] == ["tiered", "no tier"]


def test_available_hands_back_copies_so_the_caller_cannot_corrupt_the_board():
    board = [_tiered("rb", "RB", 1, 1)]
    result = available(board)
    result[0]["drafted"] = True
    assert board[0]["drafted"] is False


def test_available_treats_a_row_drafted_by_a_team_as_gone_even_without_the_flag():
    """A board row read straight out of SQLite has drafted_by_team_id set and no
    ``drafted`` key at all."""
    row = {"player_id": 1, "name": "gone", "position": "RB", "tier": 1, "rank": 1,
           "drafted_by_team_id": 104}
    assert available([row]) == []


def test_slot_positions_is_public_so_callers_need_not_re_derive_the_flex():
    """The advisor turns "which slots are open" into "which positions would fill
    them" for its deterministic fallback. That mapping lives here, with the rest
    of the roster knowledge, rather than being copied into a second module where
    the two could disagree about what a FLEX accepts.
    """
    assert board.slot_positions("QB") == frozenset({"QB"})
    assert board.slot_positions("flex") == frozenset({"RB", "WR", "TE"})
    assert board.slot_positions("D/ST") == frozenset({"D/ST"})
