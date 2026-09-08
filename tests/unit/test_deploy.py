"""Tests for the deployment artifacts in ``deploy/``.

A shell script nobody ran until deploy night is the classic way to lose an
evening, and these two scripts run at the two worst possible moments: the first
install, and a fix pushed during a season. So they are driven here the way they
will be driven there — a real git repository with a real remote, and stubbed
``uv``, ``systemctl``, ``loginctl`` and ``curl`` on ``PATH`` so the assertions
can be about *what the script decided to do* rather than about a box.

The stubs log every invocation, which is what most of these tests assert on: the
important property of ``deploy.sh`` is not that it prints the right thing when it
aborts, it is that when it aborts **it has not restarted the service**.

``git`` is deliberately not stubbed. The dirty-tree and fast-forward rules are
git's semantics, and a fake git would let a script pass here that a real one
rejects on the box.
"""

from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"
SCRIPTS = sorted(DEPLOY.glob("*.sh"))
UNITS = sorted(p for p in DEPLOY.iterdir() if p.suffix in {".service", ".timer"})

#: The two variables ``deploy.sh`` sets on *itself*, in the re-exec after the
#: pull. Every stub records them alongside its arguments, because they are the
#: ones that made this file fail on the box and nowhere else.
DEPLOY_MARKERS = ("HAL_MARY_REEXEC", "HAL_MARY_PREVIOUS")

#: Every stub logs to ``$STUB_LOG/<name>.log``, one invocation per line, and
#: exits nonzero when ``$STUB_LOG/fail-<token>`` exists for any argument token —
#: which is how a test says "pytest fails" or "doctor fails" without knowing how
#: the script spells the command. It also logs the environment the script handed
#: it, in ``$STUB_LOG/<name>.env.log``, one line per invocation: what
#: ``uv run pytest`` inherits is the whole point of one of the tests below, and a
#: separate file keeps the argument log something tests can match on verbatim.
STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_LOG/{name}.log"
printf '%s\\tHAL_MARY_REEXEC=%s\\tHAL_MARY_PREVIOUS=%s\\n' \\
    "$*" "${{HAL_MARY_REEXEC:-}}" "${{HAL_MARY_PREVIOUS:-}}" >> "$STUB_LOG/{name}.env.log"
for token in "$@"; do
    if [ -f "$STUB_LOG/fail-$token" ]; then
        echo "{name} $token failed (stub)" >&2
        exit "$(cat "$STUB_LOG/fail-$token")"
    fi
done
{extra}
exit 0
"""


class Box:
    """A fabricated deployment: an origin, a checkout, a fake HOME, and stubs."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.checkout = self.home / "hal-mary"
        self.origin = root / "origin.git"
        self.units_dir = self.home / ".config" / "systemd" / "user"
        self.data_dir = self.home / "hal-mary-data"
        self.stub_bin = root / "stub-bin"
        self.stub_log = root / "stub-log"
        for path in (self.home, self.stub_bin, self.stub_log, self.data_dir):
            path.mkdir(parents=True, exist_ok=True)

    # -- stubs ---------------------------------------------------------------

    def stub(self, name: str, extra: str = "") -> None:
        path = self.stub_bin / name
        path.write_text(STUB.format(name=name, extra=extra), encoding="utf-8")
        path.chmod(0o755)

    def fail(self, token: str, code: int = 1) -> None:
        """Make any stub invoked with ``token`` in its arguments exit ``code``."""
        (self.stub_log / f"fail-{token}").write_text(str(code), encoding="utf-8")

    def log(self, name: str) -> list[str]:
        path = self.stub_log / f"{name}.log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def marker_log(self, name: str) -> list[tuple[str, dict[str, str]]]:
        """One ``(arguments, {marker: value})`` pair per invocation of a stub."""
        path = self.stub_log / f"{name}.env.log"
        if not path.exists():
            return []
        seen = []
        for line in path.read_text(encoding="utf-8").splitlines():
            args, *assignments = line.split("\t")
            markers = dict(assignment.split("=", 1) for assignment in assignments)
            seen.append((args, markers))
        return seen

    # -- the repository ------------------------------------------------------

    def git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.checkout),
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, **self.git_identity()},
        )

    @staticmethod
    def git_identity() -> dict[str, str]:
        return {
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }

    def make_repo(self) -> None:
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(self.origin)],
            check=True,
            capture_output=True,
        )
        seed = self.root / "seed"
        seed.mkdir()
        (seed / "README.md").write_text("seed\n", encoding="utf-8")
        # The real deploy/ goes in the repository, because on the box the script
        # being executed *is* a tracked file that `git pull` can rewrite mid-run.
        # `run_in_checkout` executes that copy.
        (seed / "deploy").mkdir()
        for artifact in (*SCRIPTS, *UNITS):
            shutil.copy(artifact, seed / "deploy" / artifact.name)
            (seed / "deploy" / artifact.name).chmod(artifact.stat().st_mode)
        for args in (
            ("init", "-b", "main"),
            ("add", "-A"),
            ("commit", "-m", "seed"),
            ("remote", "add", "origin", str(self.origin)),
            ("push", "-u", "origin", "main"),
        ):
            self.git(*args, cwd=seed)
        subprocess.run(
            ["git", "clone", str(self.origin), str(self.checkout)],
            check=True,
            capture_output=True,
        )
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "test")

    def push_upstream_commit(self, message: str = "a fix") -> str:
        work = self.root / "upstream-work"
        if not work.exists():
            subprocess.run(
                ["git", "clone", str(self.origin), str(work)], check=True, capture_output=True
            )
        (work / f"{message.replace(' ', '-')}.txt").write_text(message, encoding="utf-8")
        self.git("add", "-A", cwd=work)
        self.git("commit", "-m", message, cwd=work)
        self.git("push", "origin", "main", cwd=work)
        return self.git("rev-parse", "HEAD", cwd=work).stdout.strip()

    # -- running the scripts -------------------------------------------------

    def env(self, **overrides: str) -> dict[str, str]:
        """The environment a script runs under here — this box's, not the operator's.

        Every inherited ``HAL_MARY_*`` variable is dropped before this box's own
        are put back. ``deploy.sh`` re-execs itself with ``HAL_MARY_REEXEC=1``
        and ``HAL_MARY_PREVIOUS=<sha>`` and then runs this suite; without the
        scrub, the subprocesses spawned below inherited both, skipped the
        re-exec they exist to test, and failed — so the deploy refused to
        restart the service, permanently. See the tests at the foot of this file.

        By prefix, not by name. The markers are what bit, but an operator with
        ``HAL_MARY_SKIP_DOCTOR`` exported, or a shell carrying the unit's
        ``HAL_MARY_CONFIG``, would steer this box just as invisibly, and so
        would whatever variable either script grows next.
        """
        return {
            **{key: value for key, value in os.environ.items() if not key.startswith("HAL_MARY_")},
            "HOME": str(self.home),
            "PATH": f"{self.stub_bin}:{os.environ['PATH']}",
            "STUB_LOG": str(self.stub_log),
            "HAL_MARY_HOME": str(self.checkout),
            "HAL_MARY_UNIT_DIR": str(self.units_dir),
            "HAL_MARY_DATA_DIR": str(self.data_dir),
            "HAL_MARY_HEALTH_URL": "http://127.0.0.1:8080/healthz",
            "HAL_MARY_HEALTH_TIMEOUT": "3",
            **self.git_identity(),
            **overrides,
        }

    def run_in_checkout(self, script: str, **overrides: str) -> subprocess.CompletedProcess:
        """Run the checkout's own copy — the one a pull can rewrite underneath bash."""
        return self._run(self.checkout / "deploy" / script, **overrides)

    def run(self, script: str, **overrides: str) -> subprocess.CompletedProcess:
        return self._run(DEPLOY / script, **overrides)

    def _run(self, script: Path, **overrides: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            env=self.env(**overrides),
            cwd=str(self.root),
            check=False,
        )

    def rewrite_deploy_sh_upstream(self) -> None:
        """Push a commit that inserts lines near the TOP of deploy/deploy.sh.

        Near the top on purpose. Bash reads a script in chunks and seeks by byte
        offset, so a rewrite that shifts every offset after the shebang is what
        makes it resume in the middle of a different line — the failure this
        reproduces. Appending at the end would shift nothing and prove nothing.
        """
        work = self.root / "upstream-work"
        if not work.exists():
            subprocess.run(
                ["git", "clone", str(self.origin), str(work)], check=True, capture_output=True
            )
        target = work / "deploy" / "deploy.sh"
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        padding = ["# padding that shifts every byte offset below it\n"] * 400
        target.write_text("".join([lines[0], *padding, *lines[1:]]), encoding="utf-8")
        self.git("add", "-A", cwd=work)
        self.git("commit", "-m", "rewrite deploy.sh", cwd=work)
        self.git("push", "origin", "main", cwd=work)


@pytest.fixture
def box(tmp_path: Path) -> Box:
    made = Box(tmp_path)
    made.make_repo()
    for name in ("uv", "systemctl", "loginctl", "curl"):
        made.stub(name)
    return made


@pytest.fixture
def installable(box: Box) -> Box:
    """A box ready for install.sh: a filled .env and the source unit files."""
    (box.checkout / ".env").write_text(
        "ESPN_S2=cookie\nSWID={x}\nLEAGUE_ID=1\nTEAM_ID=1\nSEASON=2026\nWEB_PASSWORD=pw\n",
        encoding="utf-8",
    )
    (box.checkout / "deploy").mkdir(exist_ok=True)
    for unit in UNITS:
        shutil.copy(unit, box.checkout / "deploy" / unit.name)
    return box


# --- the unit files ----------------------------------------------------------


def test_there_is_a_service_unit_a_backup_service_and_a_timer():
    assert {p.name for p in UNITS} == {
        "hal-mary.service",
        "hal-mary-backup.service",
        "hal-mary-backup.timer",
    }


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_every_unit_parses(unit: Path):
    parser = _unit_parser()
    parser.read_string(unit.read_text(encoding="utf-8"))
    assert parser.sections()


def _exec_start_programs() -> list[Path]:
    """The programs the units run, ``%h`` expanded against this user's home.

    Read out of the unit files rather than written down here, so the
    precondition below cannot drift away from what ``verify`` actually goes
    looking for.
    """
    programs: list[Path] = []
    for unit in UNITS:
        parser = _unit_parser()
        parser.read_string(unit.read_text(encoding="utf-8"))
        for section in parser.sections():
            exec_start = parser[section].get("ExecStart")
            if exec_start:
                programs.append(Path(exec_start.split()[0].replace("%h", str(Path.home()))))
    return programs


def _why_systemd_verify_cannot_run() -> str | None:
    """What this box is missing for ``systemd-analyze verify --user``, or None.

    A **precondition**, not a softened assertion, and the distinction is the
    point: a test skipped because its precondition is absent still runs, and
    still bites, everywhere the precondition holds — this workstation and the
    VM. A test whose assertion had been loosened to tolerate a runner would
    pass everywhere and check nothing anywhere. If you are tempted to relax
    the two asserts below instead of extending this function, that is the line
    you would be crossing.

    Three things are needed, not one. The binary is only the first, and it is
    the one a CI runner *does* have — which is exactly why checking it alone
    produced a red build that said nothing about this repository.
    """
    if shutil.which("systemd-analyze") is None:
        return "needs systemd-analyze"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime or not Path(runtime).is_dir():
        # verify --user starts a user manager, which needs a runtime directory.
        # Without one it dies with "Failed to initialize manager" before it has
        # read a single unit.
        return "needs XDG_RUNTIME_DIR: `systemd-analyze verify --user` starts a user manager"
    for program in _exec_start_programs():
        if not os.access(program, os.X_OK):
            # verify resolves ExecStart and fails when the program is missing.
            # On a CI runner uv lives in the actions tool cache, not ~/.local/bin.
            return f"needs an executable at every ExecStart; {program} is not one here"
    return None


def test_systemd_accepts_every_unit(tmp_path: Path):
    """The real parser, not ours. A unit that only configparser likes is a unit
    that fails at ``systemctl --user daemon-reload`` on the box."""
    blocked = _why_systemd_verify_cannot_run()
    if blocked is not None:
        pytest.skip(blocked)

    staged = tmp_path / "units"
    staged.mkdir()
    for unit in UNITS:
        shutil.copy(unit, staged / unit.name)

    result = subprocess.run(
        ["systemd-analyze", "verify", "--user", *[str(staged / u.name) for u in UNITS]],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr.strip() == "", result.stderr


def _unit_parser() -> configparser.ConfigParser:
    """A parser that reads systemd, not ini-with-Python-interpolation.

    ``interpolation=None`` because ``%h`` — systemd's expansion for the service
    user's home — is a syntax error to configparser's default interpolation, and
    ``%h`` is in every ``ExecStart`` here.
    """
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    return parser


def unit_section(name: str, section: str) -> dict[str, str]:
    parser = _unit_parser()
    parser.read_string((DEPLOY / name).read_text(encoding="utf-8"))
    return dict(parser[section])


def environment_lines(name: str) -> dict[str, str]:
    """``Environment=`` appears more than once, so configparser keeps only the
    last. Read them off the file instead."""
    out = {}
    for line in (DEPLOY / name).read_text(encoding="utf-8").splitlines():
        if line.startswith("Environment="):
            key, _, value = line[len("Environment=") :].partition("=")
            out[key] = value
    return out


def test_the_service_starts_hal_mary_serve_through_uv():
    exec_start = unit_section("hal-mary.service", "Service")["ExecStart"]

    assert exec_start.split()[0].startswith("%h/"), "systemd does not search PATH for ExecStart"
    assert exec_start.endswith("hal-mary serve")


def test_the_service_path_carries_the_two_per_user_bin_directories():
    """The single most likely reason the service starts and then cannot call
    Claude: a systemd user unit does not inherit the shell's PATH, and both
    ``uv`` and ``claude`` are installed per-user."""
    path = environment_lines("hal-mary.service")["PATH"]

    assert "%h/.local/bin" in path, "uv lives here"
    assert "%h/.npm-global/bin" in path, "claude lives here"
    assert "/usr/bin" in path, "and everything else still has to work"


def test_the_service_names_its_config_and_env_explicitly():
    """WorkingDirectory no longer decides where the database lives — every path
    is anchored to the config file — so the config file has to be named."""
    env = environment_lines("hal-mary.service")

    assert env["HAL_MARY_CONFIG"].endswith("config.toml")
    assert env["HAL_MARY_ENV"].endswith(".env")


def test_the_service_restarts_itself_after_a_crash():
    service = unit_section("hal-mary.service", "Service")

    assert service["Restart"] == "on-failure"
    assert int(service["RestartSec"]) >= 5


def test_the_service_waits_for_a_routable_network():
    unit = unit_section("hal-mary.service", "Unit")

    assert "network-online.target" in unit["After"]
    assert "network-online.target" in unit["Wants"], "After alone does not pull the target in"


def test_the_service_logs_to_the_journal_and_invents_no_log_file():
    service = unit_section("hal-mary.service", "Service")

    assert service["StandardOutput"] == "journal"
    assert service["StandardError"] == "journal"
    assert "hal-mary" in service["SyslogIdentifier"]
    assert not any(
        "/var/log" in line or "append:" in line
        for line in (DEPLOY / "hal-mary.service").read_text(encoding="utf-8").splitlines()
    )


def test_the_service_is_a_user_unit_not_a_system_one():
    """A system unit runs as root or a service account, neither of which has the
    ``claude`` subscription login the whole application depends on."""
    text = (DEPLOY / "hal-mary.service").read_text(encoding="utf-8")

    assert unit_section("hal-mary.service", "Install")["WantedBy"] == "default.target"
    assert "User=" not in text and "Group=" not in text


def test_the_backup_timer_is_nightly_and_catches_up_after_downtime():
    timer = unit_section("hal-mary-backup.timer", "Timer")

    assert timer["OnCalendar"].startswith("*-*-*")
    assert timer["Persistent"] == "true", "a box that was off must not silently skip a night"


def test_the_backup_service_is_oneshot_and_not_separately_enablable():
    parser = _unit_parser()
    parser.read_string((DEPLOY / "hal-mary-backup.service").read_text(encoding="utf-8"))

    assert parser["Service"]["Type"] == "oneshot"
    assert "Install" not in parser.sections(), "the timer owns it; nothing enables it directly"


def test_the_backup_service_runs_the_backup_subcommand_not_cp():
    exec_start = unit_section("hal-mary-backup.service", "Service")["ExecStart"]

    assert exec_start.endswith("hal-mary backup")
    assert "cp " not in exec_start


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="no systemd-analyze")
def test_the_timer_schedule_is_one_systemd_understands():
    schedule = unit_section("hal-mary-backup.timer", "Timer")["OnCalendar"]

    result = subprocess.run(
        ["systemd-analyze", "calendar", schedule], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert "Next elapse" in result.stdout


# --- shellcheck --------------------------------------------------------------


def shellcheck_binary() -> str | None:
    return os.environ.get("SHELLCHECK") or shutil.which("shellcheck")


@pytest.mark.skipif(shellcheck_binary() is None, reason="shellcheck not installed")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_shellcheck_is_clean(script: Path):
    result = subprocess.run(
        [shellcheck_binary(), "--severity=style", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_every_script_is_executable_and_fails_fast(script: Path):
    text = script.read_text(encoding="utf-8")

    assert os.access(script, os.X_OK), f"{script.name} is not executable"
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text, "a deploy that ignores a failed step is not a deploy"


# --- deploy.sh: refusing to do damage ----------------------------------------


def test_a_dirty_tree_stops_the_deploy_before_anything_else(box: Box):
    (box.checkout / "README.md").write_text("edited on the box\n", encoding="utf-8")

    result = box.run("deploy.sh")

    assert result.returncode != 0
    assert "README.md" in result.stdout + result.stderr, "name the file, do not just say 'dirty'"
    assert box.log("systemctl") == [], "nothing may be restarted"
    assert box.log("uv") == []


def test_an_untracked_file_also_stops_the_deploy(box: Box):
    """``git pull`` will happily clobber an untracked file that arrives in the
    pull, and the untracked file on this box is the one someone was debugging."""
    (box.checkout / "notes-from-draft-night.txt").write_text("keep me", encoding="utf-8")

    result = box.run("deploy.sh")

    assert result.returncode != 0
    assert "notes-from-draft-night.txt" in result.stdout + result.stderr


def test_a_diverged_branch_stops_the_deploy_rather_than_merging(box: Box):
    """--ff-only, and it has to be tested with a real divergence: a script that
    merges or rebases on the box produces a commit nobody will ever review."""
    (box.checkout / "local.txt").write_text("local work", encoding="utf-8")
    box.git("add", "-A")
    box.git("commit", "-m", "local commit that is not upstream")
    box.push_upstream_commit("upstream change")

    result = box.run("deploy.sh")

    assert result.returncode != 0
    assert box.log("systemctl") == []
    assert box.git("rev-list", "--count", "HEAD").stdout.strip() == "2", "no merge commit"


def test_a_failing_test_suite_aborts_before_the_restart(box: Box):
    """Deploying a red build to the box that advises on a live draft is not
    acceptable. The assertion that matters is the empty systemctl log."""
    box.push_upstream_commit()
    box.fail("pytest")

    result = box.run("deploy.sh")

    assert result.returncode != 0
    assert any("pytest" in line for line in box.log("uv")), "the suite has to have run"
    assert box.log("systemctl") == [], "a red build must not reach the service"


def test_a_fatal_preflight_aborts_before_the_tests(box: Box):
    """``doctor`` gates the deploy. This is the boot-or-degrade decision's other
    half: the check that ``serve`` will not make is made here, where a human is
    watching and a refusal costs nothing."""
    box.push_upstream_commit()
    box.fail("doctor")

    result = box.run("deploy.sh")

    assert result.returncode != 0
    assert not any("pytest" in line for line in box.log("uv"))
    assert box.log("systemctl") == []


def test_a_failing_migration_aborts_the_deploy(box: Box):
    box.push_upstream_commit()
    box.fail("migrate")

    result = box.run("deploy.sh")

    assert result.returncode != 0
    assert box.log("systemctl") == []


def test_a_checkout_that_is_not_a_git_repository_is_reported(box: Box, tmp_path: Path):
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()

    result = box.run("deploy.sh", HAL_MARY_HOME=str(elsewhere))

    assert result.returncode != 0
    assert str(elsewhere) in result.stdout + result.stderr


def test_a_missing_checkout_is_reported_not_created(box: Box, tmp_path: Path):
    missing = tmp_path / "gone"

    result = box.run("deploy.sh", HAL_MARY_HOME=str(missing))

    assert result.returncode != 0
    assert not missing.exists()


# --- deploy.sh: the happy path -----------------------------------------------


def test_a_clean_deploy_pulls_syncs_migrates_tests_restarts_and_verifies(box: Box):
    head = box.push_upstream_commit()

    result = box.run("deploy.sh")

    assert result.returncode == 0, result.stdout + result.stderr
    assert box.git("rev-parse", "HEAD").stdout.strip() == head
    uv = " | ".join(box.log("uv"))
    for step in ("sync", "doctor", "migrate", "pytest"):
        assert step in uv, f"deploy.sh never ran {step}"
    assert any("restart" in line for line in box.log("systemctl"))
    assert box.log("curl"), "a deploy that reports success without checking is worthless"


def test_the_steps_happen_in_an_order_that_cannot_ship_a_red_build(box: Box):
    box.push_upstream_commit()

    box.run("deploy.sh")

    uv = box.log("uv")
    tested = next(i for i, line in enumerate(uv) if "pytest" in line)
    migrated = next(i for i, line in enumerate(uv) if "migrate" in line)
    assert migrated < tested, "migrations before the suite that runs against them"


def test_the_previous_commit_is_printed_so_a_rollback_is_possible(box: Box):
    before = box.git("rev-parse", "HEAD").stdout.strip()
    box.push_upstream_commit()

    result = box.run("deploy.sh")

    assert before[:8] in result.stdout, "the SHA to roll back to has to be in the deploy output"


def test_a_deploy_with_nothing_to_pull_still_succeeds(box: Box):
    """Redeploying the same commit is how someone restarts after editing .env."""
    result = box.run("deploy.sh")

    assert result.returncode == 0, result.stdout + result.stderr
    assert any("restart" in line for line in box.log("systemctl"))


def test_running_the_deploy_twice_is_safe(box: Box):
    box.push_upstream_commit()

    first = box.run("deploy.sh")
    second = box.run("deploy.sh")

    assert (first.returncode, second.returncode) == (0, 0), second.stdout + second.stderr


def test_a_service_that_never_answers_healthz_fails_the_deploy(box: Box):
    """The restart succeeded and the service is not up. Reporting success here
    is the single most useless thing a deploy script can do."""
    box.stub("curl", extra="exit 7")

    result = box.run("deploy.sh")

    assert result.returncode != 0
    assert "healthz" in (result.stdout + result.stderr).lower() or "did not" in result.stdout
    assert any("restart" in line for line in box.log("systemctl")), "it did try"


def test_a_failed_restart_is_reported_with_where_to_look(box: Box):
    box.fail("restart")

    result = box.run("deploy.sh")

    assert result.returncode != 0
    assert "journalctl" in result.stdout + result.stderr


def test_the_health_url_is_derived_from_config_when_not_given(box: Box):
    """No hardcoded port: the port is ``web.port`` in config.toml, and a deploy
    script with its own copy of it is a deploy script that lies after someone
    changes it."""
    box.stub("uv", extra='if [ "$2" = "python" ]; then echo "http://h:9999/healthz"; fi')
    env = box.env()
    env.pop("HAL_MARY_HEALTH_URL")

    result = subprocess.run(
        ["bash", str(DEPLOY / "deploy.sh")], capture_output=True, text=True, env=env, check=False
    )

    assert "9999" in result.stdout + result.stderr, result.stdout
    assert any("9999" in line for line in box.log("curl"))


# --- install.sh --------------------------------------------------------------


def test_a_missing_env_file_stops_the_install(installable: Box):
    (installable.checkout / ".env").unlink()

    result = installable.run("install.sh")

    assert result.returncode != 0
    assert ".env" in result.stdout + result.stderr
    assert not installable.units_dir.exists(), "no unit may be installed"


def test_an_empty_env_file_stops_the_install(installable: Box):
    """A file created by ``cp .env.example .env`` and never filled in is the
    realistic case, and it is not the same as no file at all."""
    (installable.checkout / ".env").write_text("# nothing filled in\n", encoding="utf-8")

    result = installable.run("install.sh")

    assert result.returncode != 0
    assert ".env" in result.stdout + result.stderr


def test_a_fatal_preflight_stops_the_install(installable: Box):
    """``claude`` absent or not logged in: installing a unit that cannot make a
    single model call is worse than not installing one, because it looks fine."""
    installable.fail("doctor")

    result = installable.run("install.sh")

    assert result.returncode != 0
    assert not installable.units_dir.exists()
    assert not any("enable" in line for line in installable.log("systemctl"))


def test_an_unwritable_data_directory_stops_the_install(installable: Box):
    installable.data_dir.chmod(0o500)
    try:
        result = installable.run("install.sh")
    finally:
        installable.data_dir.chmod(0o755)

    assert result.returncode != 0
    assert str(installable.data_dir) in result.stdout + result.stderr


def test_a_missing_data_directory_is_created(installable: Box):
    installable.data_dir.rmdir()

    installable.run("install.sh")

    assert installable.data_dir.is_dir()


def test_a_successful_install_puts_all_three_units_in_place(installable: Box):
    result = installable.run("install.sh")

    assert result.returncode == 0, result.stdout + result.stderr
    installed = {p.name for p in installable.units_dir.iterdir()}
    assert installed == {
        "hal-mary.service",
        "hal-mary-backup.service",
        "hal-mary-backup.timer",
    }


def test_a_successful_install_enables_linger_reloads_and_starts(installable: Box):
    installable.run("install.sh")

    assert any("enable-linger" in line for line in installable.log("loginctl")), (
        "without linger the service dies at logout and never starts at boot"
    )
    systemctl = " | ".join(installable.log("systemctl"))
    assert "daemon-reload" in systemctl
    assert "enable --now hal-mary.service" in systemctl or "hal-mary.service" in systemctl
    assert "hal-mary-backup.timer" in systemctl


def test_a_successful_install_verifies_the_service_answers(installable: Box):
    installable.run("install.sh")

    assert installable.log("curl"), "an install that does not check is a hope"


def test_installing_twice_is_safe(installable: Box):
    first = installable.run("install.sh")
    second = installable.run("install.sh")

    assert (first.returncode, second.returncode) == (0, 0), second.stdout + second.stderr


def test_an_updated_unit_file_replaces_the_installed_one(installable: Box):
    installable.run("install.sh")
    installed = installable.units_dir / "hal-mary.service"
    installed.write_text("[Service]\nExecStart=/bin/false\n", encoding="utf-8")

    installable.run("install.sh")

    assert "hal-mary serve" in installed.read_text(encoding="utf-8")


def test_install_reports_the_next_step_a_human_has_to_take(installable: Box):
    result = installable.run("install.sh")

    lowered = result.stdout.lower()
    assert "journalctl" in lowered, "tell them where the logs are while they are still reading"


def test_install_refuses_a_checkout_that_is_not_there(installable: Box, tmp_path: Path):
    """It clones on request, but it never invents a checkout silently: the wrong
    HAL_MARY_HOME would otherwise produce a second, empty install."""
    missing = tmp_path / "nowhere"

    result = installable.run("install.sh", HAL_MARY_HOME=str(missing))

    assert result.returncode != 0
    assert str(missing) in result.stdout + result.stderr


def test_install_clones_when_told_where_from(installable: Box, tmp_path: Path):
    fresh = tmp_path / "fresh-checkout"

    result = installable.run(
        "install.sh", HAL_MARY_HOME=str(fresh), HAL_MARY_REPO=str(installable.origin)
    )

    assert (fresh / ".git").is_dir(), result.stdout + result.stderr


def test_a_health_url_that_cannot_be_derived_fails_fast(box: Box):
    """Not "poll an empty URL until the timeout".

    The restart has already happened by this point, so the message has to say so
    and hand over the two commands that answer what actually became of it.
    """
    box.stub("uv", extra='if [ "$2" = "python" ]; then exit 1; fi')
    env = box.env()
    env.pop("HAL_MARY_HEALTH_URL")

    result = subprocess.run(
        ["bash", str(DEPLOY / "deploy.sh")], capture_output=True, text=True, env=env, check=False
    )

    assert result.returncode != 0
    assert "journalctl" in result.stdout + result.stderr
    assert box.log("curl") == [], "nothing to poll, so it must not have polled"


def test_install_reports_the_lan_url_with_a_real_address(installable: Box):
    """Not a literal '$(hostname -I)' printed at someone at 1am."""
    result = installable.run("install.sh")

    assert "$(" not in result.stdout, result.stdout
    assert ":8080" in result.stdout


# --- the pull rewrites the script that is running ----------------------------


def test_a_pull_that_rewrites_deploy_sh_still_runs_every_remaining_step(box: Box):
    """A deploy that pulls a change to deploy.sh must still do everything.

    The hazard being guarded against is a script rewritten on disk while bash is
    still reading it: bash resumes at a stale byte offset and can skip the
    remaining steps while exiting 0 — no tests, no migration, no restart, and a
    clean exit code.

    Measured, because the mechanism matters: **the current git does not trigger
    it.** `git pull` replaces a modified file by unlinking and creating a new
    inode, so the running bash keeps reading the original content through its
    open descriptor. A writer that truncates the same inode (a `sed -i`, a
    hand-edit, a future git) does trigger it, reliably, at any script size. So
    this test cannot fail today for the reason it was written — which is exactly
    why the mitigation below is asserted separately, rather than trusting an
    implementation detail of git to keep holding.

    Driven through the checkout's own copy, because that is the only arrangement
    in which the hazard exists at all.
    """
    box.rewrite_deploy_sh_upstream()

    result = box.run_in_checkout("deploy.sh")

    assert result.returncode == 0, result.stdout + result.stderr
    uv = " | ".join(box.log("uv"))
    for step in ("sync", "doctor", "migrate", "pytest"):
        assert step in uv, f"deploy.sh skipped {step} after rewriting itself"
    assert any("restart" in line for line in box.log("systemctl"))
    assert box.log("curl"), "and it never checked whether the service came back"


def test_deploy_re_reads_itself_after_the_pull(box: Box):
    """The mitigation, asserted directly: everything after the pull is executed
    from bytes read after the pull, not from a descriptor opened before it."""
    box.push_upstream_commit()

    result = box.run_in_checkout("deploy.sh")

    assert result.stdout.count("re-reading") == 1, result.stdout


def test_the_re_exec_keeps_the_rollback_sha_from_before_the_pull(box: Box):
    """A re-exec that recomputed HEAD would print the commit just pulled as the
    thing to roll back to, which is the one SHA that cannot help."""
    before = box.git("rev-parse", "HEAD").stdout.strip()
    box.rewrite_deploy_sh_upstream()

    result = box.run_in_checkout("deploy.sh")

    assert before[:8] in result.stdout
    assert box.git("rev-parse", "HEAD").stdout.strip() != before


def test_the_re_exec_cannot_loop(box: Box):
    """Belt and braces on a construct that re-runs the whole script."""
    box.push_upstream_commit()

    result = box.run_in_checkout("deploy.sh")

    assert result.returncode == 0, result.stdout + result.stderr
    assert len([line for line in box.log("systemctl") if "restart" in line]) == 1


# --- the message that must not tell you to ssh to the swarm manager ----------


def test_install_never_prints_an_ssh_command_to_a_bare_short_name(installable: Box):
    """`hostname -f` returns `hal-mary` on the VM, and `ssh you@halmary`
    resolves through Pi-hole's wildcard onto the ingress VIP and lands on birdo,
    the swarm manager. This is the failure message most likely to be printed —
    'claude is not logged in' — so it is the one that must not say that."""
    import re

    installable.fail("doctor")

    result = installable.run("install.sh")
    output = result.stdout + result.stderr

    targets = re.findall(r"\bssh\s+\S+@(\S+)", output)
    assert targets, "the message should still tell them how to get onto the box"
    for target in targets:
        assert re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", target) or "." in target, (
            f"{target!r} is a bare short name; use the IP or a real FQDN"
        )


# --- the unit's PATH is the one doctor searches ------------------------------


def test_the_units_path_is_exactly_what_doctor_searches():
    """Two files, one fact. If the unit gains a directory and doctor does not,
    doctor passes a box whose service still cannot find `claude`."""
    from hal_mary.doctor import UNIT_PATH

    unit_value = environment_lines("hal-mary.service")["PATH"].replace("%h/", "~/")

    assert unit_value.split(":") == list(UNIT_PATH)


# --- HAL_MARY_HOME actually reaches the installed unit -----------------------


def test_the_installed_unit_points_at_the_checkout_that_installed_it(
    installable: Box, tmp_path: Path
):
    """The unit says %h/hal-mary; both scripts honour HAL_MARY_HOME. A unit that
    silently pointed somewhere other than the checkout it was installed from
    would run the wrong code against the wrong config."""
    elsewhere = tmp_path / "hal-mary-alt"
    shutil.copytree(installable.checkout, elsewhere)

    result = installable.run("install.sh", HAL_MARY_HOME=str(elsewhere))

    assert result.returncode == 0, result.stdout + result.stderr
    installed = (installable.units_dir / "hal-mary.service").read_text(encoding="utf-8")
    assert str(elsewhere) in installed
    assert "%h/hal-mary" not in installed


def test_the_installed_unit_is_byte_identical_when_the_checkout_is_the_default(
    installable: Box,
):
    """No substitution, no drift: the common case installs the file as written,
    so `diff` against deploy/ is a meaningful check on the box."""
    installable.run("install.sh")

    assert (installable.units_dir / "hal-mary.service").read_text(encoding="utf-8") == (
        DEPLOY / "hal-mary.service"
    ).read_text(encoding="utf-8")


# --- the timer's schedule means what the runbook says it means ---------------


def test_the_backup_timer_pins_its_timezone():
    """The runbook says 04:17 UTC. Without a pin that is true only because this
    VM happens to be Etc/UTC, and a timezone change would move the backup
    silently."""
    assert unit_section("hal-mary-backup.timer", "Timer")["OnCalendar"].endswith(" UTC")


# --- the doctor gate has an escape hatch on install too ----------------------


def test_install_can_be_forced_past_a_fatal_preflight(installable: Box):
    """The claude-login check reads an undocumented key in Claude Code's own
    config. If that format ever drifts, install would be bricked with no way
    through — so there is a way through."""
    installable.fail("doctor")

    result = installable.run("install.sh", HAL_MARY_SKIP_DOCTOR="1")

    assert result.returncode == 0, result.stdout + result.stderr
    assert installable.units_dir.is_dir()


def test_forcing_past_the_preflight_still_shows_what_was_ignored(installable: Box):
    """An operator who reaches for the override has to be able to see what they
    turned off, so the checks still run — they just stop gating."""
    installable.fail("doctor")

    result = installable.run("install.sh", HAL_MARY_SKIP_DOCTOR="1")

    assert any("doctor" in line for line in installable.log("uv")), "it still ran"
    assert "IGNORED" in result.stdout + result.stderr


def test_skipping_the_preflight_on_deploy_still_shows_what_was_ignored(box: Box):
    box.push_upstream_commit()
    box.fail("doctor")

    result = box.run("deploy.sh", HAL_MARY_SKIP_DOCTOR="1")

    assert result.returncode == 0, result.stdout + result.stderr
    assert any("doctor" in line for line in box.log("uv")), "it still ran"
    assert "IGNORED" in result.stdout + result.stderr


# --- the deploy's own environment must not reach the suite it gates on -------
#
# This is the bug that stopped a real deploy, and it is worth spelling out
# because every part of it behaved correctly. deploy.sh re-execs itself after the
# pull with HAL_MARY_REEXEC=1 (so it does not loop) and HAL_MARY_PREVIOUS=<sha>
# (so the rollback SHA survives). It then runs `uv run pytest` — this file —
# which spawns deploy.sh subprocesses. Those inherited both markers, skipped the
# re-exec they exist to test, and failed. The suite was red, so deploy.sh refused
# to restart the service, exactly as designed. The condition was permanent: the
# deploy could never complete.
#
# Both halves are asserted here, because either alone leaves the trap. The tests
# must not inherit the markers no matter how the suite was invoked, and deploy.sh
# must not hand them to the suite in the first place.


def test_the_fabricated_box_inherits_no_hal_mary_variable_it_did_not_set(
    box: Box, monkeypatch: pytest.MonkeyPatch
):
    """The half that makes these tests honest.

    Scrubbed by prefix rather than by name: the markers are what bit, but an
    operator with HAL_MARY_SKIP_DOCTOR exported, or a shell under the systemd
    unit's HAL_MARY_CONFIG, would steer the fabricated box just as invisibly —
    and the next variable either script grows would too.
    """
    for leaked in ("HAL_MARY_REEXEC", "HAL_MARY_PREVIOUS", "HAL_MARY_SKIP_DOCTOR"):
        monkeypatch.setenv(leaked, "1")
    monkeypatch.setenv("HAL_MARY_CONFIG", "/somewhere/else/config.toml")

    env = box.env()

    assert {key for key in env if key.startswith("HAL_MARY_")} == {
        "HAL_MARY_HOME",
        "HAL_MARY_UNIT_DIR",
        "HAL_MARY_DATA_DIR",
        "HAL_MARY_HEALTH_URL",
        "HAL_MARY_HEALTH_TIMEOUT",
    }


def test_the_deploy_markers_never_reach_the_suite_it_gates_on(box: Box):
    """The half that makes the gate meaningful: the suite deploy.sh runs is the
    same suite CI runs, not one steered by the re-exec that is running it."""
    box.push_upstream_commit()

    result = box.run_in_checkout("deploy.sh")

    assert result.returncode == 0, result.stdout + result.stderr
    suite_runs = [markers for args, markers in box.marker_log("uv") if "pytest" in args]
    assert suite_runs, f"deploy.sh never ran the suite: {box.marker_log('uv')}"
    for markers in suite_runs:
        assert markers == dict.fromkeys(DEPLOY_MARKERS, ""), (
            "uv run pytest inherited deploy.sh's own markers; the tests it runs "
            "spawn deploy.sh and will skip the re-exec they are testing"
        )


def test_the_deploy_tests_pass_with_the_markers_already_in_the_environment():
    """The reproduction, run as a test: the exact condition that was red on the
    VM and green everywhere else.

    A real pytest subprocess, because the fault was in what a subprocess
    inherits, and no in-process assertion can stand in for that. The three tests
    named are the three that failed on the box; naming them rather than running
    the whole file is what keeps this from recursing into itself.
    """
    broke_on_the_vm = (
        "test_the_previous_commit_is_printed_so_a_rollback_is_possible",
        "test_deploy_re_reads_itself_after_the_pull",
        "test_the_re_exec_keeps_the_rollback_sha_from_before_the_pull",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            *(f"{__file__}::{name}" for name in broke_on_the_vm),
        ],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        env={**os.environ, "HAL_MARY_REEXEC": "1", "HAL_MARY_PREVIOUS": "0" * 40},
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


# --- the PATH a non-interactive ssh actually has ------------------------------


def test_deploy_finds_uv_when_the_login_shell_did_not_export_it(box: Box):
    """`ssh box ~/hal-mary/deploy/deploy.sh` must work, not just an interactive run.

    Ubuntu's default ``.bashrc`` returns early for a non-interactive shell, so
    ``~/.local/bin`` is missing under ``ssh host 'cmd'`` and ``uv`` is not found
    — the same asymmetry ``doctor.UNIT_PATH`` exists for. The failure is nasty
    because of its ORDER: the script has already run ``git pull`` by then, so
    the checkout moves to the new commit while the dependencies and the running
    service stay on the old one, and nothing says so.
    """
    # uv exists only where a login shell would have found it.
    local_bin = box.home / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    (local_bin / "uv").write_text(
        f'#!/usr/bin/env bash\necho "uv $*" >> "{box.stub_log}/uv"\nexit 0\n'
    )
    (local_bin / "uv").chmod(0o755)
    (box.stub_bin / "uv").unlink(missing_ok=True)

    # A PATH with no uv anywhere on it, the way `ssh host 'cmd'` arrives on
    # Ubuntu: the operator's own PATH would smuggle the real one back in and
    # the test would pass without the script doing anything.
    result = box.run("deploy.sh", PATH=f"{box.stub_bin}:/usr/bin:/bin")

    assert "uv: command not found" not in result.stdout + result.stderr, (
        "deploy.sh did not put ~/.local/bin on PATH:\n" + result.stdout + result.stderr
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_deploy_sh_uses_the_same_path_as_the_unit(tmp_path: Path):
    """One definition of "where the tools are", not three.

    `doctor.UNIT_PATH` and `Environment=PATH=` in the unit are already pinned to
    each other. deploy.sh is the third place that has to agree, and it is the
    one nobody notices is wrong until a deploy half-applies.
    """
    from hal_mary.doctor import UNIT_PATH

    script = (DEPLOY / "deploy.sh").read_text()
    for entry in UNIT_PATH:
        if entry.startswith("~/"):
            needle = entry.replace("~/", "$HOME/")
            assert needle in script, f"deploy.sh never puts {needle} on PATH"
