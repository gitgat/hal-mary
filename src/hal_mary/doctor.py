"""The deployment preflight: ``hal-mary doctor``.

Why this is a separate command rather than a check inside ``serve``
-------------------------------------------------------------------
Task 11 deferred the boot-or-degrade decision to the deployment unit, and this
is the answer: **doctor reports, ``serve`` boots anyway.**

The two failure modes are not symmetric. A service that refuses to start because
the memory directory is missing is down at 2am, with nobody watching, and the
person who could fix it is asleep; a service that starts and says on ``/status``
that its memory directory is missing is still serving the draft page, still
taking hand-entered picks, and is telling the truth in the one place someone
would look. Refusing to boot converts a degradation into an outage, and it does
it precisely when the degradation is cheapest to tolerate.

So the check runs at the two moments where a human *is* watching and a refusal
costs nothing: ``deploy/install.sh`` and ``deploy/deploy.sh`` both run
``hal-mary doctor`` and stop on a fatal result. What they stop is the *install*,
not the service — the difference between "we did not put a unit on this box that
cannot make a single model call" and "we took the advisor off the air on draft
morning".

The distinction that makes this work is :attr:`Check.fatal`. Missing secrets, a
missing or logged-out ``claude``, an unwritable database directory and a database
on NFS are fatal: nothing works, and installing over them buys a silent failure
later. A missing memory directory or a pending migration is a warning: it is
worth saying out loud, and it is not worth refusing over.

Two things doctor must never do
-------------------------------
* **Touch the network.** ``hal-mary espn-check`` is the command that asks ESPN
  whether the cookies still work, and expired cookies must not block a deploy
  that is fixing something else.
* **Spawn ``claude``.** It costs a second, and the test suite may never spawn
  the real binary. The login check therefore reads Claude Code's own config
  file rather than asking the CLI — see :func:`claude_login_check`.

Overlap with the status page is deliberate, not duplication: ``/status`` is the
running service's view of the same facts for whoever has a browser, and doctor
is the same facts for whoever has a shell and no service yet. Both read through
``Settings`` — ``missing_secrets()``, ``resolved_paths()`` — so a check cannot
drift from what the app actually resolves.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from shutil import which as shutil_which
from typing import Any

__all__ = [
    "PROC_MOUNTS",
    "REMOTE_FILESYSTEMS",
    "Check",
    "claude_config_path",
    "filesystem_for",
    "read_mounts",
    "render",
    "run_checks",
    "worst_exit_code",
]

#: Where the kernel publishes the mount table. Read rather than shelled out to,
#: because doctor spawns nothing.
PROC_MOUNTS = Path("/proc/mounts")

#: Filesystems SQLite must not live on. The homelab's ``/var/data`` is a TrueNAS
#: NFS export mounted on every node; SQLite's locking is advisory over NFS and a
#: WAL database on one corrupts. The others are here for the same reason.
REMOTE_FILESYSTEMS = frozenset(
    {"nfs", "nfs3", "nfs4", "cifs", "smbfs", "smb3", "fuse.sshfs", "afs", "9p", "glusterfs"}
)

#: Exit codes. Warnings alone exit zero: a deploy that fixes something else must
#: not be blocked by "there are no notes yet".
EXIT_OK = 0
EXIT_FATAL = 1

#: "``mounts`` was not supplied", as distinct from "the mount table is
#: unreadable". Both are legitimate inputs and they mean different things.
_UNSET = object()


@dataclass(frozen=True)
class Check:
    """One question about the box, and its answer.

    ``fatal`` is the policy dial, not a severity label: it is exactly "should
    ``install.sh`` or ``deploy.sh`` stop over this". Nothing here ever stops
    ``serve``.
    """

    name: str
    ok: bool
    detail: str
    fatal: bool = True


def claude_config_path(home: Path) -> Path:
    """Where Claude Code keeps the file that says whether it is logged in.

    ``CLAUDE_CONFIG_DIR`` moves it; otherwise it is ``~/.claude.json``. Reading
    this file is a heuristic and is described as one in the output — the
    definitive test is ``claude -p hello``, which doctor will not run because it
    costs a model call and a second of wall clock on every deploy.
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(override).expanduser() if override else home
    return base / ".claude.json"


def read_mounts() -> str | None:
    """``/proc/mounts``, or ``None`` where it cannot be read (macOS, a container).

    ``None`` is not a failure. Not being able to tell what filesystem a path is
    on is a different thing from that filesystem being NFS, and doctor says so
    rather than inventing a verdict.
    """
    try:
        return PROC_MOUNTS.read_text(encoding="utf-8")
    except OSError:
        return None


def filesystem_for(path: Path, mounts: str) -> str | None:
    """The filesystem type of the mount ``path`` sits on, longest match wins.

    ``/`` is a prefix of every path, so a naive scan reports the root
    filesystem for everything — which is exactly the answer that would miss an
    NFS mount at ``/var/data``.
    """
    best: tuple[int, str] | None = None
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        mount_point, fstype = _unescape_mount(fields[1]), fields[2]
        try:
            candidate = Path(mount_point)
            path.relative_to(candidate)
        except ValueError:
            continue
        depth = len(candidate.parts)
        if best is None or depth > best[0]:
            best = (depth, fstype)
    return best[1] if best else None


def _unescape_mount(field: str) -> str:
    """``/proc/mounts`` octal-escapes spaces and tabs in mount points."""
    out, index = [], 0
    while index < len(field):
        if field[index] == "\\" and field[index + 1 : index + 4].isdigit():
            out.append(chr(int(field[index + 1 : index + 4], 8)))
            index += 4
        else:
            out.append(field[index])
            index += 1
    return "".join(out)


def _first_existing_ancestor(path: Path) -> Path:
    """``path`` if it exists, else the nearest ancestor that does.

    A first install has no data directory yet — ``db.connect`` creates it — so
    the writability question is really about the deepest parent that is there.
    """
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return Path(path.anchor or ".")


# --- the individual checks ---------------------------------------------------


def environment_check(settings: Any) -> Check:
    missing = settings.missing_secrets()
    if missing:
        return Check(
            "environment",
            False,
            f"missing from .env: {', '.join(missing)} — nothing that needs them can run",
        )
    return Check("environment", True, "every required key is set in .env")


def claude_binary_check(settings: Any, which: Callable[[str], str | None]) -> Check:
    binary = settings.claude.binary
    resolved = which(binary)
    if resolved is None:
        candidate = Path(binary)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            resolved = str(candidate)
    if resolved is None:
        return Check(
            "claude binary",
            False,
            f"{binary!r} is not on the PATH. A systemd user unit does not inherit "
            f"your shell's PATH — check Environment=PATH= in the unit file.",
        )
    return Check("claude binary", True, f"found at {resolved}")


def claude_login_check(home: Path) -> Check:
    """Is the ``claude`` binary logged in as this user?

    The single step only a human can do, and the only one with no symptom until
    a job runs: a unit installed against a logged-out ``claude`` starts, serves
    every page, and produces no advice at all.
    """
    path = claude_config_path(home)
    fix = f"run `claude` once interactively as this user to log in ({path})"
    if not path.exists():
        return Check("claude login", False, f"claude has never been run as this user — {fix}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return Check("claude login", False, f"cannot read {path} ({exc}) — {fix}")
    # Presence, not truthiness: the key is written when the login completes.
    if not isinstance(data, dict) or "oauthAccount" not in data:
        return Check("claude login", False, f"claude is installed but not logged in — {fix}")
    return Check("claude login", True, f"logged in (per {path}; confirm with `claude -p hello`)")


def prompts_check(settings: Any) -> Check:
    prompts_dir = settings.paths.prompts_dir
    system_prompt = settings.claude.system_prompt_file
    if not prompts_dir.is_dir():
        return Check("prompts", False, f"{prompts_dir} does not exist — no job can build a prompt")
    if not system_prompt.is_file():
        return Check(
            "prompts", False, f"{system_prompt} is missing — every call loses its system prompt"
        )
    return Check("prompts", True, f"{prompts_dir} and {system_prompt.name} are in place")


def memory_check(settings: Any) -> Check:
    """Standing memory, which degrades the advice rather than stopping it.

    Not fatal on purpose. ``standing_memory()`` is on the pick-clock path and
    warns rather than raising, so the honest report is "this is worse, not
    broken".
    """
    memory_dir = settings.paths.memory_dir
    if not memory_dir.is_dir():
        return Check(
            "memory",
            False,
            f"{memory_dir} does not exist, so every prompt goes out without the "
            f"context that says who Caroline is and what the league's rules are. "
            f"Advice still runs; it is thinner.",
            fatal=False,
        )
    notes = sorted(p for p in memory_dir.glob("*.md") if p.is_file())
    if not notes:
        return Check(
            "memory",
            False,
            f"{memory_dir} exists but holds no standing-memory notes (*.md)",
            fatal=False,
        )
    return Check("memory", True, f"{len(notes)} standing note(s) in {memory_dir}")


def database_directory_check(settings: Any) -> Check:
    parent = settings.db_path.parent
    existing = _first_existing_ancestor(parent)
    if not os.access(existing, os.W_OK | os.X_OK):
        return Check(
            "database directory",
            False,
            f"{parent} is not writable (checked {existing}) — the database cannot be opened",
        )
    if parent == existing:
        return Check("database directory", True, f"{parent} exists and is writable")
    return Check(
        "database directory", True, f"{parent} will be created under {existing}, which is writable"
    )


def database_filesystem_check(settings: Any, mounts: str | None) -> Check:
    """Refuse a database on NFS. This is the homelab's way to lose a season."""
    parent = _first_existing_ancestor(settings.db_path.parent)
    if mounts is None:
        return Check(
            "database filesystem",
            True,
            "cannot read the mount table, so the filesystem under the database is "
            "unverified — check by hand that it is not an NFS mount",
            fatal=False,
        )
    fstype = filesystem_for(parent, mounts)
    if fstype is None:
        return Check(
            "database filesystem", True, f"no mount found for {parent}; unverified", fatal=False
        )
    if fstype.lower() in REMOTE_FILESYSTEMS:
        return Check(
            "database filesystem",
            False,
            f"{parent} is on {fstype}. SQLite on a network filesystem corrupts — "
            f"put DB_PATH on the box's own disk (~/hal-mary-data/hal.db).",
        )
    return Check("database filesystem", True, f"{parent} is on {fstype} (local disk)")


def database_check(settings: Any) -> Check:
    """Does the database open, and is its schema current?

    Pending migrations are a warning: ``deploy.sh`` applies them one step later,
    and blocking a deploy on the state the deploy is about to fix is theatre. A
    file that is not a database at all is fatal.
    """
    path = settings.db_path
    if not path.exists():
        return Check("database", True, f"{path} does not exist yet; it is created on first use")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return Check("database", False, f"{path} cannot be opened: {exc}")
    try:
        applied = {
            row[0]
            for row in conn.execute("SELECT filename FROM schema_migrations").fetchall()
        }
    except sqlite3.DatabaseError as exc:
        applied = None
        error = exc
    finally:
        conn.close()

    from hal_mary import db as _db

    available = {p.name for p in sorted(_db.MIGRATIONS_DIR.glob("*.sql"))}
    if applied is None:
        if not _looks_like_sqlite(path):
            return Check("database", False, f"{path} is not a SQLite database ({error})")
        return Check(
            "database",
            False,
            f"{path} has no schema_migrations table; {len(available)} migration(s) "
            f"will be applied",
            fatal=False,
        )
    pending = sorted(available - applied)
    if pending:
        return Check(
            "database",
            False,
            f"{len(pending)} migration(s) not applied yet: {', '.join(pending)}",
            fatal=False,
        )
    return Check("database", True, f"{path}, schema current ({len(applied)} migration(s))")


def _looks_like_sqlite(path: Path) -> bool:
    """Header check, with the empty file counted as a database.

    A zero-byte file is what ``sqlite3.connect`` leaves behind before the first
    write, so it is a brand-new database with no schema — the state a first
    install is in — and calling that corruption would fail every fresh box.
    """
    try:
        with path.open("rb") as handle:
            header = handle.read(16)
    except OSError:
        return False
    return header in (b"", b"SQLite format 3\x00")


# --- the whole preflight -----------------------------------------------------


def run_checks(
    settings: Any,
    *,
    home: Path | None = None,
    which: Callable[[str], str | None] | None = None,
    mounts: Any = _UNSET,
) -> list[Check]:
    """Every check, in the order an operator would want to read them.

    ``home``, ``which`` and ``mounts`` are seams so the suite can describe a box
    it does not have. ``mounts`` distinguishes "not supplied" (read
    ``/proc/mounts``) from an explicit ``None`` (unreadable), which is why its
    default is a sentinel rather than ``None``.
    """
    home = Path(home) if home is not None else Path.home()
    which = which or shutil_which
    mount_table = read_mounts() if mounts is _UNSET else mounts

    return [
        Check("config", True, f"loaded {settings.config_path}"),
        environment_check(settings),
        claude_binary_check(settings, which),
        claude_login_check(home),
        prompts_check(settings),
        memory_check(settings),
        database_directory_check(settings),
        database_filesystem_check(settings, mount_table),
        database_check(settings),
    ]


def worst_exit_code(checks: Iterable[Check]) -> int:
    """Nonzero only for a fatal failure. Warnings are for reading, not blocking."""
    return EXIT_FATAL if any(not c.ok and c.fatal for c in checks) else EXIT_OK


def render(checks: Iterable[Check]) -> str:
    """The report, written to be read in a scrolled-past deploy log.

    Every check gets a line so a passing box is evidence rather than silence,
    and the verdict is the **last** line because that is the one still on screen.
    """
    checks = list(checks)
    width = max((len(c.name) for c in checks), default=0)
    lines = []
    for check in checks:
        mark = "ok  " if check.ok else ("FAIL" if check.fatal else "warn")
        lines.append(f"  [{mark}] {check.name.ljust(width)}  {check.detail}")

    fatal = [c for c in checks if not c.ok and c.fatal]
    warnings = [c for c in checks if not c.ok and not c.fatal]
    lines.append("")
    if fatal:
        lines.append(
            f"Fix {len(fatal)} problem(s) before installing: {', '.join(c.name for c in fatal)}."
        )
    elif warnings:
        lines.append(
            f"Nothing fatal. {len(warnings)} thing(s) worth knowing: "
            f"{', '.join(c.name for c in warnings)}."
        )
    else:
        lines.append("All checks passed.")
    return "\n".join(lines)
