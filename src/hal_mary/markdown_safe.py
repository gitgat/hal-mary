"""Render a small, fixed subset of Markdown to HTML that is safe by construction.

A chat answer is written by a model that has just read pages off the open
internet, so it is untrusted input in exactly the way a leaguemate's team name
is (see ``hal_mary.prompt_text``). The rule here is the same one that boundary
learned: **escape first, then introduce our own tags.** Nothing that arrives in
the text can become an element, an attribute, or a URL scheme, because by the
time any tag is added every ``<``, ``>``, ``&``, ``"`` and ``'`` in the input is
already a character reference.

That ordering is the whole design. A renderer that emits tags and sanitises
afterwards has to be right about every payload anybody will ever write; this one
has to be right about the handful of patterns it chooses to recognise.

Deliberately not supported: raw HTML pass-through, images, tables, block quotes,
reference links. Anything unrecognised stays literal text, which is the failure
mode we want — a stray asterisk looks silly, a working ``onerror=`` does not.
"""

from __future__ import annotations

import re

from markupsafe import Markup, escape

#: Only these may become a link. Everything else keeps its words and loses its
#: href — `javascript:`, `data:` and `vbscript:` are the ones that matter, but
#: this is an allowlist so an unlisted scheme fails closed.
_SAFE_URL = re.compile(r"^https?://[^\s\"'<>]+$")

_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_CODE_SPAN = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", re.DOTALL)
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^(#{1,5})\s+(.*)$")
_BULLET = re.compile(r"^[-*]\s+(.*)$")
_NUMBERED = re.compile(r"^\d+\.\s+(.*)$")


def _inline(text: str) -> str:
    """Inline markers, applied to text that is *already escaped*."""
    held: list[str] = []

    def hold(html: str) -> str:
        held.append(html)
        return f"\x00{len(held) - 1}\x00"

    # Code spans first, and held aside: a backtick span is literal, so emphasis
    # markers inside it are characters rather than markup.
    text = _CODE_SPAN.sub(lambda m: hold(f"<code>{m.group(1)}</code>"), text)

    def link(m: re.Match[str]) -> str:
        label, url = m.group(1), m.group(2)
        if not _SAFE_URL.match(url):
            # Keep the words exactly as written. This is the whole defence
            # against `[click](javascript:...)`.
            return m.group(0)
        return hold(f'<a href="{url}" rel="nofollow noopener" target="_blank">{label}</a>')

    text = _LINK.sub(link, text)
    text = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", text)
    text = _ITALIC.sub(lambda m: f"<em>{m.group(1)}</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: held[int(m.group(1))], text)


def _block(raw: str) -> str:
    lines = raw.split("\n")

    if all(_BULLET.match(line) for line in lines):
        items = "\n".join(f"<li>{_inline(_BULLET.match(x).group(1))}</li>" for x in lines)
        return f"<ul>\n{items}\n</ul>"

    if all(_NUMBERED.match(line) for line in lines):
        items = "\n".join(f"<li>{_inline(_NUMBERED.match(x).group(1))}</li>" for x in lines)
        return f"<ol>\n{items}\n</ol>"

    if len(lines) == 1 and (m := _HEADING.match(lines[0])):
        # One deeper than written: the page already owns <h1>/<h2>, and a chat
        # answer must not outrank the heading of the card it sits in.
        level = min(len(m.group(1)) + 1, 6)
        return f"<h{level}>{_inline(m.group(2))}</h{level}>"

    return "<p>" + _inline("<br>".join(lines)) + "</p>"


def render(text: str) -> Markup:
    """Render ``text`` as Markup holding only tags this module chose to emit."""
    if not text or not text.strip():
        return Markup("")

    # Escape once, at the top, before a single tag exists.
    escaped = str(escape(text)).replace("\r\n", "\n").replace("\r", "\n")

    held: list[str] = []

    def fence(m: re.Match[str]) -> str:
        held.append(f"<pre><code>{m.group(1).rstrip()}</code></pre>")
        return f"\n\n\x01{len(held) - 1}\x01\n\n"

    escaped = _FENCE.sub(fence, escaped)

    out: list[str] = []
    for chunk in re.split(r"\n\s*\n", escaped):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        if (m := re.fullmatch(r"\x01(\d+)\x01", chunk.strip())):
            out.append(held[int(m.group(1))])
        else:
            out.append(_block(chunk))
    return Markup("\n".join(out))
