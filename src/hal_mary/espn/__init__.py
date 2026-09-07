"""Reading Caroline's ESPN league.

Two paths, for one recorded reason (``docs/DECISIONS.md``):

* **Draft picks come from the raw ``mDraftDetail`` endpoint**, read with
  ``httpx`` in :mod:`hal_mary.espn.client`. The ``espn_api`` library cannot be
  used for live polling: ``refresh_draft()`` appends to a list that is only
  cleared in the constructor, and ``_fetch_draft`` returns early unless
  ``draftDetail.drafted`` is true — a flag ESPN may only set once the draft is
  over, which is the one moment we no longer care.
* **Everything else comes from the library**, which handles ESPN's slot ids,
  pro-team ids and player payloads so we do not have to.

One rule about that raw draft payload is worth knowing before reading any of
this: **ESPN pre-populates the entire draft board before the draft starts**, so
``draftDetail.picks`` is full of empty slots. ``EspnClient.draft_picks`` returns
only slots a player has actually been drafted into and
``EspnClient.draft_schedule`` returns all of them, made or not, as the pick
schedule. :func:`hal_mary.espn.client.pick_is_made` is the single definition.

Nothing in this package ever writes to ESPN.
"""

from hal_mary.espn.client import (
    UNMADE_PLAYER_ID,
    EspnAuthError,
    EspnClient,
    EspnError,
    EspnLeagueNotFound,
    EspnUnavailable,
    pick_is_made,
)
from hal_mary.espn.sync import (
    last_sync,
    sync_draft,
    sync_league,
    sync_players,
    write_league_memory,
)

__all__ = [
    "UNMADE_PLAYER_ID",
    "EspnAuthError",
    "EspnClient",
    "EspnError",
    "EspnLeagueNotFound",
    "EspnUnavailable",
    "last_sync",
    "pick_is_made",
    "sync_draft",
    "sync_league",
    "sync_players",
    "write_league_memory",
]
