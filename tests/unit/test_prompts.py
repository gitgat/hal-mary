"""Tests for filling in the Markdown prompt files.

The interesting case is a *value* that looks like a placeholder. Every caller
before the chat page filled these in from the database — a team count, a pick
number, a player's name — so nothing a person typed had ever reached
:func:`render`. The chat page passes Caroline's own question straight through,
and a question is allowed to contain any characters at all.
"""

from __future__ import annotations

import pytest

from hal_mary import prompts


def test_placeholders_are_substituted():
    assert prompts.render("There are {{team_count}} teams.", {"team_count": 6}) == (
        "There are 6 teams."
    )


def test_a_placeholder_nobody_filled_in_is_an_error():
    """A prompt that silently said "{{team_count}} teams" would advise a league
    that does not exist."""
    with pytest.raises(prompts.PromptError) as excinfo:
        prompts.render("There are {{team_count}} teams.", {})
    assert "team_count" in str(excinfo.value)


def test_a_value_that_looks_like_a_placeholder_is_left_alone():
    """The template is what gets checked, not the result of filling it in.

    Scanning the filled text meant a value containing ``{{...}}`` was read back
    as an unfilled placeholder, so the call raised — and raised identically
    every time she retyped it, with nothing saying which characters did it.
    """
    filled = prompts.render(
        "## Her question\n\n{{question}}\n", {"question": "What does {{PPR}} mean?"}
    )
    assert filled == "## Her question\n\nWhat does {{PPR}} mean?\n"


def test_a_value_that_names_a_real_placeholder_is_still_not_substituted():
    """Substitution happens once. A value is a value, not more template."""
    filled = prompts.render(
        "{{question}} — there are {{team_count}} teams.",
        {"question": "Why {{team_count}}?", "team_count": 6},
    )
    assert filled == "Why {{team_count}}? — there are 6 teams."
