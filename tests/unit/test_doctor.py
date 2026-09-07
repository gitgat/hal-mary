"""Tests for the deployment preflight, ``hal-mary doctor``.

The decision this module encodes is a policy one, so it is worth stating where
the tests can enforce it: **doctor reports, it never refuses on behalf of
``serve``.** Every check here answers a question an operator would otherwise
answer with an SSH session and four commands, and the two shell scripts in
``deploy/`` run it so that a first install and a redeploy fail *before* they
install a unit that cannot make a single model call.

What doctor must never do is touch the network or spawn ``claude``: it runs on a
box that may have neither, and the suite blocks both.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from hal_mary.config import load_settings
from hal_mary.doctor import Check, render, run_checks, worst_exit_code

MINIMAL_TOML = """
[claude]
binary = "claude"
default_model = "sonnet-test"
permission_mode = "dontAsk"
scratch_dir = ".scratch"
system_prompt_file = "prompts/system.md"

[paths]
prompts_dir = "prompts"
memory_dir = "memory"

[draft]
poll_seconds = 5
advise_within_picks = 2

[web]
host = "0.0.0.0"
port = 8080
session_cookie = "hal_mary_session"

[jobs.board_build]
tools = []
timeout_s = 45
max_budget_usd = 1.0
"""

FULL_ENV = {
    "ESPN_S2": "cookie",
    "SWID": "{00000000-0000-0000-0000-000000000001}",
    "LEAGUE_ID": "1234567",
    "TEAM_ID": "1",
    "SEASON": "2026",
    "WEB_PASSWORD": "long-random-string",
}


@pytest.fixture
def deployment(tmp_path: Path):
    """A tmp directory laid out the way a healthy box is.

    Returns ``(settings, home)``. ``home`` is a fake ``$HOME`` holding a
    ``.claude.json`` with a logged-in account, because that is the one piece of
    state doctor cannot get from the repo.
    """
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "config.toml").write_text(MINIMAL_TOML, encoding="utf-8")
    (root / "prompts").mkdir()
    (root / "prompts" / "system.md").write_text("system prompt", encoding="utf-8")
    (root / "memory").mkdir()
    (root / "memory" / "caroline.md").write_text("who she is", encoding="utf-8")

    data = tmp_path / "data"
    data.mkdir()

    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"uuid": "x"}}), "utf-8")
    (home / ".local" / "bin").mkdir(parents=True)
    claude = home / ".local" / "bin" / "claude"
    claude.write_text("#!/bin/sh\n", encoding="utf-8")
    claude.chmod(0o755)

    settings = load_settings(
        config_path=root / "config.toml",
        env={**FULL_ENV, "DB_PATH": str(data / "hal.db")},
    )
    return settings, home


def named(checks: list[Check], name: str) -> Check:
    matches = [check for check in checks if check.name == name]
    assert matches, f"no check named {name!r}; got {[c.name for c in checks]}"
    return matches[0]


def run(settings, home, **kwargs) -> list[Check]:
    kwargs.setdefault("which", lambda _binary: str(home / ".local" / "bin" / "claude"))
    return run_checks(settings, home=home, **kwargs)


# --- the happy box -----------------------------------------------------------


def test_a_healthy_box_passes_every_check(deployment):
    settings, home = deployment
    checks = run(settings, home)

    failed = [check for check in checks if not check.ok]
    assert failed == [], f"unexpected failures: {[(c.name, c.detail) for c in failed]}"
    assert worst_exit_code(checks) == 0


def test_doctor_never_spawns_anything(deployment, monkeypatch):
    """Doctor is safe to run on the pick-clock box and inside the test suite.

    ``subprocess`` is the thing that would make it slow, and the thing that
    would make the suite spawn the real ``claude``.
    """
    import subprocess

    def explode(*args, **kwargs):
        raise AssertionError("doctor must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)

    settings, home = deployment
    assert worst_exit_code(run(settings, home)) == 0


# --- the environment ---------------------------------------------------------


def test_missing_secrets_are_fatal_and_named(tmp_path, deployment):
    settings, home = deployment
    settings = load_settings(
        config_path=settings.config_path,
        env={k: v for k, v in FULL_ENV.items() if k != "WEB_PASSWORD"},
    )
    check = named(run(settings, home), "environment")

    assert check.ok is False
    assert check.fatal is True
    assert "WEB_PASSWORD" in check.detail


# --- claude ------------------------------------------------------------------


def test_a_missing_claude_binary_is_fatal(deployment):
    settings, home = deployment
    check = named(run(settings, home, which=lambda _binary: None), "claude binary")

    assert check.ok is False
    assert check.fatal is True
    assert "claude" in check.detail


def test_claude_installed_but_not_logged_in_is_fatal(deployment):
    """The one step only a human can do, and the only one that is invisible.

    A unit installed against a logged-out ``claude`` starts, serves, and gives
    no advice at all.
    """
    settings, home = deployment
    (home / ".claude.json").write_text(json.dumps({"userID": "x"}), encoding="utf-8")

    check = named(run(settings, home), "claude login")

    assert check.ok is False
    assert check.fatal is True
    assert "log in" in check.detail.lower()


def test_claude_never_run_is_reported_as_not_logged_in(deployment):
    settings, home = deployment
    (home / ".claude.json").unlink()

    check = named(run(settings, home), "claude login")

    assert check.ok is False
    assert "never" in check.detail.lower() or "not logged in" in check.detail.lower()


def test_an_unreadable_claude_config_does_not_crash_doctor(deployment):
    """Doctor is what you run when the box is broken; it may not add to it."""
    settings, home = deployment
    (home / ".claude.json").write_text("{not json", encoding="utf-8")

    check = named(run(settings, home), "claude login")

    assert check.ok is False
    assert check.fatal is True


def test_claude_config_dir_env_moves_where_the_login_is_looked_for(deployment, monkeypatch):
    settings, home = deployment
    elsewhere = home / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".claude.json").write_text(json.dumps({"oauthAccount": {}}), encoding="utf-8")
    (home / ".claude.json").unlink()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(elsewhere))

    assert named(run(settings, home), "claude login").ok is True


# --- prompts and memory ------------------------------------------------------


def test_a_missing_prompts_directory_is_fatal(deployment):
    settings, home = deployment
    (settings.claude.system_prompt_file).unlink()
    (settings.paths.prompts_dir).rmdir()

    check = named(run(settings, home), "prompts")

    assert check.ok is False
    assert check.fatal is True


def test_a_missing_memory_directory_is_a_warning_not_a_failure(deployment):
    """The boot-or-degrade decision, in one assertion.

    Thinner advice beats no advice, and nobody is watching at 2am. Doctor says
    so loudly and still exits zero, so a deploy that fixes something else is not
    blocked by it.
    """
    settings, home = deployment
    (settings.paths.memory_dir / "caroline.md").unlink()
    settings.paths.memory_dir.rmdir()

    checks = run(settings, home)
    check = named(checks, "memory")

    assert check.ok is False
    assert check.fatal is False
    assert worst_exit_code(checks) == 0


def test_an_empty_memory_directory_is_also_a_warning(deployment):
    settings, home = deployment
    (settings.paths.memory_dir / "caroline.md").unlink()

    check = named(run(settings, home), "memory")

    assert check.ok is False
    assert check.fatal is False
    assert "no standing" in check.detail.lower() or "empty" in check.detail.lower()


# --- the database ------------------------------------------------------------


def test_an_unwritable_database_directory_is_fatal(deployment):
    settings, home = deployment
    settings.db_path.parent.chmod(0o500)
    try:
        check = named(run(settings, home), "database directory")
    finally:
        settings.db_path.parent.chmod(0o755)

    assert check.ok is False
    assert check.fatal is True
    assert str(settings.db_path.parent) in check.detail


def test_a_database_directory_that_does_not_exist_yet_is_fine(deployment):
    """First install: the directory is created on the first connection."""
    settings, home = deployment
    settings.db_path.parent.rmdir()

    assert named(run(settings, home), "database directory").ok is True


def test_a_database_on_nfs_is_fatal(deployment):
    """The homelab's specific way to lose a season: /var/data is a TrueNAS
    export mounted on every node, and SQLite on NFS corrupts."""
    settings, home = deployment
    mounts = f"nas:/export {settings.db_path.parent} nfs4 rw 0 0\n/dev/vda1 / ext4 rw 0 0\n"

    check = named(run(settings, home, mounts=mounts), "database filesystem")

    assert check.ok is False
    assert check.fatal is True
    assert "nfs" in check.detail.lower()


def test_a_database_on_local_disk_passes(deployment):
    settings, home = deployment
    mounts = "/dev/vda1 / ext4 rw 0 0\n"

    check = named(run(settings, home, mounts=mounts), "database filesystem")

    assert check.ok is True
    assert "ext4" in check.detail


def test_the_longest_matching_mount_wins(deployment):
    """``/`` is a prefix of everything; the answer is the nearest mount point."""
    settings, home = deployment
    parent = settings.db_path.parent
    mounts = f"nas:/export / nfs4 rw 0 0\n/dev/vda1 {parent} ext4 rw 0 0\n"

    assert named(run(settings, home, mounts=mounts), "database filesystem").ok is True


def test_an_unreadable_mount_table_does_not_fail_the_check(deployment):
    settings, home = deployment

    check = named(run(settings, home, mounts=None), "database filesystem")

    assert check.ok is True
    assert check.fatal is False


def test_a_database_that_does_not_exist_yet_passes(deployment):
    settings, home = deployment
    check = named(run(settings, home), "database")

    assert check.ok is True
    assert "created on first use" in check.detail


def test_pending_migrations_are_reported_as_a_warning(deployment):
    """deploy.sh applies them a step later, so this informs rather than blocks."""
    import sqlite3

    settings, home = deployment
    sqlite3.connect(settings.db_path).close()  # a real, empty, unmigrated database
    check = named(run(settings, home), "database")

    assert check.ok is False
    assert check.fatal is False
    assert "migrat" in check.detail.lower()


def test_a_migrated_database_passes(deployment):
    from hal_mary import db

    settings, home = deployment
    conn = db.connect(settings.db_path)
    try:
        db.migrate(conn)
    finally:
        conn.close()

    assert named(run(settings, home), "database").ok is True


def test_a_corrupt_database_file_is_fatal(deployment):
    settings, home = deployment
    settings.db_path.write_bytes(b"this is not a sqlite file, not even close" * 8)

    check = named(run(settings, home), "database")

    assert check.ok is False
    assert check.fatal is True


# --- rendering ---------------------------------------------------------------


def test_render_puts_every_check_on_its_own_line_with_a_verdict(deployment):
    settings, home = deployment
    text = render(run(settings, home))

    for check in run(settings, home):
        assert check.name in text
    assert "ok" in text.lower()


def test_render_names_the_fatal_problems_at_the_end(deployment):
    settings, home = deployment
    text = render(run(settings, home, which=lambda _binary: None))

    assert "claude binary" in text
    # The summary line has to be the thing a human reads first when the script
    # scrolled past.
    assert text.strip().splitlines()[-1].lower().startswith(("fix", "1 problem", "problems"))


def test_worst_exit_code_is_one_when_anything_fatal_failed(deployment):
    settings, home = deployment
    assert worst_exit_code(run(settings, home, which=lambda _binary: None)) == 1


# --- the CLI wiring ----------------------------------------------------------


def test_doctor_is_a_subcommand(capsys):
    from hal_mary.cli import main

    with pytest.raises(SystemExit):
        main(["--help"])
    assert "doctor" in capsys.readouterr().out


def test_the_doctor_subcommand_exits_nonzero_when_something_is_fatal(monkeypatch, capsys):
    from hal_mary import cli

    monkeypatch.setattr(cli, "load_cli_settings", lambda: object())
    monkeypatch.setattr(
        cli,
        "run_doctor_checks",
        lambda settings: [Check("claude login", False, "not logged in", fatal=True)],
    )

    assert cli.main(["doctor"]) == 1
    assert "claude login" in capsys.readouterr().out


def test_the_doctor_subcommand_exits_zero_when_only_warnings(monkeypatch, capsys):
    from hal_mary import cli

    monkeypatch.setattr(cli, "load_cli_settings", lambda: object())
    monkeypatch.setattr(
        cli,
        "run_doctor_checks",
        lambda settings: [Check("memory", False, "no notes", fatal=False)],
    )

    assert cli.main(["doctor"]) == 0


def test_a_broken_config_file_is_reported_rather_than_traced(monkeypatch, capsys):
    """``doctor`` is the command someone runs when config.toml is the problem."""
    from hal_mary import cli
    from hal_mary.config import ConfigError

    def explode():
        raise ConfigError("config.toml not found (looked in /nowhere)")

    monkeypatch.setattr(cli, "load_cli_settings", explode)

    assert cli.main(["doctor"]) != 0
    assert "config.toml" in capsys.readouterr().err


def test_the_deployment_config_is_a_healthy_box(tmp_path):
    """The repo's own checkout, with a full .env, has nothing fatal wrong with
    it except whatever the developer's own machine lacks."""
    settings = load_settings(env={**FULL_ENV, "DB_PATH": str(tmp_path / "hal.db")})
    fatal = [c for c in run_checks(settings) if not c.ok and c.fatal]

    assert [c.name for c in fatal] in ([], ["claude binary"], ["claude login"]), textwrap.indent(
        render(run_checks(settings)), "  "
    )
