"""The chat page renders Markdown, and renders it as *ours*, never as markup.

A chat answer is written by a model that has just read pages off the internet,
so its text is untrusted in the same way a leaguemate's team name is. The
renderer therefore escapes first and only then introduces its own tags: nothing
in the input can become an element, an attribute, or a URL scheme we did not
choose. Every test here asserts on the **whole** rendered string, because a test
that asserts on a slice reads as though it checked everything and checks only
the half its author was thinking about.
"""

from __future__ import annotations

import pytest

from hal_mary.markdown_safe import render


def test_plain_text_is_a_paragraph():
    assert render("Start Gibbs this week.") == "<p>Start Gibbs this week.</p>"


def test_blank_lines_separate_paragraphs():
    assert render("First.\n\nSecond.") == "<p>First.</p>\n<p>Second.</p>"


def test_a_single_newline_is_a_line_break_not_a_new_paragraph():
    assert render("One\nTwo") == "<p>One<br>Two</p>"


def test_bold_and_italic():
    assert render("**Bench** him, *maybe*.") == (
        "<p><strong>Bench</strong> him, <em>maybe</em>.</p>"
    )


def test_bullets_become_a_list():
    assert render("- Gibbs\n- Chase") == "<ul>\n<li>Gibbs</li>\n<li>Chase</li>\n</ul>"


def test_numbered_lists_become_an_ordered_list():
    assert render("1. Gibbs\n2. Chase") == "<ol>\n<li>Gibbs</li>\n<li>Chase</li>\n</ol>"


def test_headings():
    assert render("## Who to start") == "<h3>Who to start</h3>"


def test_inline_code():
    assert render("Run `hal-mary sync` first.") == (
        "<p>Run <code>hal-mary sync</code> first.</p>"
    )


def test_fenced_code_block():
    assert render("```\nuv run pytest\n```") == "<pre><code>uv run pytest</code></pre>"


# --- the boundary ------------------------------------------------------------


def test_html_in_the_answer_is_shown_not_run():
    assert render("<script>alert(1)</script>") == (
        "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"
    )


def test_an_img_onerror_payload_is_inert():
    assert render('<img src=x onerror="alert(1)">') == (
        '<p>&lt;img src=x onerror=&#34;alert(1)&#34;&gt;</p>'
    )


def test_a_link_to_the_web_is_kept():
    assert render("[ESPN](https://espn.com/x)") == (
        '<p><a href="https://espn.com/x" rel="nofollow noopener" '
        'target="_blank">ESPN</a></p>'
    )


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "vbscript:msgbox",
        "  javascript:alert(1)",
    ],
)
def test_a_link_with_a_dangerous_scheme_keeps_its_words_and_loses_its_link(url):
    """The words survive; the scheme never becomes an href."""
    out = render(f"[click me]({url})")
    assert "href" not in out, out
    assert "<a" not in out, out
    assert "click me" in out, out


def test_a_bare_dangerous_url_is_not_linkified():
    out = render("javascript:alert(1)")
    assert out == "<p>javascript:alert(1)</p>"


def test_markdown_cannot_forge_an_attribute_on_our_own_tag():
    out = render('**bold" onmouseover="alert(1)**')
    assert 'onmouseover="alert(1)"' not in out, out
    assert out == '<p><strong>bold&#34; onmouseover=&#34;alert(1)</strong></p>'
