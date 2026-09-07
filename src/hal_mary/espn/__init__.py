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

Nothing in this package ever writes to ESPN.
"""

from hal_mary.espn.client import (
    EspnAuthError,
    EspnClient,
    EspnError,
    EspnLeagueNotFound,
    EspnUnavailable,
)
from hal_mary.espn.sync import (
    last_sync,
    sync_draft,
    sync_league,
    sync_players,
    write_league_memory,
)

__all__ = [
    "EspnAuthError",
    "EspnClient",
    "EspnError",
    "EspnLeagueNotFound",
    "EspnUnavailable",
    "last_sync",
    "sync_draft",
    "sync_league",
    "sync_players",
    "write_league_memory",
]
