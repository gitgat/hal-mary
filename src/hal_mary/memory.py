"""Memory: what hal-mary has learned, and how it gets back into a prompt.

Everything the research jobs find out is one row in ``notes`` — a single fact,
when it was learned, who learned it, and the URL it came from. A Wednesday news
sweep discovers a running back is doubtful; the Sunday lineup check has to know.
This module is the whole of that continuity: writing notes, retrieving them, and
assembling the Markdown block that every Claude call is given.

Two stores, deliberately different:

* ``notes`` in SQLite, machine-written, full-text indexed, and pruned by age.
* ``memory/*.md`` on disk, human-written, always included in full, never pruned.
  Bryan and Caroline edit those by hand while the service is running, so they are
  read fresh on every call — never cached.

Retrieval is keyword search over FTS5. Embeddings are deliberately out of scope
(see docs/DECISIONS.md); the note corpus is small and the queries are proper
nouns, which is exactly where keyword search is strongest.
"""

from __future__ import annotations

import itertools
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import db

__all__ = [
    "Note",
    "build_context",
    "prune_notes",
    "search_notes",
    "standing_memory",
    "write_note",
    "write_notes",
]


#: Savepoint names must be unique within a connection's stack, and a bare
#: counter is enough: connections are never shared across threads.
_SAVEPOINT_SEQUENCE = itertools.count()


@dataclass(frozen=True)
class Note:
    """One thing hal-mary learned.

    ``created_at`` is not a field: it is stamped by :func:`write_note` so that
    every note carries the moment it entered the database and no caller can
    backdate one by accident.

    ``expires_at`` is an ISO-8601 timestamp for facts with a known shelf life —
    "out for Week 6" is worthless in Week 7. ``None`` means the fact does not go
    stale on a schedule; age filtering still applies to it. Whatever shape it
    arrives in, it is converted to UTC before storage (see
    :func:`_normalize_expiry`), because the read path compares it as a string.
    """

    text: str
    source_job: str
    topic: str | None = None
    player_name: str | None = None
    team_abbr: str | None = None
    source_url: str | None = None
    expires_at: str | None = None


@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Make a block atomic whether or not the caller is already in a transaction.

    ``db.transaction`` issues a bare ``BEGIN``, which SQLite refuses inside an
    open transaction. That is right for a top-level unit of work and wrong here:
    the normal shape for a job is "record the run and write its findings as one
    atomic unit", and a memory helper that cannot be called from inside that
    would force every job to choose between atomicity and using this module.

    A SAVEPOINT composes. Outside a transaction it starts one and ``RELEASE``
    commits it; inside one it is a nested checkpoint, so a failed batch unwinds
    to the savepoint and leaves the caller's transaction open and usable rather
    than poisoned.
    """
    name = f"hal_mary_memory_{next(_SAVEPOINT_SEQUENCE)}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield conn
        conn.execute(f"RELEASE {name}")
    except BaseException:
        try:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
        except sqlite3.Error:  # pragma: no cover - the savepoint is always live here
            pass
        raise


def _normalize_expiry(raw: str | None, source_job: str) -> str | None:
    """Validate ``expires_at`` and re-emit it in exactly ``db.utc_now``'s format.

    Expiry is compared as a *string* on the read path, which is only correct if
    every value in the column has the same shape. A job that wrote
    ``2026-09-07T09:00:00-05:00`` — 14:00Z, five hours in the future — would
    sort below a 12:00Z "now" and its note would be dropped as expired while it
    was still true. Silently losing live information is worse than raising, and
    worse than the note simply lingering.

    So the format is enforced here rather than trusted: anything
    ``datetime.fromisoformat`` accepts is converted to UTC (a naive timestamp is
    read as UTC, a bare date as midnight UTC), and anything else is a
    ``ValueError`` naming the job that produced it.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"expires_at must be an ISO-8601 timestamp, got {raw!r} (source_job={source_job!r})"
        ) from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _clean_text(note: Note) -> str:
    """Validate a note's text, returning it stripped.

    A blank note is always a bug upstream — a Claude reply that parsed into
    nothing, or a job that wrote its result before it had one. Storing it
    silently poisons retrieval with a row that matches nothing and pads every
    prompt, so it is an error at the door.
    """
    text = (note.text or "").strip()
    if not text:
        raise ValueError(f"note text is empty (source_job={note.source_job!r})")
    return text


def write_note(conn: sqlite3.Connection, note: Note) -> int:
    """Insert ``note``, stamping ``created_at``, and return its row id.

    The ``notes_fts`` index is maintained by triggers, so this writes only to
    ``notes``. Never insert into ``notes_fts`` directly.
    """
    text = _clean_text(note)
    expires_at = _normalize_expiry(note.expires_at, note.source_job)
    cur = conn.execute(
        """
        INSERT INTO notes
            (created_at, source_job, topic, player_name, team_abbr, text, source_url, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            db.utc_now(),
            note.source_job,
            note.topic,
            note.player_name,
            note.team_abbr,
            text,
            note.source_url,
            expires_at,
        ),
    )
    note_id = cur.lastrowid
    if note_id is None:  # pragma: no cover - sqlite always reports a rowid here
        raise RuntimeError("sqlite did not report a row id for the new notes row")
    return note_id


def write_notes(conn: sqlite3.Connection, notes: Iterable[Note]) -> list[int]:
    """Insert many notes in one transaction; return their row ids in order.

    All or nothing. A job that produced ten notes, one of them malformed, has
    produced a bad batch: half of it in the database would be read later as
    complete, and there is nothing in the row to say the rest went missing.

    Uses a SAVEPOINT rather than ``db.transaction``, so this composes: a caller
    already inside ``with db.transaction(conn):`` can write its notes as part of
    that larger unit, and a rejected batch unwinds only itself.
    """
    batch = list(notes)
    if not batch:
        return []
    with _atomic(conn):
        return [write_note(conn, note) for note in batch]


# --- retrieval ---------------------------------------------------------------

#: Runs of word characters. Everything else — apostrophes, quotes, asterisks,
#: colons, carets, hyphens, parentheses — is dropped before the string reaches
#: FTS5, which is what makes any input a safe literal search.
_FTS_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

#: ``SELECT n.*`` throughout: callers get whole note rows, not FTS columns.
_NOTE_COLUMNS = (
    "n.id, n.created_at, n.source_job, n.topic, n.player_name, "
    "n.team_abbr, n.text, n.source_url, n.expires_at"
)


def _fts_query(raw: str) -> str | None:
    """Turn arbitrary user text into an FTS5 MATCH expression, or None.

    This is the single most dangerous line of SQL in the project. FTS5's query
    language is not SQL, so parameter binding does not protect it: a bound
    string is still *parsed* as a query, and ``Ja'Marr Chase``, ``RB*``, a lone
    ``-`` or a stray ``"`` all raise ``sqlite3.OperationalError`` and take down
    whatever page asked. Every football name that matters has an apostrophe in
    it eventually, so this cannot be left to chance.

    The defence is to never pass anything through: keep only runs of word
    characters and re-emit each as a double-quoted FTS5 string, which is a
    literal phrase and can never be an operator. ``Ja'Marr`` becomes
    ``"Ja" OR "Marr"``, which still matches the stored note because the
    unicode61 tokenizer split the apostrophe out of the indexed text too.

    Terms are ORed rather than ANDed. Retrieval here feeds a prompt, and a
    four-word query that ANDs down to nothing gives Claude no context at all;
    OR plus ``ORDER BY rank`` puts the note matching every term first and lets
    ``limit`` drop the tail. Returns None when nothing survives (``"***"``),
    which callers read as "no usable query".
    """
    tokens = _FTS_TOKEN_RE.findall(raw)
    if not tokens:
        return None
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)


#: Everything that is not a word character, for name comparison. Names arrive
#: from two directions that do not agree on punctuation.
_NON_WORD_RE = re.compile(r"\W+", re.UNICODE)

#: The SQL name of :func:`_normalize_key`, registered per connection.
_NORM_FN = "hal_mary_norm"


def _normalize_key(value: Any) -> str | None:
    """Casefold and drop punctuation, so two spellings of a name compare equal.

    ``Ja'Marr Chase``, ``JaMarr Chase``, ``ja'marr chase`` and ``A.J. Brown``
    versus ``AJ Brown`` all collapse to the same key. This is not cosmetic: the
    writer is a Claude job transcribing a name off a web page and the reader
    filters with ESPN's spelling, so an exact match quietly returns nothing on
    the highest-value path there is — "everything we know about her starters" —
    and an empty notes section looks exactly like "we have learned nothing".
    """
    if not isinstance(value, str):
        return None
    return _NON_WORD_RE.sub("", value).casefold()


def _register_normalizer(conn: sqlite3.Connection) -> None:
    """Teach this connection the normaliser, so both sides use the same one.

    Doing the comparison in SQL with nested ``replace()`` calls would mean two
    implementations of "the same name" that could drift apart; registering the
    Python function means the column and the parameter are normalised by one
    piece of code. The cost is that the filter cannot use an index, which does
    not matter for a table holding a season's notes.
    """
    conn.create_function(_NORM_FN, 1, _normalize_key, deterministic=True)


def _normalized_terms(name: str, values: Sequence[str]) -> list[str]:
    """Reject a bare string, then normalise each value.

    A plain ``str`` is a perfectly good ``Sequence[str]``, so
    ``players="Bijan Robinson"`` would bind eighteen single letters and return
    nothing at all. That is a typo the type checker cannot see and the result
    cannot be distinguished from "we know nothing about him".
    """
    if isinstance(values, str | bytes):
        raise TypeError(f"{name} must be a list of strings, not a bare {type(values).__name__}")
    return [term for term in (_normalize_key(value) for value in values) if term]


def _age_cutoff(max_age_days: int | None) -> str | None:
    if max_age_days is None:
        return None
    return (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat(timespec="seconds")


def _filter_sql(
    *,
    players: Sequence[str] | None,
    topics: Sequence[str] | None,
    max_age_days: int | None,
    include_expired: bool,
) -> tuple[list[str], list[Any]]:
    """The WHERE clauses shared by both retrieval paths.

    ``players`` and ``topics`` are OR-within and AND-between: "anything we know
    about these two backs, on the subject of injuries". An empty list is not a
    filter that matches nothing — it is no filter, because callers build these
    lists from a roster and an empty roster should not silently blank the
    prompt. Both sides of the comparison go through :func:`_normalize_key`, so
    punctuation and case cannot silence a match.
    """
    clauses: list[str] = []
    params: list[Any] = []

    for column, name, values in (
        ("n.player_name", "players", players),
        ("n.topic", "topics", topics),
    ):
        if values is None:
            continue
        # Type-check before the emptiness check, so players="" is a rejected
        # bare string rather than an accidental "no filter".
        terms = _normalized_terms(name, values)
        if len(values) == 0:
            continue
        if not terms:
            # Every term normalised away, but the caller did ask to filter.
            # Matching nothing shows them the bug; matching everything would
            # bury it in a prompt that looks fine.
            clauses.append("0")
            continue
        clauses.append(f"{_NORM_FN}({column}) IN ({', '.join('?' * len(terms))})")
        params.extend(terms)

    cutoff = _age_cutoff(max_age_days)
    if cutoff is not None:
        clauses.append("n.created_at >= ?")
        params.append(cutoff)

    if not include_expired:
        # Lexical comparison of ISO-8601 UTC strings; see db.utc_now.
        clauses.append("(n.expires_at IS NULL OR n.expires_at > ?)")
        params.append(db.utc_now())

    return clauses, params


def search_notes(
    conn: sqlite3.Connection,
    query: str | None = None,
    *,
    players: Sequence[str] | None = None,
    topics: Sequence[str] | None = None,
    limit: int = 20,
    max_age_days: int | None = None,
    include_expired: bool = False,
) -> list[sqlite3.Row]:
    """Retrieve notes, by full-text query and/or by player and topic.

    With a ``query`` this searches the FTS index and orders by relevance, then
    by recency. Without one — or when the query sanitises down to nothing — it
    filters the ``notes`` columns directly and orders newest first, so
    "everything we know about this player" does not require inventing search
    terms.

    An empty result is a normal outcome. A brand-new database, a player nobody
    has researched yet, and a query that matches nothing all return ``[]``, and
    every caller renders that as a prompt section that simply is not there.
    """
    _register_normalizer(conn)
    match = _fts_query(query) if query else None
    clauses, params = _filter_sql(
        players=players,
        topics=topics,
        max_age_days=max_age_days,
        include_expired=include_expired,
    )
    row_limit = max(int(limit), 0)

    if match is not None:
        # notes_fts is deliberately not aliased: MATCH wants the table name on
        # its left, and aliasing it fails with a misleading "no such column".
        sql = [
            f"SELECT {_NOTE_COLUMNS} FROM notes n JOIN notes_fts ON notes_fts.rowid = n.id",
            "WHERE notes_fts MATCH ?",
        ]
        args: list[Any] = [match, *params]
        sql.extend(f"AND {clause}" for clause in clauses)
        # rank first, then recency, then id: a total order, so two identical
        # calls produce byte-identical prompts and cache the same way.
        sql.append("ORDER BY rank, n.created_at DESC, n.id DESC LIMIT ?")
    else:
        sql = [f"SELECT {_NOTE_COLUMNS} FROM notes n"]
        args = list(params)
        if clauses:
            sql.append("WHERE " + " AND ".join(clauses))
        sql.append("ORDER BY n.created_at DESC, n.id DESC LIMIT ?")

    args.append(row_limit)
    return list(conn.execute("\n".join(sql), args))


# --- standing memory ---------------------------------------------------------


def standing_memory(settings: Any) -> str:
    """Concatenate every ``*.md`` in ``settings.paths.memory_dir``, in name order.

    These files are the things that are true every time hal-mary thinks: who
    Caroline is, how she wants to be talked to, what the league's rules are.
    They go into every prompt in full, so they are kept short by hand.

    **Read fresh on every call, never cached.** Bryan and Caroline edit them
    while the service is running, and an advisor still quoting last week's
    version of `caroline.md` because a process started before the edit is a bug
    that would take days to notice.

    A missing directory yields ``""``, an unreadable file is skipped, and a file
    that is not valid UTF-8 is decoded with replacement characters. Nothing
    about the state of this directory may raise: a deployment whose memory
    directory is missing, or one file of which was saved in the wrong encoding,
    should give slightly thinner advice, not a stack trace on every page.

    ``league.md`` carries the ``hal-mary:preserve-below`` sentinel that the ESPN
    sync writes around. Nothing here interprets it: the file goes in whole,
    sentinel included, because the hand-written half below it is exactly the
    part Claude most needs.

    ``*.example.md`` files are skipped. ``memory/league.example.md`` is the
    tracked placeholder for the generated, gitignored ``league.md``; it is
    documentation for whoever sets up a box, not context for Claude.
    """
    directory = Path(settings.paths.memory_dir)
    if not directory.is_dir():
        return ""

    sections: list[str] = []
    for path in sorted(directory.glob("*.md"), key=lambda p: p.name):
        # `league.example.md` is the tracked placeholder that ships so a fresh
        # checkout has the template; the real `league.md` beside it is generated
        # and gitignored. Sending both would put "nothing has synced yet" into
        # the same prompt as the actual league.
        if path.name.endswith(".example.md"):
            continue
        try:
            # errors="replace", not strict: one curly apostrophe pasted from a
            # web page and saved as cp1252 is a byte that is not valid UTF-8,
            # and a UnicodeDecodeError here would take down every Claude call
            # in the process. A mojibake character in one line of standing
            # context costs nothing; losing the file costs the advice.
            content = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            # A file being rewritten by hand, or one we cannot read, must not
            # take down every prompt in the process. Skip it.
            continue
        if content:
            sections.append(f"## From {path.name}\n\n{content}")
    return "\n\n".join(sections)


# --- prompt context ----------------------------------------------------------

STANDING_HEADING = "What you always know"
NOTES_HEADING = "What we have learned recently"

#: A three-week-old injury note is usually wrong, and a wrong note is worse than
#: no note because Claude cannot tell that it is stale. Callers that want the
#: whole history — a recap, a chat question about the season — pass None.
DEFAULT_MAX_AGE_DAYS = 21


def _render_note(row: sqlite3.Row) -> str:
    """One note as one Markdown bullet, with absent fields left out entirely.

    ``- [2026-09-07] (Ja'Marr Chase, injury) Full practice — source: https://...``

    The text is collapsed onto a single line: a note whose text contains a
    newline would otherwise break out of the bullet list and read to Claude as
    a new section of the prompt.
    """
    line = f"- [{(row['created_at'] or '')[:10]}]"
    label = ", ".join(part for part in (row["player_name"], row["topic"]) if part)
    if label:
        line += f" ({label})"
    line += " " + " ".join((row["text"] or "").split())
    if row["source_url"]:
        line += f" — source: {row['source_url']}"
    return line


def build_context(
    conn: sqlite3.Connection,
    settings: Any,
    *,
    query: str | None = None,
    players: Sequence[str] | None = None,
    topics: Sequence[str] | None = None,
    note_limit: int = 20,
    max_age_days: int | None = DEFAULT_MAX_AGE_DAYS,
    extra_sections: Mapping[str, str] | None = None,
) -> str:
    """Assemble the Markdown memory block for a prompt.

    In order, skipping whatever is empty:

    1. ``## What you always know`` — the standing ``memory/*.md`` files.
    2. one ``## <key>`` section per entry of ``extra_sections``, in the order
       given. This is how a caller injects live state it already has in hand:
       the roster, the board, the last few draft picks.
    3. ``## What we have learned recently`` — notes retrieved for this call.

    Deterministic: the same database and the same arguments produce byte-identical
    output, because every query carries a total ordering and ``extra_sections``
    is emitted in insertion order. Tests depend on that, and so does prompt
    caching — a block that reshuffles itself between calls busts the cache on
    every request and costs real money.

    Empty is a valid answer. On a fresh install with no notes and no memory
    files this returns ``""``, and it never raises: the memory block is the one
    part of a prompt that must not be able to stop a job from running.
    """
    sections: list[str] = []

    standing = standing_memory(settings)
    if standing.strip():
        sections.append(f"## {STANDING_HEADING}\n\n{standing.strip()}")

    for heading, body in (extra_sections or {}).items():
        if body and body.strip():
            sections.append(f"## {heading}\n\n{body.strip()}")

    rows = search_notes(
        conn,
        query,
        players=players,
        topics=topics,
        limit=note_limit,
        max_age_days=max_age_days,
    )
    if rows:
        bullets = "\n".join(_render_note(row) for row in rows)
        sections.append(f"## {NOTES_HEADING}\n\n{bullets}")

    return "\n\n".join(sections)


# --- pruning -----------------------------------------------------------------


def prune_notes(conn: sqlite3.Connection, older_than_days: int) -> int:
    """Delete notes older than the cutoff, and return how many went.

    A note with a future ``expires_at`` survives regardless of age: "he is
    suspended through Week 8" was learned in July and is still the reason he is
    not startable in October. Everything else old enough is noise, and noise in
    a prompt costs tokens and misleads.

    The FTS index follows through the delete trigger, so no separate
    housekeeping is needed here.
    """
    cutoff = _age_cutoff(older_than_days)
    cur = conn.execute(
        "DELETE FROM notes WHERE created_at < ? AND (expires_at IS NULL OR expires_at <= ?)",
        (cutoff, db.utc_now()),
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
