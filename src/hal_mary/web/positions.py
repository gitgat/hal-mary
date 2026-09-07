"""Roster slots and positions, in the words Caroline actually uses.

One copy, shared by the team page and the draft page. Two copies is how the
draft page ends up calling ``RB/WR/TE`` a "Flex" while the team page calls it
something else, and both of them are names for a thing that appears on her
screen under a third name entirely.

The rule these tables encode: **no bare position code is ever the only label on
anything.** "QB" is not a word she has a reason to know, and a heading that says
"QB (0 open)" is a heading she has to decode before she can read the page. Codes
still appear next to a player's name — ESPN shows them, so they are worth
recognising — but never as the only thing naming a group.
"""

from __future__ import annotations

__all__ = [
    "POSITION_PLURALS",
    "POSITION_WORDS",
    "SLOT_LABELS",
    "SLOT_ORDER",
    "position_plural",
    "position_word",
    "slot_label",
    "slot_sort_key",
]

#: Lineup slots in the order a roster is read, not the order ESPN returns them.
#: Anything ESPN sends that is not listed here sorts to the end, so an
#: unfamiliar slot shows up rather than vanishing.
SLOT_ORDER = ("QB", "RB", "WR", "TE", "RB/WR/TE", "WR/TE", "OP", "D/ST", "K", "BE", "IR")

#: Slot names as she would say them.
#:
#: The flex slot is spelled ``RB/WR/TE`` in this league, and "Flex" is a term
#: that appears nowhere on her ESPN screen — so the label says what the slot
#: actually accepts instead of naming a concept she would then have to look up.
SLOT_LABELS = {
    "QB": "Quarterback",
    "RB": "Running back",
    "WR": "Wide receiver",
    "TE": "Tight end",
    "RB/WR/TE": "Spare running back, receiver or tight end",
    "WR/TE": "Spare receiver or tight end",
    "OP": "Spare attacking player",
    "D/ST": "Defense",
    "K": "Kicker",
    "BE": "Bench",
    "IR": "Injured reserve",
}

#: One player, in words. Used beside a name and inside a sentence.
POSITION_WORDS = {
    "QB": "quarterback",
    "RB": "running back",
    "WR": "wide receiver",
    "TE": "tight end",
    "K": "kicker",
    "D/ST": "defense",
    "DL": "defensive lineman",
    "LB": "linebacker",
    "DB": "defensive back",
}

#: A group of them, for a heading or a filter button.
POSITION_PLURALS = {
    "QB": "Quarterbacks",
    "RB": "Running backs",
    "WR": "Wide receivers",
    "TE": "Tight ends",
    "K": "Kickers",
    "D/ST": "Defenses",
    "DL": "Defensive linemen",
    "LB": "Linebackers",
    "DB": "Defensive backs",
}


def slot_label(slot: str) -> str:
    """The heading for a roster slot. Falls back to the slot's own name."""
    return SLOT_LABELS.get(slot, slot)


def position_word(code: str | None) -> str:
    """One player's position in words, or the raw code when we have no word.

    A code we cannot translate is shown as-is rather than hidden: an unfamiliar
    label she can look up beats a blank where a position should be.
    """
    if not code:
        return ""
    return POSITION_WORDS.get(code.upper(), code)


def position_plural(code: str | None) -> str:
    if not code:
        return ""
    return POSITION_PLURALS.get(code.upper(), code)


def slot_sort_key(slot: str) -> tuple[int, str]:
    return (SLOT_ORDER.index(slot) if slot in SLOT_ORDER else len(SLOT_ORDER), slot)
