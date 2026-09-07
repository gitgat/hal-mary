"""One test that actually spends money, and only when asked to.

Everything else in the suite runs against ``tests/fake_claude/claude``, which
proves the argv, the parsing and the bookkeeping but cannot prove the flags are
still the flags the real CLI accepts. A CLI upgrade that renames
``--setting-sources`` would sail past the unit tests and cost $0.82 a call in
production. This is the test that notices.

It is skipped unless ``HAL_MARY_LIVE`` is set to something meaning yes and the
configured binary exists, so ``uv run pytest`` on a laptop never spends
anything::

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

#: Values of HAL_MARY_LIVE that mean "no". Testing truthiness alone would make
#: HAL_MARY_LIVE=0 and HAL_MARY_LIVE=false spend money, which is the opposite of
#: what someone writing either of those means.
_OFF = {"", "0", "false", "no", "off"}


def _live_enabled() -> bool:
    return os.environ.get("HAL_MARY_LIVE", "").strip().lower() not in _OFF


@pytest.mark.skipif(not _live_enabled(), reason="HAL_MARY_LIVE is not set to an on value")
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
    # The ceiling has to sit below the cheapest *partial* isolation, not below
    # the $0.82 of no isolation at all. Measured on this box: no flags $0.82,
    # MCP flags only $0.048, all three $0.005 (and $0.0017 for this prompt,
    # which has no json-schema). A ceiling of $0.10 would have let a renamed
    # --setting-sources through at $0.048 — passing the one test written to
    # catch it. $0.015 is ~9x headroom over the measurement for pricing drift
    # and still fails the moment isolation breaks.
    assert row["cost_usd"] is not None and row["cost_usd"] < 0.015, (
        f"call cost ${row['cost_usd']}, over the isolation ceiling: a flag has "
        f"probably been renamed or dropped. Isolated calls cost ~$0.002 here; "
        f"$0.048 means --setting-sources stopped working, $0.82 means none of "
        f"the flags are."
    )
    conn.close()
