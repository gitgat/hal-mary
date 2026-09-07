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
