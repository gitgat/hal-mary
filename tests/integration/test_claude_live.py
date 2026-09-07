"""One test that actually spends money, and only when asked to.

Everything else in the suite runs against ``tests/fake_claude/claude``, which
proves the argv, the parsing and the bookkeeping but cannot prove the flags are
still the flags the real CLI accepts. A CLI upgrade that renames
``--setting-sources`` would sail past the unit tests and cost $0.82 a call in
production. This is the test that notices.

It is skipped unless ``HAL_MARY_LIVE`` is set and the configured binary exists,
so ``uv run pytest`` on a laptop never spends anything::

    HAL_MARY_LIVE=1 uv run pytest -m integration
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from hal_mary import db
from hal_mary.claude_runner import ClaudeRunner
from hal_mary.config import load_settings

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not os.environ.get("HAL_MARY_LIVE"), reason="HAL_MARY_LIVE is not set")
def test_real_claude_answers_under_the_isolation_flags(tmp_path: Path):
    settings = load_settings(REPO_ROOT / "config.toml", env={})
    if shutil.which(settings.claude.binary) is None:
        pytest.skip(f"{settings.claude.binary} is not on PATH")

    # A tools-off job with a one-word answer: the cheapest end-to-end proof that
    # the flags are accepted and the stream still parses.
    settings = settings.model_copy(
        update={
            "claude": settings.claude.model_copy(
                update={"scratch_dir": str(tmp_path / "scratch")}
            )
        }
    )
    conn = db.connect(tmp_path / "hal.db")
    db.migrate(conn)

    result = ClaudeRunner(settings, conn).run(
        "draft_advice",
        "Reply with the single word OK and nothing else.",
    )

    assert result.ok, result.error
    assert "OK" in result.text.upper()
    assert result.session_id
    assert result.raw_path is not None and result.raw_path.is_file()

    row = conn.execute("SELECT * FROM claude_calls").fetchone()
    assert row["exit_code"] == 0
    # The whole point of the flags: an inherited environment cost $0.82 here.
    assert row["cost_usd"] is not None and row["cost_usd"] < 0.10, (
        f"call cost ${row['cost_usd']}: the isolation flags are not working"
    )
    conn.close()
