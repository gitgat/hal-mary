"""Rewrite the prompt blocks in ``docs/COWORK.md`` from ``cowork/tasks.toml``.

A Cowork scheduled task is a saved prompt on a cadence, so the prompt *is* the
cron job. It lives in ``cowork/tasks.toml`` as data, and it also has to appear in
the document Bryan pastes from — two copies of the same load-bearing text, which
is exactly the pair that drifts.

So the document owns the prose and this script owns the prompts. Each block in
the document is fenced by ``<!-- prompt:NAME -->`` markers, and everything
between a pair is replaced by that task's prompt, verbatim. Idempotent: running
it on an up-to-date document changes nothing, which is what
``tests/unit/test_cowork.py`` asserts rather than trusting anyone to remember.

    uv run python scripts/render_cowork_doc.py            # rewrite in place
    uv run python scripts/render_cowork_doc.py --check    # nonzero if stale
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS_FILE = REPO_ROOT / "cowork" / "tasks.toml"
DOC_FILE = REPO_ROOT / "docs" / "COWORK.md"

_BLOCK = re.compile(
    r"(?P<open><!-- prompt:(?P<name>[a-z0-9-]+) -->\n).*?(?P<close>\n<!-- /prompt:(?P=name) -->)",
    re.DOTALL,
)


def prompts(tasks_file: Path = TASKS_FILE) -> dict[str, str]:
    raw = tomllib.loads(tasks_file.read_text(encoding="utf-8"))
    return {task["name"]: task["prompt"].strip() for task in raw["task"]}


def render(doc: str, by_name: dict[str, str]) -> str:
    """Replace every marked block with its task's prompt."""

    def swap(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in by_name:
            raise SystemExit(f"{DOC_FILE.name} marks a prompt block for unknown task {name!r}")
        return f"{match.group('open')}```text\n{by_name[name]}\n```{match.group('close')}"

    rendered, count = _BLOCK.subn(swap, doc)
    if not count:
        raise SystemExit(f"{DOC_FILE.name} has no <!-- prompt:NAME --> blocks to fill in")
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit nonzero if the doc is stale")
    args = parser.parse_args(argv)

    current = DOC_FILE.read_text(encoding="utf-8")
    rendered = render(current, prompts())
    if rendered == current:
        return 0
    if args.check:
        print(
            f"{DOC_FILE} is out of date with {TASKS_FILE}; "
            "run `uv run python scripts/render_cowork_doc.py`",
            file=sys.stderr,
        )
        return 1
    DOC_FILE.write_text(rendered, encoding="utf-8")
    print(f"rewrote the prompt blocks in {DOC_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
