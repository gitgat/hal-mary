"""A read-only prompt may not tell the model something it can disprove.

The MCP endpoint exposes **all** tools to anyone holding `MCP_TOKEN`. There is
no per-task or per-caller gating: `cowork.py`'s `ACTING_TOOLS` check runs when
`tasks.toml` is *loaded*, and refuses to let a read-only task *declare* an
acting tool — it does not stop the server offering one at runtime.

So a Cowork session running a read-only task comes up holding `pending_actions`
and `report_action` regardless. A prompt asserting "there is no tool here that
can change the roster" is therefore falsifiable with one `list_tools` call, and
a model that catches an instruction lying to it has no reason to trust the rest
of that instruction — which is the exact failure the wording was meant to avoid.

The fix is not a better lie. It is to instruct rather than assert: *do not call
them* is true whatever the session is holding.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TASKS = tomllib.loads((REPO / "cowork" / "tasks.toml").read_text(encoding="utf-8"))
READ_ONLY = [t for t in TASKS.get("task", []) if t.get("mode") == "read_only"]

#: Claims about what the session *has*. Each is checkable by the model, and each
#: is false while the server exposes every tool to every caller.
FALSIFIABLE = (
    "there is no tool here",
    "only tools that read",
    "you have no tool",
    "cannot change",
    "are incapable",
)


def test_there_are_read_only_tasks_to_check():
    """Guard the guard: a rename must not turn this file into a no-op."""
    assert READ_ONLY, "no read_only tasks found — has `mode` been renamed?"


@pytest.mark.parametrize("task", READ_ONLY, ids=lambda t: t["name"])
def test_a_read_only_prompt_makes_no_claim_the_model_can_disprove(task):
    prompt = task["prompt"].lower()

    found = [phrase for phrase in FALSIFIABLE if phrase in prompt]
    assert not found, (
        f"{task['name']} claims {found} — but the MCP server hands every caller "
        "every tool, so the session really is holding pending_actions and "
        "report_action. Instruct it not to call them instead of telling it they "
        "are not there."
    )


@pytest.mark.parametrize("task", READ_ONLY, ids=lambda t: t["name"])
def test_a_read_only_prompt_still_forbids_acting(task):
    """Dropping the false claim must not drop the instruction with it."""
    prompt = task["prompt"].lower()

    assert "do not change anything" in prompt, f"{task['name']} no longer forbids changing anything"
    assert re.search(r"do not call|do not use", prompt), (
        f"{task['name']} does not tell the run to leave the acting tools alone. "
        "The prompt is the only thing standing between a read-only run and a "
        "tool the server will happily let it call."
    )
