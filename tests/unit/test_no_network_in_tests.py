"""The guard that makes every other test's isolation claim true.

`tests/conftest.py` blocks both HTTP stacks at their real transport. If that
guard ever stops working, a mocking mistake somewhere else in the suite starts
quietly talking to ESPN — passing until the day the box has no internet, or
until a test starts depending on the state of Caroline's real league.
"""

import httpx
import pytest
import requests


def test_httpx_cannot_reach_the_network():
    with pytest.raises(AssertionError, match="real network"):
        httpx.get("https://lm-api-reads.fantasy.espn.com/")


def test_requests_cannot_reach_the_network():
    """espn_api uses requests, so blocking httpx alone would not be enough."""
    with pytest.raises(AssertionError, match="real network"):
        requests.get("https://lm-api-reads.fantasy.espn.com/")


def test_httpx2_cannot_reach_the_network():
    """The `mcp` SDK brings its own HTTP stack, and it is a third door.

    Nothing in hal-mary makes an outbound call through it today, so this hole was
    latent rather than open — but CLAUDE.md says the guard blocks every HTTP
    stack in play, and a latent hole that the documentation denies is exactly the
    kind that gets found the hard way.
    """
    import httpx2

    with pytest.raises(AssertionError, match="real network"):
        httpx2.get("https://lm-api-reads.fantasy.espn.com/")
