"""What day it is, and how much that matters — rendered for a research prompt.

The season is live and the model's training cutoff is not. That is hard rule
six in ``CLAUDE.md``, and until this module existed it was enforced only by
asking: ``board_build.md`` said "prefer recent sources", ``news_sweep.md`` said
"a stale claim is worse than silence", and **no prompt was ever told what day it
was**. A model with no anchor treats its own cutoff as the present, and answers
about this week from last season without any of the hedging it would use if it
knew. Nothing in the output looks wrong — that is what makes it expensive.

So Python supplies the facts, the way it does everywhere else here: the date,
the two windows, and the instruction to date every claim. The prose that reasons
about football stays in ``prompts/*.md``; the arithmetic that can be tested
stays here.

The windows come from ``[research]`` in ``config.toml`` (``recency_current_days``
and ``recency_stale_days``) so they can be widened in the quiet part of the year
without editing six prompt files.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

__all__ = ["recency_block"]


def recency_block(settings: Any, *, now: datetime | None = None) -> str:
    """The block every web-enabled prompt gets, filled in for today.

    ``now`` is injectable so the tests do not depend on the day they run. It is
    rendered in the operator's local zone rather than UTC: a Sunday-morning
    lineup check is reasoning about kickoff, and "Sunday" is the word that
    matters to it.
    """
    moment = now.astimezone() if now is not None else datetime.now().astimezone()
    current = settings.research.recency_current_days
    stale = settings.research.recency_stale_days

    # No leading zero on the day: "8 September", not "08 September". A model
    # reading "08" has occasionally taken it for an ISO fragment and repeated it
    # back as a different date.
    today = f"{moment.strftime('%A')} {moment.day} {moment.strftime('%B %Y')}"

    return "\n".join(
        [
            "## What day it is, and why it decides everything",
            "",
            f"**Today is {today}.** The NFL season is live and the facts move every day.",
            "",
            "**Recency is the main filter on your research, not a tie-breaker.**",
            "",
            f"- Published in the last {current} day(s): current. Use it.",
            f"- Older than {current} day(s): background. It may still be true and it may",
            "  have been overtaken this morning. Say when it is from.",
            f"- Older than {stale} day(s): it must not decide a ranking, a start or a",
            "  claim on its own. Find something newer, or say plainly that you could not.",
            "- A preseason or training-camp report is background by definition once the",
            "  season has started, however confident it sounds.",
            "",
            "**Your own training data is not a source.** You do not know what happened",
            "this week; the web tools do. If the two disagree, the web is right and you",
            "are out of date. Never answer a question about a current injury, depth",
            "chart or role from memory — look it up, even when you are sure.",
            "",
            "**Date every claim.** When you write that a player is hurt, has lost his job",
            "or has changed teams, say when that was reported. An undated claim cannot be",
            "checked for staleness later, by you or by anyone reading the note in a week.",
            "",
            "When two sources disagree, prefer the more recent one and say that is why.",
        ]
    )
