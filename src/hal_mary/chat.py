"""The chat page's engine: sessions, the prompt, and the streamed reply.

This is the part of hal-mary that answers the question a dashboard cannot
anticipate. "Is this trade good?" "What does PPR mean?" "Why did you tell me to
bench him?" Everything else in this application decides what to say and then says
it; this waits to be asked.

Three things shape the module.

**She must never have to explain her own team.** Every question goes out with a
compact state-of-the-team block: her league's rules, who is on her roster, which
starting spots are still empty, where the draft got to, and the last few things
hal-mary told her. It is assembled from the database on every call and it never
raises — a database in any state still produces an answer, because a chat box
that returns an error when the league has not synced is a chat box she stops
opening.

**The conversation continues.** ``chat_sessions.claude_session_id`` holds the
CLI's own session id, and the next message resumes it, so "why did you say that?"
has something to refer back to. That is also why chat is the one caller that
passes ``persist_session=True`` to the runner: every scheduled job is one-shot
and tells the CLI to keep nothing, and a session the CLI discarded cannot be
resumed. A resume that fails clears the stored id, so one bad session costs one
message rather than every message after it.

**Nothing ends in silence.** :func:`send` is a generator, and the caller is an
HTTP response that can vanish mid-answer. Every exit — the reply finishing, the
call failing, the runner raising, the browser closing — leaves either a message
in ``chat_messages`` or an unanswered question that the page will offer to ask
again. A blank bubble is the one outcome this module does not allow.

The retrieved notes and the standing memory come through
:func:`hal_mary.memory.build_context` and travel to the model as
``extra_context``, never glued onto the prompt: the runner owns how the two are
assembled, and that boundary is what keeps browser-sourced note text quarantined
in its own labelled section instead of reading as instruction.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from hal_mary import db, memory, prompts
from hal_mary.claude_runner import StreamChunk
from hal_mary.config import Settings
from hal_mary.draft import store
from hal_mary.draft.board import roster_needs
from hal_mary.league import LeagueContext, load_league_context
from hal_mary.recency import recency_block

# Slot and position wording lives in exactly one place, and this is a reader of
# it rather than a second copy. web/positions.py imports nothing from the web
# stack; the package it sits in costs no FastAPI import.
from hal_mary.web.positions import SLOT_LABELS, position_word, slot_sort_key

__all__ = [
    "JOB_NAME",
    "PROMPT_FILE",
    "ROLE_ASSISTANT",
    "ROLE_USER",
    "answer",
    "build_prompt",
    "get_messages",
    "get_session",
    "list_sessions",
    "newest_session",
    "pending_question",
    "record_question",
    "remember",
    "send",
    "start_session",
    "state_sections",
]

log = logging.getLogger(__name__)

#: The configured job. Its tool list is the one in hal-mary with web search on:
#: this is the only path where a live lookup is both affordable and the point.
JOB_NAME = "chat"
PROMPT_FILE = "chat.md"

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

#: How much of her first question becomes the conversation's name in the list.
TITLE_LENGTH = 60

#: What a session that answered nothing at all still says. Reachable when the
#: CLI exits cleanly having produced no text — rare, and indistinguishable from
#: a hung page unless it is said out loud.
EMPTY_REPLY = (
    "I did not manage to write anything that time, and I do not know why. "
    "Ask me again."
)


# --- sessions ----------------------------------------------------------------


def start_session(conn: sqlite3.Connection, title: str | None = None) -> int:
    """Open a conversation and return its id.

    ``title`` is optional because the useful one is her first question, which
    has not been asked yet. :func:`record_question` fills it in.
    """
    now = db.utc_now()
    cleaned = (title or "").strip() or None
    cur = conn.execute(
        "INSERT INTO chat_sessions (claude_session_id, title, created_at, updated_at)"
        " VALUES (NULL, ?, ?, ?)",
        (cleaned, now, now),
    )
    session_id = cur.lastrowid
    if session_id is None:  # pragma: no cover - sqlite always reports a rowid
        raise RuntimeError("sqlite did not report a row id for the new chat session")
    return int(session_id)


def list_sessions(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """Conversations, most recently touched first.

    Ordered by ``updated_at`` rather than ``created_at`` so a conversation she
    came back to this morning is not buried under one she abandoned this
    afternoon. ``id DESC`` breaks the tie, because two sessions opened in the
    same second are otherwise in an order that changes between reads.
    """
    return _rows(
        conn,
        "SELECT id, claude_session_id, title, created_at, updated_at FROM chat_sessions"
        " ORDER BY COALESCE(updated_at, created_at) DESC, id DESC LIMIT ?",
        (max(int(limit), 0),),
    )


def get_session(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row | None:
    return _one(conn, "SELECT * FROM chat_sessions WHERE id = ?", (session_id,))


def newest_session(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The conversation the page opens on when she has not named one."""
    sessions = list_sessions(conn, limit=1)
    return sessions[0] if sessions else None


def get_messages(conn: sqlite3.Connection, session_id: int) -> list[sqlite3.Row]:
    """Every message in one conversation, oldest first."""
    return _rows(
        conn,
        "SELECT id, session_id, role, content, created_at FROM chat_messages"
        " WHERE session_id = ? ORDER BY id",
        (session_id,),
    )


def record_question(conn: sqlite3.Connection, session_id: int, text: str) -> int:
    """Persist Caroline's message and return its row id.

    Separate from :func:`answer` because the web app persists the question on the
    POST and streams the reply from a later GET: the question has to survive the
    gap between the two, and a page reloaded in that gap has to be able to see
    that it was asked.

    Names the conversation from the first question when it has no name. A list
    of "Chat 1, Chat 2" is a list she cannot find anything in.
    """
    question = (text or "").strip()
    if not question:
        raise ValueError("a chat message with no text in it")

    now = db.utc_now()
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, created_at)"
            " VALUES (?, ?, ?, ?)",
            (session_id, ROLE_USER, question, now),
        )
        conn.execute(
            "UPDATE chat_sessions"
            " SET updated_at = ?, title = COALESCE(NULLIF(title, ''), ?)"
            " WHERE id = ?",
            (now, _title_from(question), session_id),
        )
    message_id = cur.lastrowid
    if message_id is None:  # pragma: no cover - sqlite always reports a rowid
        raise RuntimeError("sqlite did not report a row id for the new chat message")
    return int(message_id)


def pending_question(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row | None:
    """Her newest message, if nothing has answered it yet.

    This is the whole handover between the POST that records a question and the
    stream that answers it, and it is deliberately derived rather than stored: a
    flag would need clearing on four different failure paths, and the one that
    got missed would leave a conversation permanently convinced it owed a reply.
    """
    row = _one(
        conn,
        "SELECT id, session_id, role, content, created_at FROM chat_messages"
        " WHERE session_id = ? ORDER BY id DESC LIMIT 1",
        (session_id,),
    )
    return row if row is not None and row["role"] == ROLE_USER else None


# --- asking ------------------------------------------------------------------


def send(
    conn: sqlite3.Connection,
    settings: Settings,
    runner: Any,
    session_id: int,
    user_text: str,
) -> Iterator[StreamChunk]:
    """Record ``user_text`` and return the stream of the reply.

    Not itself a generator: the question must be persisted when this is *called*,
    not on the first ``next()``. A caller that builds the iterator and then
    abandons it — a browser that closed between the two — has still asked, and
    the page has to be able to see the question and offer to ask it again.

    Raises ``ValueError`` for an empty message; every other failure arrives as a
    message in the conversation.
    """
    record_question(conn, session_id, user_text)
    return answer(conn, settings, runner, session_id, user_text)


def answer(
    conn: sqlite3.Connection,
    settings: Settings,
    runner: Any,
    session_id: int,
    question: str,
) -> Iterator[StreamChunk]:
    """Stream one reply to ``question`` and persist it however it ends.

    **Runs entirely on the thread that owns ``conn``.** ``ClaudeRunner.stream``
    is a blocking generator that finishes by writing a ``claude_calls`` row, and
    a ``sqlite3.Connection`` belongs to the thread that opened it — so the web
    app opens the connection, builds the runner and drains this inside one
    threadpool call rather than hopping threads per chunk.
    """
    session = get_session(conn, session_id)
    resume = session["claude_session_id"] if session is not None else None
    parts: list[str] = []
    settled = False

    try:
        prompt, context = build_prompt(conn, settings, question)
        stream = runner.stream(
            JOB_NAME,
            prompt,
            resume=resume,
            # Chat is the one conversation in hal-mary, so it is the one caller
            # that asks the CLI to keep its session. See the runner's build_argv.
            persist_session=True,
            extra_context=context,
        )
    except Exception as exc:
        log.exception("the chat call could not be started")
        settled = True
        yield from _fail(conn, session_id, parts, _describe(exc), forget=bool(resume))
        return

    try:
        with contextlib.closing(stream):
            for chunk in stream:
                if chunk.kind == "text":
                    if chunk.text:
                        parts.append(chunk.text)
                        yield chunk
                    continue
                settled = True
                result = chunk.result
                if result is not None and result.ok:
                    _persist_reply(
                        conn,
                        session_id,
                        (result.text or "".join(parts)).strip() or EMPTY_REPLY,
                        claude_session_id=result.session_id,
                    )
                    yield chunk
                else:
                    reason = getattr(result, "error", None) or "it did not say why"
                    yield from _fail(
                        conn, session_id, parts, reason, forget=bool(resume), done=chunk
                    )
    except Exception as exc:
        log.exception("the chat call failed part-way through")
        settled = True
        yield from _fail(conn, session_id, parts, _describe(exc), forget=bool(resume))
    finally:
        # Reached when the browser vanished mid-answer: the generator is closed,
        # GeneratorExit unwinds through the yield above, and what arrived is
        # worth keeping. Nothing is written when nothing arrived — an empty
        # bubble claims to have answered, and leaving the question unanswered
        # lets the page offer to ask it again.
        if not settled:
            partial = "".join(parts).strip()
            if partial:
                _persist_reply(conn, session_id, partial)


def _fail(
    conn: sqlite3.Connection,
    session_id: int,
    parts: list[str],
    reason: str,
    *,
    forget: bool,
    done: StreamChunk | None = None,
) -> Iterator[StreamChunk]:
    """Say what went wrong, in the conversation, and end the stream.

    The sentence is hers to read, so it leads with the plain fact and puts the
    machine's words in brackets after it. A chat box that goes quiet is
    indistinguishable, from the sofa, from a chat box that is thinking.
    """
    note = (
        f"Something went wrong while I was answering, so this is not a real "
        f"answer. ({reason.strip() or 'no reason given'}) Try asking again — if "
        f"it keeps happening, the status page says what is broken."
    )
    partial = "".join(parts).strip()
    _persist_reply(
        conn,
        session_id,
        f"{partial}\n\n{note}" if partial else note,
        # A session id the CLI no longer holds fails every message after it, so
        # a conversation that failed while resuming starts clean next time. It
        # loses the thread, which is a far smaller loss than losing the page.
        forget=forget,
    )
    yield StreamChunk(kind="text", text=("\n\n" if partial else "") + note)
    yield done if done is not None else StreamChunk(kind="done")


def _persist_reply(
    conn: sqlite3.Connection,
    session_id: int,
    content: str,
    *,
    claude_session_id: str | None = None,
    forget: bool = False,
) -> None:
    """Write the assistant's message and touch the session. Never raises.

    Called from a ``finally`` that may be running under ``GeneratorExit``, where
    a raised ``sqlite3.Error`` would replace a lost connection with a confusing
    traceback from a generator nobody is watching any more.
    """
    now = db.utc_now()
    try:
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, created_at)"
                " VALUES (?, ?, ?, ?)",
                (session_id, ROLE_ASSISTANT, content, now),
            )
            if forget:
                conn.execute(
                    "UPDATE chat_sessions SET claude_session_id = NULL, updated_at = ?"
                    " WHERE id = ?",
                    (now, session_id),
                )
            elif claude_session_id:
                conn.execute(
                    "UPDATE chat_sessions SET claude_session_id = ?, updated_at = ?"
                    " WHERE id = ?",
                    (claude_session_id, now, session_id),
                )
            else:
                conn.execute(
                    "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
    except sqlite3.Error:
        log.exception("could not save the reply to chat session %s", session_id)


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _title_from(question: str) -> str:
    collapsed = " ".join(question.split())
    if len(collapsed) <= TITLE_LENGTH:
        return collapsed
    return collapsed[: TITLE_LENGTH - 1].rstrip() + "…"


# --- remembering -------------------------------------------------------------


def remember(
    conn: sqlite3.Connection,
    session_id: int | None,
    text: str,
    *,
    topic: str | None = None,
    player_name: str | None = None,
    source_url: str | None = None,
    expires_at: str | None = None,
) -> int:
    """Save something established in conversation as a note, and return its id.

    Tagged ``source_job='chat'`` so a fact she told hal-mary — "I am away in week
    11", "I would rather not start anyone playing on Thursday" — is retrieved by
    the same search every research job writes into and every prompt reads from.
    Without this the chat page is a dead end: things learned in it stay in it.

    ``ValueError`` for an empty note, from :func:`hal_mary.memory.write_note`.
    """
    note_id = memory.write_note(
        conn,
        memory.Note(
            text=text,
            source_job=JOB_NAME,
            topic=topic,
            player_name=player_name,
            source_url=source_url,
            expires_at=expires_at,
        ),
    )
    if session_id is not None:
        # The session was used, so it sorts up the list. Saving a fact is the
        # most deliberate thing she does on this page and the least visible.
        with contextlib.suppress(sqlite3.Error):
            conn.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
                (db.utc_now(), session_id),
            )
    return note_id


# --- the prompt --------------------------------------------------------------


def build_prompt(
    conn: sqlite3.Connection, settings: Settings, question: str
) -> tuple[str, str]:
    """``(prompt, context)`` for one question.

    The question is the FTS query as well as the question, so asking about a
    player surfaces whatever the research jobs stored about him. ``max_age_days``
    is ``None`` on purpose: the draft's three-week cutoff is right for "is he
    hurt" and wrong for "how has my season gone".
    """
    prompt = prompts.render_prompt(
        settings, PROMPT_FILE, {"question": question, "today": _today(), "recency": recency_block(settings)}
    )
    context = memory.build_context(
        conn,
        settings,
        query=question,
        note_limit=settings.chat.note_limit,
        max_age_days=None,
        extra_sections=state_sections(conn, settings),
    )
    return prompt, context


def _today() -> str:
    return datetime.now().astimezone().strftime("%A %d %B %Y")


def state_sections(conn: sqlite3.Connection, settings: Settings) -> dict[str, str]:
    """The state of her team, as Markdown sections. Never raises.

    She must never have to explain her own team to the bot, and an unsynced
    database is a reason to say "this is not synced" rather than a reason to send
    no context at all.
    """
    try:
        league: LeagueContext | None = load_league_context(conn, settings)
    except Exception:  # noqa: BLE001 - LeagueUnknown, a bad row, anything at all
        # Not even LeagueUnknown specifically. This block's whole job is that a
        # database in any state still produces a prompt.
        league = None

    sections: dict[str, str] = {}
    for heading, builder in (
        ("Caroline's league", lambda: _league_section(conn, settings, league)),
        ("Caroline's team right now", lambda: _team_section(conn, settings, league)),
        ("Where the draft has got to", lambda: _draft_section(conn, league)),
        ("The last few things hal-mary told her", lambda: _advice_section(conn, settings)),
    ):
        try:
            body = builder()
        except Exception:
            log.exception("could not build the %r section of the chat prompt", heading)
            continue
        if body and body.strip():
            sections[heading] = body.strip()
    return sections


def _league_section(
    conn: sqlite3.Connection, settings: Settings, league: LeagueContext | None
) -> str:
    my_name = _my_team_name(conn, settings, league)
    if league is None:
        return (
            "The league has not been read from ESPN yet, so its size, scoring and "
            "roster shape are unknown. Do not assume the usual twelve-team, "
            "standard-scoring league — say what you would need to know instead."
        )

    lines = [
        f"- {league.team_count} teams" + (f', called "{league.name}"' if league.name else "") + ".",
        f"- Scoring: {league.scoring_summary}",
        f"- Draft: {league.draft_type or 'unknown type'}, {league.rounds} rounds.",
        f'- Caroline is team {league.my_team_id}, "{my_name}".',
    ]
    if league.season:
        lines.append(f"- Season: {league.season}.")
    starters = league.starting_slots
    if starters:
        listed = ", ".join(
            f"{count} x {SLOT_LABELS.get(slot, slot)}"
            for slot, count in sorted(starters.items(), key=lambda item: slot_sort_key(item[0]))
        )
        lines.append(f"- She starts each week: {listed}.")
    lines.append(
        "- She makes every move in ESPN herself. You advise; you never act on her "
        "account and never say you have."
    )
    return "\n".join(lines)


def _team_section(
    conn: sqlite3.Connection, settings: Settings, league: LeagueContext | None
) -> str:
    team_id = league.my_team_id if league is not None else settings.team_id
    roster = _roster(conn, team_id)
    if roster:
        held = "\n".join(
            f"- {player['name']} — "
            f"{position_word(player['position']) or 'position unknown'}"
            + (f", plays for {player['pro_team']}" if player.get("pro_team") else "")
            + (f", currently {player['injury'].lower()}" if player.get("injury") else "")
            for player in roster
        )
    else:
        held = "- Nobody yet. Nothing has been drafted, or nothing has been synced."

    lines = [f"Players on her team ({len(roster)}):", held]

    if league is not None and league.roster_slots:
        needs = roster_needs(
            [{"position": player["position"]} for player in roster], league.roster_slots
        )
        open_slots = [
            f"- {SLOT_LABELS.get(slot, slot)} x{count}"
            for slot, count in sorted(needs.items(), key=lambda item: slot_sort_key(item[0]))
            if count > 0
        ]
        lines += [
            "",
            "Starting spots she still has to fill:",
            "\n".join(open_slots) or "- None. Every starting spot is filled.",
        ]
    lines += [
        "",
        (
            "hal-mary does not store her win-loss record, her weekly points, or "
            "the other teams' rosters. Say so plainly if she asks about one."
        ),
    ]
    return "\n".join(lines)


def _draft_section(conn: sqlite3.Connection, league: LeagueContext | None) -> str:
    made = store.picks_made(conn)
    if made == 0:
        return "The draft has not started — no picks have been recorded."
    if league is not None:
        total = league.total_picks
        if made >= total:
            return f"The draft is over. All {total} picks were made."
        next_pick = store.next_overall_pick(conn)
        upcoming = league.upcoming_picks(next_pick)
        her = ", ".join(str(pick) for pick in upcoming[:2]) or "none left"
        return (
            f"The draft is running. {made} of {total} picks are in; pick "
            f"{next_pick} is next. Her next picks are {her}."
        )
    return f"{made} draft picks have been recorded. The league's size is unknown."


def _advice_section(conn: sqlite3.Connection, settings: Settings) -> str:
    rows = _rows(
        conn,
        "SELECT created_at, kind, headline, body, done FROM advice"
        " ORDER BY id DESC LIMIT ?",
        (settings.chat.advice_limit,),
    )
    if not rows:
        return ""
    lines = []
    for row in rows:
        line = f"- [{(row['created_at'] or '')[:10]}] ({row['kind']}) {row['headline']}"
        if row["body"]:
            line += " — " + " ".join(str(row["body"]).split())[:400]
        lines.append(line)
    return "\n".join(lines)


def _my_team_name(
    conn: sqlite3.Connection, settings: Settings, league: LeagueContext | None
) -> str:
    team_id = league.my_team_id if league is not None else settings.team_id
    row = _one(conn, "SELECT name FROM teams WHERE team_id = ?", (team_id,))
    return (row["name"] if row and row["name"] else None) or "her team"


def _roster(conn: sqlite3.Connection, team_id: int | None) -> list[dict[str, Any]]:
    """Her players: the synced roster first, then anything only the draft knows.

    Two sources because they fail at different times. ``roster_slots`` is empty
    until a league sync lands, and a draft night run from hand-entered picks
    never gets one; ``draft_picks`` holds those, and the board is where a
    hand-entered pick's position comes from.
    """
    if team_id is None:
        return []

    players = [
        {
            "name": row["name"],
            "position": row["position"],
            "pro_team": row["pro_team"],
            "injury": (row["injury_status"] or "").upper()
            if (row["injury_status"] or "").upper() not in {"ACTIVE", "NORMAL", ""}
            else None,
        }
        for row in _rows(
            conn,
            "SELECT p.name, p.position, p.pro_team, p.injury_status"
            "  FROM roster_slots r JOIN players p ON p.player_id = r.player_id"
            " WHERE r.team_id = ? ORDER BY p.name",
            (team_id,),
        )
    ]
    seen = {(player["name"] or "").casefold() for player in players}

    for pick in store.picks_for_team(conn, team_id):
        name = pick.get("player_name")
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        row = _one(conn, "SELECT position, pro_team FROM board WHERE name = ?", (name,))
        players.append(
            {
                "name": name,
                "position": row["position"] if row else None,
                "pro_team": row["pro_team"] if row else None,
                "injury": None,
            }
        )
    return players


# --- small helpers -----------------------------------------------------------


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    try:
        return conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return None


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    try:
        return list(conn.execute(sql, params))
    except sqlite3.Error:
        return []
