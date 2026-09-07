"""The only module in hal-mary allowed to spawn the ``claude`` binary.

Every scheduled job, the on-the-clock draft advisor and the chat page call
through :class:`ClaudeRunner`. Callers hand it a job name and a prompt; it owns
argv construction, the subprocess, the timeout, the stream-json parsing, the
transcript on disk and the ``claude_calls`` row.

Why the isolation flags are hardcoded
-------------------------------------
A bare ``claude -p`` inherits the *operator's* whole environment: every MCP
server, plugin and skill installed for the user running the daemon. Measured on
the production box with the CLI at 2.1.260, a two-token prompt cost **$0.82**
and dragged 82,289 cached tokens along with it. The same prompt with
``--strict-mcp-config --mcp-config '{"mcpServers":{}}'`` cost $0.048, and adding
``--setting-sources ""`` brought it to **$0.005**. That is a 165x difference on
a call this application makes dozens of times a day.

So those three flags live in :data:`ISOLATION_ARGS` and are appended to every
argv unconditionally. They are deliberately *not* per-job configuration: a job
must not be able to forget them, and a test asserts they are present for every
job. If a future job genuinely needs an MCP server, it gets a new explicit
argument here and a new test, not a config key that defaults to "inherit
everything".

Other CLI facts this module encodes, measured rather than assumed:

* ``--verbose`` is **required** alongside ``--output-format stream-json`` under
  ``-p``; without it the CLI refuses to start.
* The final event is ``{"type": "result", ...}`` carrying ``total_cost_usd``,
  ``duration_ms``, ``session_id``, ``is_error`` and ``result``.
* With ``--json-schema`` the result event also carries ``structured_output``
  holding the already-parsed object; ``result`` holds the same JSON as a string.
* Event types beyond those are common (``system``, ``user``, ``stream_event``,
  ``rate_limit_event``) and the set is not closed. Unknown types are skipped,
  never fatal, and so are unparseable lines: a truncated final line must not
  cost us the events before it.

Threading
---------
A :class:`ClaudeRunner` holds one ``sqlite3.Connection``, and ``db.connect``
leaves ``check_same_thread`` on, so the connection must not cross threads. Build
the connection and the runner together, in the thread that will use them — one
per job worker, one per request — rather than sharing a module-level singleton.
This bites hardest around :meth:`ClaudeRunner.stream`, whose final chunk writes
the ``claude_calls`` row; see that method's docstring for the pattern that works
under FastAPI.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from . import db
from .config import JobConfig, Settings

__all__ = [
    "CONTEXT_SEPARATOR",
    "ISOLATION_ARGS",
    "TIMEOUT_TEARDOWN_S",
    "ClaudeResult",
    "ClaudeRunner",
    "StreamChunk",
]

log = logging.getLogger(__name__)

#: The three flags that keep a call from inheriting the operator's environment.
#: Hardcoded on purpose — see the module docstring. Never make these configurable.
ISOLATION_ARGS: tuple[str, ...] = (
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--setting-sources",
    "",
)

#: How ``extra_context`` is joined to the prompt. Task 4's ``build_context()``
#: output goes through here, so every caller gets the same shape and prompt
#: files can rely on the headings being present.
CONTEXT_SEPARATOR = "\n\n--- END CONTEXT ---\n\n"

#: Grace period for a killed process group to actually die.
_REAP_TIMEOUT_S = 5.0

#: How long to wait for the stderr reader to reach EOF before giving up on it.
#: ``proc.wait()`` returns the moment the child exits, which can be before the
#: reader thread has drained the pipe; reading its buffer without joining first
#: throws away the diagnostic exactly when there is one.
_STDERR_JOIN_TIMEOUT_S = 2.0

#: What a timeout costs *beyond* the job's own ``timeout_s``.
#:
#: When a deadline expires the runner kills the process group and reaps it
#: (:data:`_REAP_TIMEOUT_S`), then joins the stdout reader to recover whatever
#: the model said before it stopped (:data:`_STDERR_JOIN_TIMEOUT_S`), then writes
#: the ``claude_calls`` row. None of that is inside ``timeout_s``, so a job
#: configured for 25 seconds can occupy 32.
#:
#: Exported because the draft advisor budgets a 90-second pick clock and has to
#: know the true cost of an attempt. A private copy of these numbers in that
#: module would drift from these the first time either is tuned, and the symptom
#: would be a recommendation arriving after the pick was made.
TIMEOUT_TEARDOWN_S = _REAP_TIMEOUT_S + _STDERR_JOIN_TIMEOUT_S

#: Exit code recorded for a call that never reached the binary at all.
_NEVER_RAN = -1

#: First line of every transcript: the call itself, so the file is a complete
#: record. ``claude`` never emits this type, and a reader that does not know it
#: skips it like any other unknown event.
_HEADER_EVENT_TYPE = "hal_mary_call"


@dataclass(frozen=True)
class ClaudeResult:
    """Everything one ``claude`` invocation produced.

    ``ok`` is false for a timeout, a nonzero exit, a missing binary, a result
    the CLI itself flagged as an error, and a schema request whose output could
    not be parsed. ``error`` says which. Nothing in this module raises for a
    failed call: jobs record failures, they do not crash the scheduler.
    """

    ok: bool
    text: str
    structured: dict | None
    session_id: str | None
    cost_usd: float | None
    duration_ms: int | None
    exit_code: int
    error: str | None
    raw_path: Path | None


@dataclass(frozen=True)
class StreamChunk:
    """One item from :meth:`ClaudeRunner.stream`.

    ``kind="text"`` chunks carry a fragment of the assistant's reply as it
    arrives, for the chat page's SSE endpoint. Exactly one ``kind="done"`` chunk
    is yielded last, carrying the :class:`ClaudeResult`.
    """

    kind: Literal["text", "done"]
    text: str = ""
    result: ClaudeResult | None = None


class _EventSink:
    """Accumulates the interesting parts of a stream-json event stream.

    Deliberately forgiving: any event whose shape is not what we expect is
    ignored rather than raising. The transcript on disk is the record of what
    really arrived; this class only pulls out what callers need.
    """

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.result_text: str | None = None
        self.structured_output: Any = None
        self.cost_usd: float | None = None
        self.duration_ms: int | None = None
        self.is_error = False
        self.subtype: str | None = None
        self.saw_result = False
        self.assistant_texts: list[str] = []
        self._streamed_ids: set[str] = set()
        self._current_stream_id: str | None = None

    def feed(self, event: dict) -> list[str]:
        """Absorb one event; return text fragments a streaming caller should see."""
        etype = event.get("type")
        if etype == "assistant":
            return self._feed_assistant(event)
        if etype == "stream_event":
            return self._feed_stream_event(event)
        if etype == "result":
            self._feed_result(event)
        elif etype == "system":
            self.session_id = event.get("session_id") or self.session_id
        # Everything else (user, rate_limit_event, and whatever the CLI adds
        # next) is intentionally ignored.
        return []

    def _feed_assistant(self, event: dict) -> list[str]:
        message = event.get("message")
        if not isinstance(message, dict):
            return []
        content = message.get("content")
        texts = [
            block["text"]
            for block in (content if isinstance(content, list) else [])
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        self.assistant_texts.extend(texts)
        # Under --include-partial-messages the deltas arrived first and the
        # assistant event repeats them verbatim; emitting both would show the
        # reader everything twice.
        message_id = message.get("id")
        if message_id in self._streamed_ids or (self._streamed_ids and message_id is None):
            return []
        return texts

    def _feed_stream_event(self, event: dict) -> list[str]:
        inner = event.get("event")
        if not isinstance(inner, dict):
            return []
        inner_type = inner.get("type")
        if inner_type == "message_start":
            message = inner.get("message")
            if isinstance(message, dict) and isinstance(message.get("id"), str):
                self._current_stream_id = message["id"]
            return []
        if inner_type != "content_block_delta":
            return []
        delta = inner.get("delta")
        if not isinstance(delta, dict) or delta.get("type") != "text_delta":
            return []
        text = delta.get("text")
        if not isinstance(text, str) or not text:
            return []
        if self._current_stream_id:
            self._streamed_ids.add(self._current_stream_id)
        return [text]

    def _feed_result(self, event: dict) -> None:
        self.saw_result = True
        self.session_id = event.get("session_id") or self.session_id
        if isinstance(event.get("result"), str):
            self.result_text = event["result"]
        if "structured_output" in event:
            self.structured_output = event["structured_output"]
        cost = event.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            self.cost_usd = float(cost)
        duration = event.get("duration_ms")
        if isinstance(duration, (int, float)):
            self.duration_ms = int(duration)
        self.is_error = bool(event.get("is_error"))
        if isinstance(event.get("subtype"), str):
            self.subtype = event["subtype"]

    @property
    def text(self) -> str:
        if self.result_text is not None:
            return self.result_text
        return "".join(self.assistant_texts)


class ClaudeRunner:
    """Spawns ``claude`` for a configured job and records what happened.

    Construct it with the resolved :class:`~hal_mary.config.Settings` and an open
    database connection; callers then never build argv, never pick a model and
    never have to remember the isolation flags.
    """

    def __init__(self, settings: Settings, conn: sqlite3.Connection) -> None:
        self.settings = settings
        self.conn = conn

    # -- public API ---------------------------------------------------------

    def run(
        self,
        job: str,
        prompt: str,
        *,
        schema: dict | None = None,
        resume: str | None = None,
        system_prompt: str | None = None,
        extra_context: str | None = None,
    ) -> ClaudeResult:
        """Run ``job``'s prompt to completion and return the result.

        Blocking, and bounded by the job's ``timeout_s``. Raises ``KeyError``
        only for an unknown job name — a typo, not a runtime condition. Every
        other failure comes back as ``ok=False`` with ``error`` set.
        """
        final: ClaudeResult | None = None
        for chunk in self._execute(
            job,
            prompt,
            streaming=False,
            schema=schema,
            resume=resume,
            system_prompt=system_prompt,
            extra_context=extra_context,
        ):
            if chunk.kind == "done" and chunk.result is not None:
                final = chunk.result
        if final is None:  # pragma: no cover - _execute always yields a done chunk
            raise RuntimeError("claude runner produced no result")
        return final

    def stream(
        self,
        job: str,
        prompt: str,
        *,
        resume: str | None = None,
        system_prompt: str | None = None,
        extra_context: str | None = None,
    ) -> Iterator[StreamChunk]:
        """Yield assistant text as it arrives, then one final ``done`` chunk.

        For the chat page's SSE endpoint. Adds ``--include-partial-messages`` so
        text arrives in small pieces rather than a message at a time.

        Abandoning the iterator (the browser closed the connection) kills the
        process group and still writes the ``claude_calls`` row.

        **Driving this from FastAPI.** It is a blocking generator, so it cannot
        run on the event loop. It also finishes by writing a ``claude_calls``
        row, and ``db.connect`` leaves ``check_same_thread`` on, so the thread
        that consumes the final chunk must be the thread that opened the
        connection. ``iterate_in_threadpool`` does **not** satisfy that: it hops
        threads per ``next()``, and the ``done`` chunk then raises
        ``sqlite3.ProgrammingError`` on a worker that did not create the
        connection — intermittently, because anyio often reuses the same worker,
        which is the worst way for it to fail. Build the connection *and* the
        runner inside one ``run_in_threadpool`` call::

            def _consume(prompt: str, out: queue.Queue) -> None:
                conn = db.connect(settings.db_path)      # this thread owns it
                try:
                    for chunk in ClaudeRunner(settings, conn).stream("chat", prompt):
                        out.put(chunk)
                finally:
                    out.put(None)
                    conn.close()

            # in the SSE endpoint
            out: queue.Queue = queue.Queue()
            task = asyncio.create_task(run_in_threadpool(_consume, prompt, out))
            while (chunk := await run_in_threadpool(out.get)) is not None:
                yield sse(chunk)
            await task

        One thread runs the whole call, owns the connection for its lifetime,
        and hands chunks across a queue.
        """
        # Look the job up before building the generator: a typo'd job name
        # should raise here, not on the caller's first ``next()`` halfway
        # through an already-open SSE response.
        self.settings.job(job)
        return self._execute(
            job,
            prompt,
            streaming=True,
            schema=None,
            resume=resume,
            system_prompt=system_prompt,
            extra_context=extra_context,
        )

    # -- argv ---------------------------------------------------------------

    def build_argv(
        self,
        job: JobConfig,
        *,
        streaming: bool = False,
        system_prompt: str | None = None,
        schema: dict | None = None,
        resume: str | None = None,
    ) -> list[str]:
        """Construct the full argv, prompt excluded — it travels over stdin.

        Exposed (rather than private) so the status page can show an operator
        exactly what a job would run without running it.
        """
        claude = self.settings.claude
        argv: list[str] = [
            claude.binary,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            job.model,
            "--permission-mode",
            claude.permission_mode,
            *ISOLATION_ARGS,
        ]
        if streaming:
            argv.append("--include-partial-messages")
        if system_prompt:
            argv += ["--system-prompt", system_prompt]
        if job.tools:
            argv += ["--tools", *job.tools, "--allowedTools", *job.tools]
        else:
            # The empty string is how the CLI spells "no tools at all"; omitting
            # the flag would give the job the full built-in set.
            argv += ["--tools", ""]
        if schema is not None:
            argv += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
        if job.max_budget_usd is not None:
            argv += ["--max-budget-usd", str(job.max_budget_usd)]
        if resume:
            argv += ["--resume", resume]
        else:
            # Jobs are one-shot; without this every run leaves a session on disk.
            argv.append("--no-session-persistence")
        return argv

    # -- prompt and paths ---------------------------------------------------

    def resolve_system_prompt(self, explicit: str | None) -> str | None:
        """The explicit argument wins, then ``claude.system_prompt_file``.

        A missing file is a warning, not a crash: hal-mary answering without its
        standing instructions beats hal-mary not answering. The warning names
        the path so the operator can see it on the status page.
        """
        if explicit is not None:
            text = explicit.strip()
            return text or None
        path = Path(self.settings.claude.system_prompt_file).expanduser()
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            log.warning("system prompt file %s unreadable (%s); running without one", path, exc)
            return None
        if not text:
            log.warning("system prompt file %s is empty; running without one", path)
            return None
        return text

    def scratch_dir(self) -> Path:
        """The subprocess cwd, created if absent.

        Never the repo root: the CLI reads ``CLAUDE.md`` and wanders into files
        under its working directory, and this application's own source is the
        last thing a football research job should be reasoning about. A relative
        ``claude.scratch_dir`` is resolved against the process working
        directory, which for the deployed daemon is the repo root.
        """
        path = Path(self.settings.claude.scratch_dir).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _transcript_path(self, scratch: Path, job_name: str) -> Path:
        """Where the raw stream-json for one call is kept.

        This is the debugging record behind a bad recommendation, so it is named
        for when and what: two calls in the same second still get separate files.
        """
        directory = scratch / "transcripts"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", job_name) or "job"
        return directory / f"{stamp}-{safe}-{uuid.uuid4().hex[:8]}.jsonl"

    # -- execution ----------------------------------------------------------

    def _execute(
        self,
        job_name: str,
        prompt: str,
        *,
        streaming: bool,
        schema: dict | None,
        resume: str | None,
        system_prompt: str | None,
        extra_context: str | None,
    ) -> Iterator[StreamChunk]:
        job = self.settings.job(job_name)  # KeyError names the configured jobs
        payload = prompt if not extra_context else f"{extra_context}{CONTEXT_SEPARATOR}{prompt}"
        prompt_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

        resolved_system_prompt = self.resolve_system_prompt(system_prompt)
        argv = self.build_argv(
            job,
            streaming=streaming,
            system_prompt=resolved_system_prompt,
            schema=schema,
            resume=resume,
        )

        started_at = db.utc_now()
        began = time.monotonic()
        sink = _EventSink()
        transcript: Path | None = None
        proc: subprocess.Popen[str] | None = None
        recorded = False
        result: ClaudeResult | None = None

        def finish(error: str | None, exit_code: int) -> ClaudeResult:
            nonlocal recorded
            built = self._build_result(
                sink,
                schema=schema,
                error=error,
                exit_code=exit_code,
                elapsed_ms=int((time.monotonic() - began) * 1000),
                transcript=transcript,
            )
            self._record_call(job, started_at, argv, prompt_hash, built)
            recorded = True
            return built

        try:
            # Opening the transcript is the first thing that can touch the disk,
            # and a full or read-only scratch volume must not escape as an
            # OSError: this runs inside an APScheduler job and an SSE handler,
            # both of which expect a result object, not an exception.
            try:
                scratch = self.scratch_dir()
                transcript = self._transcript_path(scratch, job.name)
                handle = transcript.open("w", encoding="utf-8")
            except OSError as exc:
                transcript = None
                result = finish(
                    f"scratch directory {self.settings.claude.scratch_dir!r} is unusable: {exc}",
                    _NEVER_RAN,
                )
                yield StreamChunk("done", "", result)
                return

            with handle:
                self._write_header(handle, job, argv, prompt_hash, started_at,
                                   resolved_system_prompt)
                try:
                    proc = subprocess.Popen(
                        argv,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        cwd=str(scratch),
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                        # Its own process group: `claude` spawns children, and
                        # killing only the child on timeout orphans them.
                        start_new_session=True,
                    )
                except OSError as exc:
                    result = finish(f"could not start {argv[0]}: {exc}", 127)
                    yield StreamChunk("done", "", result)
                    return

                stderr_box: list[str] = []
                lines: queue.Queue[str | None] = queue.Queue()
                stdout_thread = threading.Thread(
                    target=self._pump_stdout, args=(proc, lines), daemon=True
                )
                stderr_thread = threading.Thread(
                    target=self._drain_stderr, args=(proc, stderr_box), daemon=True
                )
                stdin_thread = threading.Thread(
                    target=self._write_stdin, args=(proc, payload), daemon=True
                )
                for thread in (stdout_thread, stderr_thread, stdin_thread):
                    thread.start()

                deadline = began + job.timeout_s
                timed_out = False
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    try:
                        line = lines.get(timeout=remaining)
                    except queue.Empty:
                        timed_out = True
                        break
                    if line is None:
                        break
                    handle.write(line if line.endswith("\n") else line + "\n")
                    for fragment in self._parse(line, sink):
                        if streaming:
                            yield StreamChunk("text", fragment)

                if not timed_out:
                    try:
                        proc.wait(timeout=max(deadline - time.monotonic(), 0.0))
                    except subprocess.TimeoutExpired:
                        timed_out = True

                if timed_out:
                    _kill_group(proc)
                    # Whatever was already in the queue is the last thing the
                    # model said before it stopped responding — the most useful
                    # part of the transcript for working out why a job hung.
                    stdout_thread.join(timeout=_STDERR_JOIN_TIMEOUT_S)
                    self._drain_pending(lines, handle, sink)
                    elapsed = time.monotonic() - began
                    result = finish(
                        f"timed out after {elapsed:.1f}s (limit {job.timeout_s}s)",
                        proc.returncode if proc.returncode is not None else -signal.SIGKILL,
                    )
                    yield StreamChunk("done", "", result)
                    return

                exit_code = proc.returncode if proc.returncode is not None else 0
                # Join before reading: the child has exited but the reader may
                # not have reached EOF, and losing this loses the only
                # explanation a failed call ever gets.
                stderr_thread.join(timeout=_STDERR_JOIN_TIMEOUT_S)
                stderr_text = "".join(stderr_box).strip()
                error = None
                if exit_code != 0:
                    error = stderr_text or f"claude exited {exit_code} with no stderr"
                result = finish(error, exit_code)
                yield StreamChunk("done", "", result)
        finally:
            if proc is not None and proc.poll() is None:
                _kill_group(proc)
            _close_pipes(proc)
            if not recorded:
                # The consumer abandoned the iterator mid-stream (the SSE client
                # went away). The call still happened and still cost money, so
                # it still gets a row.
                finish("stream abandoned by caller", _NEVER_RAN)

    @staticmethod
    def _write_header(
        handle: Any,
        job: JobConfig,
        argv: list[str],
        prompt_hash: str,
        started_at: str,
        system_prompt: str | None,
    ) -> None:
        """Open the transcript with the call itself.

        ``claude_calls.argv_json`` reduces the system prompt to a digest to keep
        the row readable, which is fine when it came from a file in git. A
        system prompt built dynamically — per pick, say — exists nowhere else,
        and without it a bad recommendation cannot be reconstructed. The
        transcript already has a retained path in ``output_path``, so it is the
        natural home for the full text and the unredacted argv.
        """
        header = {
            "type": _HEADER_EVENT_TYPE,
            "job": job.name,
            "model": job.model,
            "started_at": started_at,
            "prompt_hash": prompt_hash,
            "argv": argv,
            "system_prompt": system_prompt,
        }
        handle.write(json.dumps(header) + "\n")

    def _drain_pending(
        self, lines: queue.Queue[str | None], handle: Any, sink: _EventSink
    ) -> None:
        """Write and parse whatever the reader queued but the loop never took.

        Feeding the sink matters as much as writing the file: a call that
        produced a result event and then hung still spent the money, and the
        cost and session id belong on its row.
        """
        while True:
            try:
                line = lines.get_nowait()
            except queue.Empty:
                return
            if line is None:
                return
            handle.write(line if line.endswith("\n") else line + "\n")
            self._parse(line, sink)

    @staticmethod
    def _parse(line: str, sink: _EventSink) -> list[str]:
        """Parse one transcript line into the sink; skip anything unparseable."""
        stripped = line.strip()
        if not stripped:
            return []
        try:
            event = json.loads(stripped)
        except ValueError:
            # A truncated final line is normal when the CLI is killed. Skipping
            # it must not cost us the events that already arrived.
            log.debug("skipping unparseable stream-json line: %.120s", stripped)
            return []
        if not isinstance(event, dict):
            return []
        return sink.feed(event)

    @staticmethod
    def _pump_stdout(proc: subprocess.Popen[str], lines: queue.Queue[str | None]) -> None:
        try:
            if proc.stdout is not None:
                for line in proc.stdout:
                    lines.put(line)
        except (ValueError, OSError):  # pragma: no cover - pipe closed under us
            pass
        finally:
            lines.put(None)

    @staticmethod
    def _drain_stderr(proc: subprocess.Popen[str], box: list[str]) -> None:
        try:
            if proc.stderr is not None:
                box.append(proc.stderr.read())
        except (ValueError, OSError):  # pragma: no cover - pipe closed under us
            pass

    @staticmethod
    def _write_stdin(proc: subprocess.Popen[str], payload: str) -> None:
        """Deliver the prompt over stdin, in a thread.

        Prompts run to tens of kilobytes — a full draft board plus retrieved
        notes — which is past the pipe buffer, so a synchronous write would
        deadlock against a child that has not started reading yet.
        """
        try:
            if proc.stdin is not None:
                proc.stdin.write(payload)
                proc.stdin.close()
        except (BrokenPipeError, ValueError, OSError):
            pass

    # -- results and bookkeeping -------------------------------------------

    def _build_result(
        self,
        sink: _EventSink,
        *,
        schema: dict | None,
        error: str | None,
        exit_code: int,
        elapsed_ms: int,
        transcript: Path | None,
    ) -> ClaudeResult:
        text = sink.text
        structured: dict | None = None

        if error is None and not sink.saw_result:
            error = "claude produced no result event"
        if error is None and sink.is_error:
            error = f"claude reported an error result (subtype={sink.subtype})"
        if error is None and schema is not None:
            structured, error = _structured_from(sink)

        return ClaudeResult(
            ok=error is None,
            text=text,
            structured=structured,
            session_id=sink.session_id,
            cost_usd=sink.cost_usd,
            duration_ms=sink.duration_ms if sink.duration_ms is not None else elapsed_ms,
            exit_code=exit_code,
            error=error,
            raw_path=transcript,
        )

    def _record_call(
        self,
        job: JobConfig,
        started_at: str,
        argv: list[str],
        prompt_hash: str,
        result: ClaudeResult,
    ) -> None:
        """One ``claude_calls`` row per call, successes and failures alike.

        ``argv_json`` holds the argv with the prompt excluded — it went over
        stdin, so it was never in there — and with the system prompt reduced to
        a digest, which keeps the row small enough to read on the status page.
        ``prompt_hash`` is the sha256 of what was actually sent,
        ``extra_context`` included, so two calls that look alike can be told
        apart.

        One statement, so the connection's autocommit covers it; no explicit
        transaction is needed or wanted here.
        """
        self.conn.execute(
            """
            INSERT INTO claude_calls
                (job, model, argv_json, prompt_hash, started_at, duration_ms,
                 cost_usd, exit_code, session_id, output_path, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.name,
                job.model,
                json.dumps(_redact_argv(argv)),
                prompt_hash,
                started_at,
                result.duration_ms,
                result.cost_usd,
                result.exit_code,
                result.session_id,
                str(result.raw_path) if result.raw_path is not None else None,
                result.error,
            ),
        )


def _redact_argv(argv: list[str]) -> list[str]:
    """Replace the ``--system-prompt`` text with a digest for storage.

    The prompt itself is on stdin and never in argv, but the system prompt is,
    and it runs to thousands of identical characters on every single call. Kept
    verbatim it would be most of the ``claude_calls`` table. The digest still
    answers the question the row exists to answer: which system prompt was in
    force when this recommendation was made.
    """
    redacted = list(argv)
    try:
        index = redacted.index("--system-prompt") + 1
    except ValueError:
        return redacted
    if index < len(redacted):
        text = redacted[index]
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        redacted[index] = f"<system-prompt {len(text)} chars sha256:{digest}>"
    return redacted


def _structured_from(sink: _EventSink) -> tuple[dict | None, str | None]:
    """Resolve the structured object a schema request asked for.

    ``structured_output`` is the parsed object the CLI hands back and is
    preferred. When it is absent the ``result`` string is the same JSON, so it is
    worth a parse. When neither works the caller has to know: a draft advisor
    that silently treats a refusal as an empty recommendation is worse than one
    that reports a parse failure and falls back to the deterministic board.
    """
    if isinstance(sink.structured_output, dict):
        return sink.structured_output, None
    if isinstance(sink.result_text, str):
        try:
            parsed = json.loads(sink.result_text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed, None
    return None, "a JSON schema was requested but the output had no parseable structured output"


def _kill_group(proc: subprocess.Popen[str]) -> None:
    """Kill the whole process group and reap it.

    ``claude`` spawns children (MCP servers, hooks, its own helpers). Killing
    only the direct child leaves them running and holding the pipes open, so the
    timeout that was supposed to bound a job instead leaks processes.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:  # pragma: no cover - already gone
            pass
    try:
        proc.wait(timeout=_REAP_TIMEOUT_S)
    except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL is not refusable
        log.error("claude process group %s survived SIGKILL", proc.pid)


def _close_pipes(proc: subprocess.Popen[str] | None) -> None:
    if proc is None:
        return
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is None:
            continue
        try:
            pipe.close()
        except (OSError, ValueError):  # pragma: no cover - already closed
            pass
