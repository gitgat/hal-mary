"""Tests for the chat page's engine: sessions, streaming, memory and failure.

Nothing here spawns the real ``claude``. ``settings.claude.binary`` points at
``tests/fake_claude/claude``, which replays a recorded stream-json fixture and
writes back the argv and the stdin it was handed — which is how a test can prove
the second message of a conversation carried ``--resume`` and that the prompt
really contained her roster.

The failure paths matter more than the happy one. A chat box that goes silent is
indistinguishable, from the sofa, from a chat box that is thinking, so every way
this can go wrong has to end in a message she can read.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from conftest import FIXTURE_ENV
from hal_mary import chat, db, memory
from hal_mary.claude_runner import ClaudeRunner
from hal_mary.config import Settings, load_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_CLAUDE = REPO_ROOT / "tests" / "fake_claude" / "claude"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "claude"

#: Caroline's team in these tests. Her real id comes from settings.team_id.
HER_TEAM_ID = 6
HER_TEAM_NAME = "Invented Squad"

# Invented, like every other identity in this suite: `hal-mary sync` writes the
# real leaguemates' names into memory/league.md and CLAUDE.md forbids those
# reaching a fixture.
FAKE_LEAGUE_ID = 7654321
ROSTER_SLOTS = {
    "QB": 1,
    "RB": 2,
    "WR": 2,
    "TE": 1,
    "D/ST": 1,
    "K": 1,
    "RB/WR/TE": 1,
    "BE": 7,
    "IR": 1,
}


@dataclass
class ChatHarness:
    settings: Settings
    conn: sqlite3.Connection
    runner: ClaudeRunner
    scratch: Path
    argv_path: Path
    stdin_path: Path
    knobs: dict = field(default_factory=dict)

    def use(self, fixture: str | None = "streaming.jsonl", **knobs: object) -> None:
        """Point the fake binary at a fixture, plus any extra knobs."""
        if fixture is not None:
            knobs["fixture"] = str(FIXTURES / fixture)
        else:
            self.knobs.pop("fixture", None)
        self.knobs.update(knobs)
        self.scratch.mkdir(parents=True, exist_ok=True)
        (self.scratch / "fake_knobs.json").write_text(
            json.dumps(self.knobs), encoding="utf-8"
        )

    @property
    def argv(self) -> list[str]:
        return json.loads(self.argv_path.read_text(encoding="utf-8"))

    @property
    def stdin(self) -> str:
        return self.stdin_path.read_text(encoding="utf-8")

    def messages(self, session_id: int) -> list[tuple[str, str]]:
        return [
            (row["role"], row["content"])
            for row in chat.get_messages(self.conn, session_id)
        ]


@pytest.fixture
def harness(tmp_path: Path) -> ChatHarness:
    scratch = tmp_path / "scratch"
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    system_prompt = tmp_path / "system.md"
    system_prompt.write_text("You advise Caroline.\n", encoding="utf-8")

    db_path = tmp_path / "hal.db"
    base = load_settings(
        env={**FIXTURE_ENV, "TEAM_ID": str(HER_TEAM_ID), "DB_PATH": str(db_path)}
    )
    # model_copy, with absolute paths: Settings anchors paths in a validator that
    # a copy does not re-run, so a relative value here would stay relative.
    settings = base.model_copy(
        update={
            "claude": base.claude.model_copy(
                update={
                    "binary": str(FAKE_CLAUDE),
                    "scratch_dir": scratch,
                    "system_prompt_file": system_prompt,
                }
            ),
            "paths": base.paths.model_copy(update={"memory_dir": memory_dir}),
        }
    )

    conn = db.connect(db_path)
    db.migrate(conn)

    h = ChatHarness(
        settings=settings,
        conn=conn,
        runner=ClaudeRunner(settings, conn),
        scratch=scratch,
        argv_path=tmp_path / "argv.json",
        stdin_path=tmp_path / "stdin.txt",
        knobs={},
    )
    h.knobs = {"argv_out": str(h.argv_path), "stdin_out": str(h.stdin_path)}
    h.use()
    yield h
    conn.close()


def populate_league(conn: sqlite3.Connection) -> None:
    with db.transaction(conn):
        conn.execute(
            """
            INSERT INTO league_settings
                (id, season, league_id, name, team_count, scoring_type, draft_type,
                 draft_date, roster_slots_json, raw_json, updated_at)
            VALUES (1, 2026, ?, 'The Invented League', 6, 'H2H_POINTS', 'SNAKE',
                    NULL, ?, ?, '2026-09-07T20:13:54+00:00')
            """,
            (
                FAKE_LEAGUE_ID,
                json.dumps(ROSTER_SLOTS),
                json.dumps({"settings": {"scoringSettings": {"scoringItems": []}}}),
            ),
        )
        conn.executemany(
            "INSERT INTO teams (team_id, name, owner, abbrev, draft_slot, updated_at)"
            " VALUES (?, ?, NULL, ?, ?, '2026-09-07T20:13:54+00:00')",
            [
                (1, "Gridiron Gerbils", "GERB", 1),
                (2, "Team 2", "TM2", 2),
                (3, "Team 3", "TM3", 3),
                (4, "Punt Intended", "PUNT", 4),
                (5, "Team 5", "TM5", 5),
                (HER_TEAM_ID, HER_TEAM_NAME, "INV", 6),
            ],
        )


def populate_roster(conn: sqlite3.Connection) -> None:
    with db.transaction(conn):
        conn.executemany(
            "INSERT INTO players (player_id, name, position, pro_team, injury_status,"
            " updated_at) VALUES (?, ?, ?, ?, ?, '2026-09-07T20:13:54+00:00')",
            [
                (4430807, "Bijan Robinson", "RB", "ATL", "ACTIVE"),
                (4362628, "Nora Fixture", "WR", "CIN", "QUESTIONABLE"),
            ],
        )
        conn.executemany(
            "INSERT INTO roster_slots (team_id, player_id, slot, week, updated_at)"
            " VALUES (?, ?, ?, NULL, '2026-09-07T20:13:54+00:00')",
            [(HER_TEAM_ID, 4430807, "RB"), (HER_TEAM_ID, 4362628, "WR")],
        )


# --- sessions and messages ---------------------------------------------------


def test_a_session_is_created_and_messages_persist_in_order(harness: ChatHarness):
    session_id = chat.start_session(harness.conn)
    harness.use("streaming.jsonl")
    list(chat.send(harness.conn, harness.settings, harness.runner, session_id, "Hello?"))

    assert harness.messages(session_id) == [
        ("user", "Hello?"),
        ("assistant", "First chunk. Second chunk. Third chunk."),
    ]


def test_the_first_question_names_the_session(harness: ChatHarness):
    """A list of "Chat 1, Chat 2" is a list nobody can find anything in."""
    session_id = chat.start_session(harness.conn)
    list(
        chat.send(
            harness.conn,
            harness.settings,
            harness.runner,
            session_id,
            "Is this trade any good?",
        )
    )
    sessions = chat.list_sessions(harness.conn)
    assert [row["id"] for row in sessions] == [session_id]
    assert sessions[0]["title"] == "Is this trade any good?"


def test_an_explicit_title_is_not_overwritten(harness: ChatHarness):
    session_id = chat.start_session(harness.conn, title="Trade talk")
    list(chat.send(harness.conn, harness.settings, harness.runner, session_id, "Hi"))
    assert chat.list_sessions(harness.conn)[0]["title"] == "Trade talk"


def test_sessions_are_listed_newest_first(harness: ChatHarness):
    first = chat.start_session(harness.conn, title="One")
    second = chat.start_session(harness.conn, title="Two")
    assert [row["id"] for row in chat.list_sessions(harness.conn)] == [second, first]
    assert [row["id"] for row in chat.list_sessions(harness.conn, limit=1)] == [second]


def test_an_empty_question_is_refused_rather_than_sent(harness: ChatHarness):
    """Whitespace costs a real Claude call and produces nothing worth reading."""
    session_id = chat.start_session(harness.conn)
    with pytest.raises(ValueError):
        list(chat.send(harness.conn, harness.settings, harness.runner, session_id, "   "))
    assert harness.messages(session_id) == []


# --- streaming ---------------------------------------------------------------


def test_send_streams_chunks_and_persists_the_assistant_message(harness: ChatHarness):
    session_id = chat.start_session(harness.conn)
    chunks = list(
        chat.send(harness.conn, harness.settings, harness.runner, session_id, "Hello?")
    )

    assert [c.text for c in chunks if c.kind == "text"] == [
        "First chunk. ",
        "Second chunk. ",
        "Third chunk.",
    ]
    assert chunks[-1].kind == "done"
    assert harness.messages(session_id)[-1] == (
        "assistant",
        "First chunk. Second chunk. Third chunk.",
    )


def test_the_stored_session_id_is_resumed_on_the_second_message(harness: ChatHarness):
    """Without this every message starts a stranger who has never met her."""
    session_id = chat.start_session(harness.conn)
    list(chat.send(harness.conn, harness.settings, harness.runner, session_id, "First"))

    # The fixture's result event names sess-stream, and nothing else may.
    assert "--resume" not in harness.argv
    assert "--no-session-persistence" not in harness.argv, (
        "the CLI would discard the session whose id was just stored"
    )
    row = harness.conn.execute(
        "SELECT claude_session_id FROM chat_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    assert row["claude_session_id"] == "sess-stream"

    harness.use("streaming.jsonl")
    list(chat.send(harness.conn, harness.settings, harness.runner, session_id, "Second"))
    argv = harness.argv
    assert argv[argv.index("--resume") + 1] == "sess-stream"


def test_a_mid_stream_disconnect_persists_the_partial_reply(harness: ChatHarness):
    """Her phone locked. What arrived is worth more than nothing."""
    session_id = chat.start_session(harness.conn)
    stream = chat.send(
        harness.conn, harness.settings, harness.runner, session_id, "Hello?"
    )
    first = next(stream)
    assert first.text == "First chunk. "
    stream.close()

    role, content = harness.messages(session_id)[-1]
    assert role == "assistant"
    assert content.startswith("First chunk.")
    assert "Third chunk." not in content


def test_a_disconnect_before_any_text_leaves_the_question_unanswered(
    harness: ChatHarness,
):
    """An empty assistant bubble is a lie about having answered."""
    session_id = chat.start_session(harness.conn)
    stream = chat.send(harness.conn, harness.settings, harness.runner, session_id, "Hi")
    stream.close()
    assert harness.messages(session_id) == [("user", "Hi")]
    assert chat.pending_question(harness.conn, session_id) is not None


# --- failure -----------------------------------------------------------------


def test_a_runner_failure_persists_a_message_saying_what_went_wrong(
    harness: ChatHarness,
):
    session_id = chat.start_session(harness.conn)
    harness.use(fixture=None, exit=3, stderr="the CLI fell over")
    chunks = list(
        chat.send(harness.conn, harness.settings, harness.runner, session_id, "Hello?")
    )

    assert chunks[-1].kind == "done"
    role, content = harness.messages(session_id)[-1]
    assert role == "assistant"
    assert "went wrong" in content.lower()
    # The reason travels with it: a message that only says "sorry" cannot be acted on.
    assert "exit" in content.lower() or "the CLI fell over" in content


def test_a_failed_resume_forgets_the_session_so_the_next_message_starts_clean(
    harness: ChatHarness,
):
    """A session id the CLI no longer has would fail every message forever."""
    session_id = chat.start_session(harness.conn)
    list(chat.send(harness.conn, harness.settings, harness.runner, session_id, "First"))

    harness.use(fixture=None, exit=1)
    list(chat.send(harness.conn, harness.settings, harness.runner, session_id, "Second"))
    row = harness.conn.execute(
        "SELECT claude_session_id FROM chat_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    assert row["claude_session_id"] is None

    harness.use("streaming.jsonl")
    list(chat.send(harness.conn, harness.settings, harness.runner, session_id, "Third"))
    assert "--resume" not in harness.argv


def test_a_runner_that_raises_still_leaves_her_a_message(harness: ChatHarness):
    """Nothing about a broken chat box may reach her as a blank screen."""

    class Exploding:
        def stream(self, *args: object, **kwargs: object):
            raise RuntimeError("no binary anywhere")

    session_id = chat.start_session(harness.conn)
    chunks = list(
        chat.send(harness.conn, harness.settings, Exploding(), session_id, "Hello?")
    )
    assert chunks[-1].kind == "done"
    role, content = harness.messages(session_id)[-1]
    assert role == "assistant"
    assert "no binary anywhere" in content


# --- the prompt --------------------------------------------------------------


def test_the_prompt_carries_her_roster_the_league_and_the_matching_notes(
    harness: ChatHarness,
):
    populate_league(harness.conn)
    populate_roster(harness.conn)
    memory.write_note(
        harness.conn,
        memory.Note(
            text="Bijan Robinson is expected to get twenty carries a game.",
            source_job="news_sweep",
            player_name="Bijan Robinson",
            topic="usage",
            source_url="https://example.invalid/bijan",
        ),
    )

    session_id = chat.start_session(harness.conn)
    list(
        chat.send(
            harness.conn,
            harness.settings,
            harness.runner,
            session_id,
            "How good is Bijan Robinson?",
        )
    )

    delivered = harness.stdin
    assert "How good is Bijan Robinson?" in delivered
    assert "Bijan Robinson" in delivered
    assert "twenty carries a game" in delivered
    assert "https://example.invalid/bijan" in delivered
    assert HER_TEAM_NAME in delivered
    # The league's own shape, so nothing assumes a twelve-team standard league.
    assert "6 teams" in delivered or "6-team" in delivered
    # A bare position code is never the only label on anything she reads, and
    # the prompt is what teaches the model that vocabulary.
    assert "Running back" in delivered or "running back" in delivered


def test_the_prompt_survives_a_database_with_nothing_in_it(harness: ChatHarness):
    """The morning before the draft. Nothing is synced and she asks a question."""
    session_id = chat.start_session(harness.conn)
    chunks = list(
        chat.send(
            harness.conn, harness.settings, harness.runner, session_id, "What is PPR?"
        )
    )
    assert chunks[-1].kind == "done"
    assert "What is PPR?" in harness.stdin
    assert harness.messages(session_id)[-1][0] == "assistant"


def test_notes_of_any_age_are_retrieved_for_a_question(harness: ChatHarness):
    """The draft's three-week cutoff is wrong for "what happened this season"."""
    harness.conn.execute(
        "INSERT INTO notes (created_at, source_job, topic, player_name, text)"
        " VALUES ('2026-06-01T00:00:00+00:00', 'board_build', 'draft',"
        " 'Bijan Robinson', 'Bijan Robinson was the second player off the board.')"
    )
    session_id = chat.start_session(harness.conn)
    list(
        chat.send(
            harness.conn,
            harness.settings,
            harness.runner,
            session_id,
            "Where did Bijan Robinson go in the draft?",
        )
    )
    assert "second player off the board" in harness.stdin


def test_the_chat_job_keeps_the_web_tools_its_config_gives_it(harness: ChatHarness):
    """The one place in hal-mary where a live search is the point."""
    session_id = chat.start_session(harness.conn)
    list(chat.send(harness.conn, harness.settings, harness.runner, session_id, "Hi"))
    assert "--tools" in harness.argv
    assert "WebSearch" in harness.argv


# --- remember ----------------------------------------------------------------


def test_remember_writes_a_note_that_search_finds(harness: ChatHarness):
    session_id = chat.start_session(harness.conn)
    note_id = chat.remember(
        harness.conn,
        session_id,
        "Caroline would rather not start anyone playing on Thursday.",
        topic="preferences",
    )
    assert note_id > 0

    found = memory.search_notes(harness.conn, "Thursday", max_age_days=None)
    assert [row["text"] for row in found] == [
        "Caroline would rather not start anyone playing on Thursday."
    ]
    assert found[0]["source_job"] == "chat"
    assert found[0]["topic"] == "preferences"


def test_remember_refuses_an_empty_note(harness: ChatHarness):
    session_id = chat.start_session(harness.conn)
    with pytest.raises(ValueError):
        chat.remember(harness.conn, session_id, "   ")


def test_remember_touches_the_session_so_the_list_shows_the_activity(
    harness: ChatHarness,
):
    session_id = chat.start_session(harness.conn)
    chat.remember(harness.conn, session_id, "She is away in week 11.")
    row = harness.conn.execute(
        "SELECT updated_at FROM chat_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    assert row["updated_at"] is not None


def test_a_question_may_contain_anything_she_can_type(harness: ChatHarness):
    """Braces are characters, not template syntax.

    The prompt loader used to re-scan the *filled* template, so a question
    carrying ``{{...}}`` came back as an unfilled placeholder and the call never
    ran. It failed out loud, which was something — but it failed identically
    every time she retyped it, and nothing on the page said which characters
    were the problem.
    """
    session_id = chat.start_session(harness.conn)
    question = "What does {{PPR}} mean?"
    chunks = list(
        chat.send(harness.conn, harness.settings, harness.runner, session_id, question)
    )

    assert chunks[-1].kind == "done"
    assert question in harness.stdin
    assert harness.messages(session_id) == [
        ("user", question),
        ("assistant", "First chunk. Second chunk. Third chunk."),
    ]
