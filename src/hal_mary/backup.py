"""Nightly snapshots of the one thing here that cannot be regenerated.

Everything else hal-mary holds can be rebuilt: the ESPN state resyncs in
seconds, the draft board rebuilds from a job. The **notes** cannot. They are the
season's accumulated research, written by every job and retrieved by every
prompt, and they exist nowhere but ``hal.db``.

Why this is not ``cp``
----------------------
The database runs in WAL mode (see :func:`hal_mary.db.connect`) and the service
writes to it while a backup runs. In WAL mode a committed row can live entirely
in ``hal.db-wal`` with nothing of it in ``hal.db`` yet, so copying the main file
produces a snapshot that opens cleanly, passes ``integrity_check``, and is
missing the most recent writes — which are the interesting ones. Copying all
three files is no better: they are copied at different instants, so the ``-wal``
can be newer than the ``-shm`` and the header it is validated against.

``sqlite3.Connection.backup`` is SQLite's own online backup API. It reads
through the same page cache and WAL the writers use, holds a read transaction
for each step, and produces one self-contained file with no sidecars.

Why it is a subcommand rather than a shell script
-------------------------------------------------
The database's location is not a constant: ``DB_PATH`` is anchored to the
directory holding the resolved ``config.toml``, so a shell script would have to
re-implement that resolution and would eventually back up the wrong file — or,
worse, a file that does not exist, silently, at 4am. Reading the path through
:class:`hal_mary.config.Settings` is the only way to be sure the backup is of the
database the service actually opens. ``deploy/hal-mary-backup.timer`` runs it.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["STAMP_FORMAT", "BackupResult", "backup_database", "backup_files"]

#: Sortable, unambiguous, and filename-safe on every filesystem. ``ls`` sorts
#: these in chronological order, which is what someone restoring at 2am needs.
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"

#: The same shape as :data:`STAMP_FORMAT`, for recognising our own files. A
#: regex rather than a ``strptime`` round-trip because this is a name test, not a
#: date: it must not turn a stray filename into a naive datetime to throw away.
_STAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")

#: Written first, renamed into place last. A half-written file that looks like a
#: backup is worse than no file, because it is the one that gets restored.
PARTIAL_SUFFIX = ".partial"


@dataclass(frozen=True)
class BackupResult:
    path: Path
    size_bytes: int
    pruned: list[Path]


def backup_files(dest_dir: Path, db_path: Path) -> list[Path]:
    """Backups of ``db_path`` in ``dest_dir``, oldest first.

    Matched by name, not by "everything in the directory". The retention window
    deletes what this returns, and a backup directory is a directory on
    someone's disk — deleting a file we did not write is not a mistake anyone
    gets to make twice.
    """
    if not dest_dir.is_dir():
        return []
    stem = db_path.stem
    suffix = db_path.suffix or ".db"
    return sorted(
        path
        for path in dest_dir.glob(f"{stem}-*{suffix}")
        if path.is_file() and _has_stamp(path.name, stem, suffix)
    )


def _has_stamp(name: str, stem: str, suffix: str) -> bool:
    """Is ``name`` one of ours — ``<stem>-<stamp><suffix>``?"""
    return bool(_STAMP_RE.match(name[len(stem) + 1 : len(name) - len(suffix)]))


def backup_database(
    settings: Any,
    *,
    now: datetime | None = None,
    keep: int | None = None,
    dest_dir: Path | None = None,
) -> BackupResult:
    """Snapshot the live database, then prune to the retention window.

    Raises ``FileNotFoundError`` when there is no database yet: "backed up
    nothing, successfully" is the report that hides a misconfigured ``DB_PATH``
    for a whole season.
    """
    source = settings.db_path
    if not source.is_file():
        raise FileNotFoundError(
            f"no database at {source} — nothing to back up. Check DB_PATH in .env; "
            f"a relative value is resolved against {settings.config_path.parent}."
        )

    destination = Path(dest_dir) if dest_dir is not None else settings.backup_dir()
    destination.mkdir(parents=True, exist_ok=True)

    stamp = (now or datetime.now(UTC)).strftime(STAMP_FORMAT)
    suffix = source.suffix or ".db"
    target = destination / f"{source.stem}-{stamp}{suffix}"
    partial = target.with_name(target.name + PARTIAL_SUFFIX)

    _copy_online(source, partial)
    partial.replace(target)

    window = settings.backup.keep if keep is None else keep
    pruned = _prune(destination, source, window)
    return BackupResult(path=target, size_bytes=target.stat().st_size, pruned=pruned)


def _copy_online(source: Path, target: Path) -> None:
    """SQLite's online backup API, cleaning up after itself on failure."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        src.close()


def _prune(dest_dir: Path, db_path: Path, keep: int) -> list[Path]:
    """Delete all but the newest ``keep``. A ``keep`` below 1 prunes nothing.

    Refusing to act on a nonsensical window is deliberate: the failure mode of
    "keep = 0" read as "delete everything" is unrecoverable, and the failure
    mode of ignoring it is a full disk that someone notices.
    """
    if keep < 1:
        return []
    existing = backup_files(dest_dir, db_path)
    doomed = existing[: max(0, len(existing) - keep)]
    for path in doomed:
        path.unlink(missing_ok=True)
    return doomed
