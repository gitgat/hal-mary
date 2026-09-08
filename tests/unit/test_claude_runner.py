"""Tests for the one module allowed to spawn ``claude``.

Nothing here touches the real binary: ``settings.claude.binary`` points at
``tests/fake_claude/claude``, which replays a recorded stream-json fixture and
records the argv and stdin it was handed.

The isolation-flag tests are the important ones. A bare ``claude -p`` on this
box inherits every MCP server, plugin and skill the operator has installed:
measured at 82,289 cached tokens and $0.82 for a two-token prompt. The same call
with ``--strict-mcp-config --mcp-config '{"mcpServers":{}}' --setting-sources ""``
cost $0.005. Those flags are not per-job configuration precisely so that no job
can forget them, and these tests are what keeps that true.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from hal_mary import db
from hal_mary.claude_runner import (
    ENV_PASSTHROUGH,
    ENV_PASSTHROUGH_PREFIXES,
    ClaudeRunner,
    child_environment,
)
from hal_mary.config import (
    ClaudeConfig,
    DraftConfig,
    JobConfig,
    PathsConfig,
    Settings,
    WebConfig,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_CLAUDE = REPO_ROOT / "tests" / "fake_claude" / "claude"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "claude"

# Not a real model name; the guard test in test_no_hardcoded_models.py only
# scans src/, but keeping fakes obviously fake avoids anyone copying one out.
TOOLS_ON_MODEL = "fake-model-big"
TOOLS_OFF_MODEL = "fake-model-small"


def flag_values(argv: list[str], flag: str) -> list[str]:
    """Every value following ``flag``, up to the next ``--option``."""
    assert flag in argv, f"{flag} missing from argv: {argv}"
    start = argv.index(flag) + 1
    values = []
    for item in argv[start:]:
        if item.startswith("--"):
            break
        values.append(item)
    return values


def flag_value(argv: list[str], flag: str) -> str:
    values = flag_values(argv, flag)
    assert len(values) == 1, f"expected one value after {flag}, got {values}"
    return values[0]


@dataclass
class Harness:
    settings: Settings
    conn: object
    runner: ClaudeRunner
    argv_path: Path
    stdin_path: Path
    monkeypatch: pytest.MonkeyPatch
    scratch: Path
    knobs: dict

    def use(self, fixture: str | None = "simple_text.jsonl", **knobs: object) -> None:
        """Point the fake binary at a fixture, plus any extra fake knobs.

        Written to ``fake_knobs.json`` in the scratch directory, which is the
        fake's working directory. Not the environment: the runner hands its
        child an explicit allowlist, and a fake driven through the environment
        would need that allowlist widened for the tests' own sake.
        """
        if fixture is not None:
            knobs["fixture"] = str(FIXTURES / fixture)
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

    def calls(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM claude_calls ORDER BY id").fetchall()
        return [dict(row) for row in rows]


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    scratch = tmp_path / "scratch"
    system_prompt_file = tmp_path / "system.md"
    system_prompt_file.write_text("You are a fantasy football advisor.\n", encoding="utf-8")

    settings = Settings(
        claude=ClaudeConfig(
            binary=str(FAKE_CLAUDE),
            default_model=TOOLS_ON_MODEL,
            permission_mode="dontAsk",
            scratch_dir=str(scratch),
            system_prompt_file=str(system_prompt_file),
        ),
        paths=PathsConfig(prompts_dir="prompts", memory_dir="memory"),
        draft=DraftConfig(poll_seconds=5, advise_within_picks=2),
        web=WebConfig(host="127.0.0.1", port=8080, session_cookie="hal_mary_session"),
        jobs={
            "tools_on": JobConfig(
                name="tools_on",
                model=TOOLS_ON_MODEL,
                tools=["WebSearch", "WebFetch"],
                timeout_s=30,
                max_budget_usd=5.0,
            ),
            "tools_off": JobConfig(
                name="tools_off",
                model=TOOLS_OFF_MODEL,
                tools=[],
                timeout_s=30,
                max_budget_usd=1.0,
            ),
            "impatient": JobConfig(
                name="impatient",
                model=TOOLS_OFF_MODEL,
                tools=[],
                timeout_s=1,
                max_budget_usd=1.0,
            ),
        },
    )

    conn = db.connect(tmp_path / "hal.db")
    db.migrate(conn)

    argv_path = tmp_path / "argv.json"
    stdin_path = tmp_path / "stdin.txt"

    h = Harness(
        settings=settings,
        conn=conn,
        runner=ClaudeRunner(settings, conn),
        argv_path=argv_path,
        stdin_path=stdin_path,
        monkeypatch=monkeypatch,
        scratch=scratch,
        knobs={"argv_out": str(argv_path), "stdin_out": str(stdin_path)},
    )
    h.use()
    yield h
    conn.close()


# --------------------------------------------------------------------------
# argv construction
# --------------------------------------------------------------------------


@pytest.mark.parametrize("job", ["tools_on", "tools_off"])
def test_isolation_flags_are_present_for_every_job(harness: Harness, job: str):
    """The 165x cost difference. Not optional, not per-job configurable."""
    harness.runner.run(job, "hello")
    argv = harness.argv
    assert "--strict-mcp-config" in argv
    assert flag_value(argv, "--mcp-config") == '{"mcpServers":{}}'
    assert flag_values(argv, "--setting-sources") == [""]


@pytest.mark.parametrize("job", ["tools_on", "tools_off"])
def test_stream_json_flags_are_present_for_every_job(harness: Harness, job: str):
    """--verbose is mandatory alongside stream-json under -p; the CLI errors without it."""
    harness.runner.run(job, "hello")
    argv = harness.argv
    assert argv[0] == "-p"
    assert flag_value(argv, "--output-format") == "stream-json"
    assert "--verbose" in argv
    assert flag_value(argv, "--permission-mode") == "dontAsk"


def test_allowed_tools_present_only_when_tools_are_configured(harness: Harness):
    harness.runner.run("tools_on", "hello")
    argv = harness.argv
    assert flag_values(argv, "--tools") == ["WebSearch", "WebFetch"]
    assert flag_values(argv, "--allowedTools") == ["WebSearch", "WebFetch"]


def test_tools_off_job_disables_tools_and_omits_allowed_tools(harness: Harness):
    harness.runner.run("tools_off", "hello")
    argv = harness.argv
    assert flag_values(argv, "--tools") == [""]
    assert "--allowedTools" not in argv


def test_json_schema_only_when_a_schema_is_passed(harness: Harness):
    harness.runner.run("tools_off", "hello")
    assert "--json-schema" not in harness.argv

    schema = {"type": "object", "properties": {"pick": {"type": "string"}}}
    harness.use("structured.jsonl")
    harness.runner.run("tools_off", "hello", schema=schema)
    raw = flag_value(harness.argv, "--json-schema")
    assert json.loads(raw) == schema
    assert " " not in raw, f"schema should be compact JSON, got {raw!r}"


def test_resume_and_no_session_persistence_are_mutually_exclusive(harness: Harness):
    harness.runner.run("tools_off", "hello")
    argv = harness.argv
    assert "--no-session-persistence" in argv
    assert "--resume" not in argv

    harness.runner.run("tools_off", "hello", resume="sess-abc")
    argv = harness.argv
    assert flag_value(argv, "--resume") == "sess-abc"
    assert "--no-session-persistence" not in argv


def test_persist_session_keeps_the_session_the_chat_page_will_resume(harness: Harness):
    """A one-shot job leaves nothing behind; a conversation has to.

    ``--no-session-persistence`` is right for every scheduled job and fatal for
    chat: the CLI would discard the session whose id chat then stores, and the
    ``--resume`` on her second message would name a session that was never
    written. So the flag is suppressed only when a caller says it is holding a
    conversation.
    """
    harness.runner.run("tools_off", "hello", persist_session=True)
    argv = harness.argv
    assert "--no-session-persistence" not in argv
    assert "--resume" not in argv

    harness.use("streaming.jsonl")
    list(harness.runner.stream("tools_off", "hello", persist_session=True))
    assert "--no-session-persistence" not in harness.argv


def test_persist_session_is_off_unless_asked_for(harness: Harness):
    harness.use("streaming.jsonl")
    list(harness.runner.stream("tools_off", "hello"))
    assert "--no-session-persistence" in harness.argv


def test_model_comes_from_job_config(harness: Harness):
    harness.runner.run("tools_on", "hello")
    assert flag_value(harness.argv, "--model") == TOOLS_ON_MODEL
    harness.runner.run("tools_off", "hello")
    assert flag_value(harness.argv, "--model") == TOOLS_OFF_MODEL


def test_max_budget_comes_from_job_config(harness: Harness):
    harness.runner.run("tools_on", "hello")
    assert flag_value(harness.argv, "--max-budget-usd") == "5.0"


def test_include_partial_messages_only_when_streaming(harness: Harness):
    harness.runner.run("tools_off", "hello")
    assert "--include-partial-messages" not in harness.argv

    harness.use("streaming.jsonl")
    list(harness.runner.stream("tools_off", "hello"))
    assert "--include-partial-messages" in harness.argv


def test_unknown_job_name_raises_keyerror(harness: Harness):
    with pytest.raises(KeyError):
        harness.runner.run("no_such_job", "hello")


# --------------------------------------------------------------------------
# prompt delivery, cwd, system prompt
# --------------------------------------------------------------------------


def test_prompt_arrives_on_stdin_not_argv(harness: Harness):
    prompt = "Who should I draft?\nHere's a \"quoted\" thing.\n" + ("x" * 40_000)
    harness.runner.run("tools_off", prompt)
    assert harness.stdin == prompt
    assert prompt not in harness.argv
    assert not any(prompt[:50] in arg for arg in harness.argv)


def test_extra_context_is_prepended_to_the_prompt(harness: Harness):
    harness.runner.run("tools_off", "The task.", extra_context="The board so far.")
    delivered = harness.stdin
    assert "The board so far." in delivered
    assert delivered.index("The board so far.") < delivered.index("The task.")


def test_cwd_is_the_scratch_dir_and_is_created(harness: Harness, monkeypatch, tmp_path: Path):
    """A scratch directory the harness has never touched, so "created" means it.

    The fake finds no knobs file there and behaves like a CLI that produced
    nothing; that is fine, because what is under test is the cwd and the mkdir.
    """
    seen = {}
    import subprocess

    real_popen = subprocess.Popen

    def spy(*args, **kwargs):
        seen["cwd"] = kwargs.get("cwd")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)

    fresh = tmp_path / "never-created" / "scratch"
    settings = harness.settings.model_copy(
        update={"claude": harness.settings.claude.model_copy(update={"scratch_dir": fresh})}
    )
    assert not fresh.exists()

    ClaudeRunner(settings, harness.conn).run("tools_off", "hello")

    assert Path(seen["cwd"]).resolve() == fresh.resolve()
    assert fresh.is_dir()


def test_system_prompt_comes_from_the_config_file_by_default(harness: Harness):
    harness.runner.run("tools_off", "hello")
    assert flag_value(harness.argv, "--system-prompt") == "You are a fantasy football advisor."


def test_explicit_system_prompt_wins(harness: Harness):
    harness.runner.run("tools_off", "hello", system_prompt="Be terse.")
    assert flag_value(harness.argv, "--system-prompt") == "Be terse."


def test_missing_system_prompt_file_omits_the_flag(harness: Harness, tmp_path: Path):
    Path(harness.settings.claude.system_prompt_file).unlink()
    result = harness.runner.run("tools_off", "hello")
    assert "--system-prompt" not in harness.argv
    assert result.ok


# --------------------------------------------------------------------------
# output parsing
# --------------------------------------------------------------------------


def test_simple_text_result(harness: Harness):
    result = harness.runner.run("tools_off", "hello")
    assert result.ok
    assert result.text == "Take the running back."
    assert result.structured is None
    assert result.session_id == "sess-simple"
    assert result.cost_usd == pytest.approx(0.0042)
    assert result.duration_ms == 1234
    assert result.exit_code == 0
    assert result.error is None


def test_structured_output_is_parsed(harness: Harness):
    harness.use("structured.jsonl")
    result = harness.runner.run("tools_off", "hello", schema={"type": "object"})
    assert result.ok
    assert result.structured == {
        "pick": "Bijan Robinson",
        "reason": "Best back left on the board.",
    }


def test_structured_output_falls_back_to_parsing_the_result_string(harness: Harness):
    harness.use("structured_result_only.jsonl")
    result = harness.runner.run("tools_off", "hello", schema={"type": "object"})
    assert result.ok
    assert result.structured == {
        "pick": "Bijan Robinson",
        "reason": "Best back left on the board.",
    }


def test_unparseable_structured_output_is_a_failure(harness: Harness):
    harness.use("structured_unparseable.jsonl")
    result = harness.runner.run("tools_off", "hello", schema={"type": "object"})
    assert not result.ok
    assert result.structured is None
    assert result.error is not None
    assert "structured" in result.error.lower() or "parse" in result.error.lower()
    # The text is still there, so a caller can show the operator what came back.
    assert result.text == "I could not follow the schema, sorry."


def test_malformed_lines_are_skipped_and_earlier_events_survive(harness: Harness):
    harness.use("malformed.jsonl")
    result = harness.runner.run("tools_off", "hello")
    assert result.ok
    assert result.text == "Survived the noise."
    assert result.session_id == "sess-malformed"


def test_transcript_is_written_and_raw_path_points_at_it(harness: Harness):
    result = harness.runner.run("tools_off", "hello")
    assert result.raw_path is not None
    assert result.raw_path.is_file()
    assert result.raw_path.parent == (harness.scratch / "transcripts")
    lines = result.raw_path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["type"] == "result"


# --------------------------------------------------------------------------
# failure modes
# --------------------------------------------------------------------------


def test_timeout_kills_and_reports(harness: Harness):
    harness.use("simple_text.jsonl", sleep="30")
    result = harness.runner.run("impatient", "hello")
    assert not result.ok
    assert result.error is not None
    assert "timed out" in result.error.lower()
    assert "1" in result.error  # the configured limit
    assert result.text == ""


def test_nonzero_exit_captures_stderr(harness: Harness):
    harness.use("simple_text.jsonl", exit="2", stderr="boom: no auth")
    result = harness.runner.run("tools_off", "hello")
    assert not result.ok
    assert result.exit_code == 2
    assert "boom: no auth" in (result.error or "")


def test_missing_binary_is_reported_not_raised(harness: Harness, tmp_path: Path):
    missing = tmp_path / "definitely-not-here"
    broken = harness.settings.model_copy(
        update={"claude": harness.settings.claude.model_copy(update={"binary": str(missing)})}
    )
    runner = ClaudeRunner(broken, harness.conn)
    result = runner.run("tools_off", "hello")
    assert not result.ok
    assert result.error is not None
    assert str(missing) in result.error
    assert result.exit_code != 0


# --------------------------------------------------------------------------
# claude_calls bookkeeping
# --------------------------------------------------------------------------


def test_success_writes_exactly_one_claude_calls_row(harness: Harness):
    prompt = "Who should I draft?"
    result = harness.runner.run("tools_off", prompt)
    rows = harness.calls()
    assert len(rows) == 1
    row = rows[0]
    assert row["job"] == "tools_off"
    assert row["model"] == TOOLS_OFF_MODEL
    assert row["prompt_hash"] == hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    assert row["exit_code"] == 0
    assert row["session_id"] == "sess-simple"
    assert row["cost_usd"] == pytest.approx(0.0042)
    assert row["duration_ms"] == 1234
    assert row["error"] is None
    assert row["output_path"] == str(result.raw_path)
    assert row["started_at"]
    argv = json.loads(row["argv_json"])
    assert "--strict-mcp-config" in argv
    assert prompt not in argv
    assert not any(prompt in arg for arg in argv)


@pytest.mark.parametrize(
    ("job", "env"),
    [
        ("impatient", {"sleep": "30"}),
        ("tools_off", {"exit": "3", "stderr": "nope"}),
    ],
    ids=["timeout", "nonzero-exit"],
)
def test_each_failure_mode_still_writes_a_row(harness: Harness, job: str, env: dict):
    harness.use("simple_text.jsonl", **env)
    prompt = "Who should I draft?"
    result = harness.runner.run(job, prompt)
    assert not result.ok
    rows = harness.calls()
    assert len(rows) == 1
    assert rows[0]["error"]
    assert rows[0]["prompt_hash"] == hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    assert prompt not in rows[0]["argv_json"]


def test_missing_binary_still_writes_a_row(harness: Harness, tmp_path: Path):
    broken = harness.settings.model_copy(
        update={
            "claude": harness.settings.claude.model_copy(
                update={"binary": str(tmp_path / "nope")}
            )
        }
    )
    ClaudeRunner(broken, harness.conn).run("tools_off", "hello")
    rows = harness.calls()
    assert len(rows) == 1
    assert rows[0]["error"]


def test_prompt_hash_covers_the_extra_context(harness: Harness):
    harness.runner.run("tools_off", "The task.", extra_context="The board.")
    delivered = harness.stdin
    row = harness.calls()[0]
    assert row["prompt_hash"] == hashlib.sha256(delivered.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------


def test_stream_yields_text_then_a_done_chunk(harness: Harness):
    harness.use("streaming.jsonl")
    chunks = list(harness.runner.stream("tools_off", "hello"))
    text_chunks = [c for c in chunks if c.kind == "text"]
    assert [c.text for c in text_chunks] == ["First chunk. ", "Second chunk. ", "Third chunk."]
    assert chunks[-1].kind == "done"
    assert chunks[-1].result is not None
    assert chunks[-1].result.ok
    assert chunks[-1].result.text == "First chunk. Second chunk. Third chunk."
    assert len(harness.calls()) == 1


def test_stream_does_not_duplicate_partial_message_text(harness: Harness):
    """With --include-partial-messages the assistant event repeats the deltas."""
    harness.use("streaming_partial.jsonl")
    chunks = list(harness.runner.stream("tools_off", "hello"))
    streamed = "".join(c.text for c in chunks if c.kind == "text")
    assert streamed == "Start then end."


def test_stream_failure_still_yields_a_done_chunk(harness: Harness):
    harness.use("streaming.jsonl", exit="4", stderr="stream boom")
    chunks = list(harness.runner.stream("tools_off", "hello"))
    assert chunks[-1].kind == "done"
    assert not chunks[-1].result.ok
    assert "stream boom" in chunks[-1].result.error
    assert len(harness.calls()) == 1


def test_stream_rejects_an_unknown_job_before_the_first_next(harness: Harness):
    """A typo must not surface halfway through an already-open SSE response."""
    with pytest.raises(KeyError):
        harness.runner.stream("no_such_job", "hello")


def test_argv_json_redacts_the_system_prompt(harness: Harness):
    """The row is a status-page record, not a copy of every prompt file.

    The system prompt is thousands of characters and identical on every call;
    stored verbatim it dwarfs the rest of the row and makes claude_calls
    unreadable. The digest is enough to tell which prompt was in force.
    """
    long_prompt = "You are hal-mary." * 500
    harness.runner.run("tools_off", "hello", system_prompt=long_prompt)
    argv = json.loads(harness.calls()[0]["argv_json"])
    # The flag is still recorded, so the row shows a system prompt was in play.
    value = flag_value(argv, "--system-prompt")
    assert long_prompt not in value
    assert hashlib.sha256(long_prompt.encode("utf-8")).hexdigest()[:12] in value
    assert len(harness.calls()[0]["argv_json"]) < 1000
    # ...and the real text still reached the binary.
    assert flag_value(harness.argv, "--system-prompt") == long_prompt


# --------------------------------------------------------------------------
# review follow-ups
# --------------------------------------------------------------------------


def assert_isolated(argv: list[str]) -> None:
    """The three flags worth 165x. Asserted on every argv shape there is."""
    assert "--strict-mcp-config" in argv
    assert flag_value(argv, "--mcp-config") == '{"mcpServers":{}}'
    assert flag_values(argv, "--setting-sources") == [""]


@pytest.mark.parametrize("job", ["tools_on", "tools_off"])
@pytest.mark.parametrize("streaming", [False, True], ids=["run", "stream"])
@pytest.mark.parametrize("resume", [None, "sess-abc"], ids=["fresh", "resumed"])
@pytest.mark.parametrize("schema", [None, {"type": "object"}], ids=["no-schema", "schema"])
def test_build_argv_is_isolated_in_every_combination(
    harness: Harness, job: str, streaming: bool, resume: str | None, schema: dict | None
):
    """Cover the option matrix, not just the one shape run() happens to take.

    build_argv is the only argv producer, so if the flags survive every
    combination of its options they cannot be dropped by a caller.
    """
    argv = harness.runner.build_argv(
        harness.settings.job(job),
        streaming=streaming,
        system_prompt="hi",
        schema=schema,
        resume=resume,
    )
    assert_isolated(argv)


def test_isolation_flags_survive_a_streaming_resumed_call(harness: Harness):
    """End to end, not just through build_argv: what the binary really received."""
    harness.use("streaming.jsonl")
    list(harness.runner.stream("tools_on", "hello", resume="sess-abc"))
    assert_isolated(harness.argv)


def test_stderr_is_waited_for_not_raced(harness: Harness, monkeypatch):
    """A slow stderr reader must not cost us the only diagnostic there was.

    proc.wait() returns the moment the child exits, which can be before the
    reader thread has drained the pipe. Reading the box without joining first
    records "exited 1 with no stderr" and throws away the reason — exactly when
    it matters most, because exit 1 with a stderr message is what expired auth
    looks like.
    """
    real_drain = ClaudeRunner._drain_stderr

    def slow_drain(proc, box):
        time.sleep(0.3)
        real_drain(proc, box)

    monkeypatch.setattr(ClaudeRunner, "_drain_stderr", staticmethod(slow_drain))
    harness.use(
        "simple_text.jsonl", exit="1", stderr="auth expired"
    )
    result = harness.runner.run("tools_off", "hello")
    assert not result.ok
    assert "auth expired" in (result.error or "")
    assert harness.calls()[0]["error"] and "auth expired" in harness.calls()[0]["error"]


def test_unusable_scratch_dir_is_reported_not_raised(harness: Harness, tmp_path: Path):
    """The only escape from "nothing raises" was a full or read-only scratch volume.

    It would surface as an OSError out of run() inside an APScheduler job or an
    SSE handler, mid-draft, with no claude_calls row to show for it.
    """
    a_file = tmp_path / "not-a-directory"
    a_file.write_text("", encoding="utf-8")
    broken = harness.settings.model_copy(
        update={
            "claude": harness.settings.claude.model_copy(
                update={"scratch_dir": a_file / "scratch"}
            )
        }
    )
    result = ClaudeRunner(broken, harness.conn).run("tools_off", "hello")
    assert not result.ok
    assert result.error is not None and "scratch" in result.error.lower()
    assert result.exit_code != 0
    rows = harness.calls()
    assert len(rows) == 1
    assert rows[0]["error"] == result.error


def test_transcript_opens_with_a_header_naming_the_system_prompt(harness: Harness):
    """The transcript is the only place a dynamic system prompt survives.

    claude_calls.argv_json keeps a digest to stay small, which is fine for the
    file-sourced prompt because that file is in git. A system prompt built per
    pick exists nowhere else, so it goes in the transcript, whose path is on the
    row.
    """
    built_per_call = "You are advising on pick 14. Zero RBs rostered."
    result = harness.runner.run("tools_off", "hello", system_prompt=built_per_call)
    lines = result.raw_path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert header["type"] == "hal_mary_call"
    assert header["job"] == "tools_off"
    assert header["system_prompt"] == built_per_call
    assert "--strict-mcp-config" in header["argv"]
    # The stream itself still follows, untouched.
    assert json.loads(lines[-1])["type"] == "result"


def test_transcript_header_records_no_system_prompt_when_there_is_none(harness: Harness):
    Path(harness.settings.claude.system_prompt_file).unlink()
    result = harness.runner.run("tools_off", "hello")
    header = json.loads(result.raw_path.read_text(encoding="utf-8").splitlines()[0])
    assert header["system_prompt"] is None


def test_abandoned_stream_kills_and_still_records_a_row(harness: Harness):
    """The SSE client went away. The call still happened and still cost money."""
    harness.use("streaming.jsonl", hang="30")
    chunks = harness.runner.stream("tools_off", "hello")
    first = next(chunks)
    assert first.kind == "text"
    chunks.close()

    rows = harness.calls()
    assert len(rows) == 1
    assert "abandoned" in rows[0]["error"]
    assert rows[0]["prompt_hash"]


def test_lines_buffered_at_the_deadline_still_reach_the_transcript(harness: Harness, monkeypatch):
    """A timeout must not throw away output that already arrived.

    Lines sitting in the reader queue when the deadline fires are the last thing
    the model said before it stopped responding — the most useful part of the
    transcript for working out why a job hung.
    """
    real_parse = ClaudeRunner._parse

    def slow_parse(line, sink):
        time.sleep(0.4)
        return real_parse(line, sink)

    monkeypatch.setattr(ClaudeRunner, "_parse", staticmethod(slow_parse))
    harness.use("streaming.jsonl", hang="30")
    result = harness.runner.run("impatient", "hello")  # timeout_s = 1

    assert not result.ok
    assert "timed out" in (result.error or "").lower()
    lines = result.raw_path.read_text(encoding="utf-8").splitlines()
    # One header plus every one of the fixture's five events: parsing at 0.4s a
    # line means only three were consumed before the deadline.
    assert len(lines) == 6, lines
    assert json.loads(lines[-1])["type"] == "result"


# --------------------------------------------------------------------------
# the child's environment
# --------------------------------------------------------------------------
#
# `claude -p` inherits nothing it is not handed. Most jobs run with tools off,
# but board_build, news_sweep, waiver_scan and chat run with WebSearch and
# WebFetch on — and those are exactly the calls where a prompt-injected
# "print your environment" would have something worth taking. ESPN_S2 and SWID
# are a session on Caroline's ESPN account; WEB_PASSWORD is the household
# password to this app.


SECRET_KEYS = ("ESPN_S2", "SWID", "WEB_PASSWORD", "DB_PATH")


def test_the_child_environment_drops_the_secrets(harness: Harness, monkeypatch, tmp_path: Path):
    """The evidence is what the child actually received, not what we meant to send."""
    for key in SECRET_KEYS:
        monkeypatch.setenv(key, f"secret-value-for-{key}")
    env_out = tmp_path / "child-env.json"
    harness.use("simple_text.jsonl", env_out=str(env_out))

    harness.runner.run("tools_on", "hello")

    child = json.loads(env_out.read_text(encoding="utf-8"))
    leaked = sorted(set(child) & set(SECRET_KEYS))
    assert leaked == [], f"the child inherited {leaked}"
    assert not [v for v in child.values() if v.startswith("secret-value-for-")]


def test_the_child_environment_keeps_what_the_binary_needs(
    harness: Harness, monkeypatch, tmp_path: Path
):
    """HOME above all: the binary's subscription credentials live under it."""
    monkeypatch.setenv("LC_ALL", "en_GB.UTF-8")
    env_out = tmp_path / "child-env.json"
    harness.use("simple_text.jsonl", env_out=str(env_out))

    harness.runner.run("tools_on", "hello")

    child = json.loads(env_out.read_text(encoding="utf-8"))
    assert child["PATH"] == os.environ["PATH"]
    assert child["HOME"] == os.environ["HOME"]
    assert child["LC_ALL"] == "en_GB.UTF-8"


def test_the_allowlist_is_a_list_not_a_filter(monkeypatch):
    """Allow by name, never deny by name. A deny list has to be updated every
    time a new secret is added; this one is wrong only when something the binary
    needs is left out, which is loud."""
    monkeypatch.setenv("SOMETHING_NOBODY_THOUGHT_OF", "value")
    monkeypatch.setenv("ESPN_S2", "cookie")

    built = child_environment()

    assert "SOMETHING_NOBODY_THOUGHT_OF" not in built
    assert "ESPN_S2" not in built
    assert set(built) <= set(ENV_PASSTHROUGH) | {
        k for k in built if k.startswith(ENV_PASSTHROUGH_PREFIXES)
    }


def test_an_unset_allowlisted_variable_is_simply_absent(monkeypatch):
    """Never a key with an empty value: `TMPDIR=""` is not the same as no TMPDIR."""
    monkeypatch.delenv("TMPDIR", raising=False)

    assert "TMPDIR" not in child_environment()


def test_dropping_an_api_key_says_so(monkeypatch, caplog):
    """The one genuinely silent case in the allowlist.

    A box with no ``~/.claude`` fails loudly on the next call. A box carrying
    both a login *and* an ANTHROPIC_API_KEY meant for billing does not: it falls
    back to the subscription and the only evidence is the invoice.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")

    with caplog.at_level(logging.WARNING, logger="hal_mary.claude_runner"):
        built = child_environment()

    assert "ANTHROPIC_API_KEY" not in built
    assert "ANTHROPIC_API_KEY" in caplog.text
    assert "subscription" in caplog.text.lower()


def test_no_api_key_means_no_warning(monkeypatch, caplog):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with caplog.at_level(logging.WARNING, logger="hal_mary.claude_runner"):
        child_environment()

    assert caplog.records == []
