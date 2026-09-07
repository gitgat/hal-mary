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
from dataclasses import dataclass
from pathlib import Path

import pytest

from hal_mary import db
from hal_mary.claude_runner import ClaudeRunner
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

    def use(self, fixture: str = "simple_text.jsonl", **env: str) -> None:
        """Point the fake binary at a fixture, plus any extra fake knobs."""
        self.monkeypatch.setenv("HAL_MARY_FAKE_FIXTURE", str(FIXTURES / fixture))
        for key, value in env.items():
            self.monkeypatch.setenv(key, value)

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
    monkeypatch.setenv("HAL_MARY_FAKE_ARGV_OUT", str(argv_path))
    monkeypatch.setenv("HAL_MARY_FAKE_STDIN_OUT", str(stdin_path))

    h = Harness(
        settings=settings,
        conn=conn,
        runner=ClaudeRunner(settings, conn),
        argv_path=argv_path,
        stdin_path=stdin_path,
        monkeypatch=monkeypatch,
        scratch=scratch,
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


def test_cwd_is_the_scratch_dir_and_is_created(harness: Harness, monkeypatch):
    seen = {}
    import subprocess

    real_popen = subprocess.Popen

    def spy(*args, **kwargs):
        seen["cwd"] = kwargs.get("cwd")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    assert not harness.scratch.exists()
    harness.runner.run("tools_off", "hello")
    assert Path(seen["cwd"]).resolve() == harness.scratch.resolve()
    assert harness.scratch.is_dir()


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
    harness.use("simple_text.jsonl", HAL_MARY_FAKE_SLEEP="30")
    result = harness.runner.run("impatient", "hello")
    assert not result.ok
    assert result.error is not None
    assert "timed out" in result.error.lower()
    assert "1" in result.error  # the configured limit
    assert result.text == ""


def test_nonzero_exit_captures_stderr(harness: Harness):
    harness.use("simple_text.jsonl", HAL_MARY_FAKE_EXIT="2", HAL_MARY_FAKE_STDERR="boom: no auth")
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
        ("impatient", {"HAL_MARY_FAKE_SLEEP": "30"}),
        ("tools_off", {"HAL_MARY_FAKE_EXIT": "3", "HAL_MARY_FAKE_STDERR": "nope"}),
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
    harness.use("streaming.jsonl", HAL_MARY_FAKE_EXIT="4", HAL_MARY_FAKE_STDERR="stream boom")
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
