"""Loading and filling the Markdown prompt files.

Football reasoning lives in ``prompts/*.md``, not in Python: Bryan and Caroline
can change how hal-mary thinks about a draft without touching code, and the
strategy is reviewable as prose. Python's job is to fill in the facts — how many
teams, which picks are hers, what the board says — and to be loud when it cannot.

Placeholders are ``{{name}}``. Deliberately not ``str.format``: a prompt is full
of prose braces and JSON examples, and ``format`` would explode on the first one.
An unfilled placeholder raises rather than reaching the model as literal
``{{team_count}}``, because a prompt that silently says "there are {{team_count}}
teams" produces advice for a league that does not exist.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

__all__ = ["PromptError", "load_prompt", "render", "render_prompt"]

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class PromptError(RuntimeError):
    """A prompt file is missing, empty, or has a placeholder nobody filled in."""


def load_prompt(settings: Any, name: str) -> str:
    """Read ``<prompts_dir>/<name>`` fresh, never cached.

    Fresh on every call for the same reason standing memory is: these files are
    edited while the service runs, and an advisor still using the version that
    was on disk when the process started is a bug nobody would find for days.
    """
    path = Path(settings.paths.prompts_dir).expanduser() / name
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptError(f"prompt file {path} could not be read: {exc}") from exc
    if not text.strip():
        raise PromptError(f"prompt file {path} is empty")
    return text


def render(template: str, values: dict[str, Any]) -> str:
    """Substitute ``{{name}}`` placeholders; raise if any are left over."""
    filled = _PLACEHOLDER.sub(
        lambda match: str(values[match.group(1)]) if match.group(1) in values else match.group(0),
        template,
    )
    missing = sorted({match.group(1) for match in _PLACEHOLDER.finditer(filled)})
    if missing:
        raise PromptError(f"prompt has unfilled placeholders: {', '.join(missing)}")
    return filled


def render_prompt(settings: Any, name: str, values: dict[str, Any]) -> str:
    """:func:`load_prompt` then :func:`render`."""
    return render(load_prompt(settings, name), values)
