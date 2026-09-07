"""SQLite access: connections, migrations, and job-run bookkeeping.

The database is the whole of hal-mary's memory. It is a single file on local
disk (never NFS — SQLite over NFS corrupts) opened in WAL mode so the web app can
read while a scheduled job writes.

Migrations are plain ``.sql`` files in ``migrations/``, applied in filename order
and recorded in ``schema_migrations``. No ORM and no migration framework: the
schema is small, and a file of SQL is the clearest thing to review.

``job_run_started`` / ``job_run_finished`` live here rather than in a jobs package
because the scheduler needs them before that package exists.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "FINISHED_STATUSES",
    "connect",
    "job_run_finished",
    "job_run_started",
    "migrate",
]

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

#: Migration files are ``NNN_name.sql``; anything else in the directory is ignored.
_MIGRATION_RE = re.compile(r"^\d{3}_.*\.sql$")

#: Terminal statuses for a job run. ``running`` is written by job_run_started.
FINISHED_STATUSES = ("ok", "error")

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""


def utc_now() -> str:
    """An ISO-8601 UTC timestamp; the only time format written to the database."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open ``db_path`` with the settings every caller in this project expects.

    * ``row_factory = sqlite3.Row`` so callers index by column name.
    * foreign keys ON — SQLite leaves them off per-connection by default, so a
      connection opened without this silently accepts orphan rows.
    * WAL journalling so a reader (the web app) is never blocked by a writer.
    * ``isolation_level = None``: transactions are explicit. The implicit-BEGIN
      behaviour of the stdlib driver interacts badly with ``executescript`` and
      with DDL, and explicit is easier to reason about across a scheduler.
    """
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _migration_files(migrations_dir: Path) -> list[Path]:
    return sorted(
        (p for p in migrations_dir.iterdir() if p.is_file() and _MIGRATION_RE.match(p.name)),
        key=lambda p: p.name,
    )


def migrate(conn: sqlite3.Connection, migrations_dir: str | Path | None = None) -> list[str]:
    """Apply every not-yet-applied migration, in filename order.

    Returns the filenames applied by this call, so a second call returns ``[]``.
    Each migration and its ``schema_migrations`` row commit together: a migration
    that fails part-way leaves the database exactly as it was.
    """
    directory = Path(migrations_dir) if migrations_dir is not None else MIGRATIONS_DIR
    conn.execute(_SCHEMA_MIGRATIONS_DDL)

    already = {row["filename"] for row in conn.execute("SELECT filename FROM schema_migrations")}
    applied: list[str] = []

    for path in _migration_files(directory):
        if path.name in already:
            continue
        sql = path.read_text(encoding="utf-8")
        try:
            # executescript performs no implicit transaction control, so the
            # BEGIN below stays open until we commit — DDL and bookkeeping land
            # atomically.
            conn.executescript(f"BEGIN;\n{sql}")
            conn.execute(
                "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, ?)",
                (path.name, utc_now()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(path.name)

    return applied


def job_run_started(conn: sqlite3.Connection, job: str) -> int:
    """Record that ``job`` has started; return the ``job_runs`` row id."""
    cur = conn.execute(
        "INSERT INTO job_runs (job, started_at, status) VALUES (?, ?, 'running')",
        (job, utc_now()),
    )
    run_id = cur.lastrowid
    if run_id is None:  # pragma: no cover - sqlite always reports a rowid here
        raise RuntimeError("sqlite did not report a row id for the new job_runs row")
    return run_id


def job_run_finished(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    summary: str | None = None,
    error: str | None = None,
) -> None:
    """Complete the ``job_runs`` row ``run_id``.

    ``status`` must be ``ok`` or ``error``: a typo here would otherwise show up
    on the status page as a job that never finished.
    """
    if status not in FINISHED_STATUSES:
        raise ValueError(
            f"invalid job run status {status!r}; expected one of {', '.join(FINISHED_STATUSES)}"
        )
    cur = conn.execute(
        "UPDATE job_runs SET finished_at = ?, status = ?, summary = ?, error = ? WHERE id = ?",
        (utc_now(), status, summary, error, run_id),
    )
    if cur.rowcount == 0:
        raise LookupError(f"no job_runs row with id {run_id}")
