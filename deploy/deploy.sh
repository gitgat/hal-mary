#!/usr/bin/env bash
#
# Redeploy hal-mary on the VM it already runs on.
#
#   ssh bryan@hal-mary.thehalf.io          # the FQDN, never the bare short name
#   ~/hal-mary/deploy/deploy.sh
#
# Idempotent, and it refuses to do damage. Every step that could put a broken
# hal-mary in front of a live draft stops the script instead:
#
#   * a dirty or untracked-file tree stops it, because `git pull` would clobber
#     work someone did on the box at 6am on draft morning;
#   * only --ff-only, because a merge or a rebase performed by a script on a
#     production box produces a commit nobody will ever review;
#   * a fatal `hal-mary doctor` stops it, before the database is touched;
#   * a red test suite stops it, before the service is restarted;
#   * and the service has to answer /healthz afterwards, because a deploy that
#     reports success without checking is worthless.
#
# Every environment variable below exists so the test suite can drive this
# script against a fabricated box (tests/unit/test_deploy.py). On the VM none of
# them are set and the defaults are the deployment.
set -euo pipefail

# The PATH this script needs, not the one it was handed.
#
# Ubuntu's default ~/.bashrc returns early for a non-interactive shell, so under
# `ssh box ~/hal-mary/deploy/deploy.sh` neither ~/.local/bin (uv) nor
# ~/.npm-global/bin (claude) is on PATH -- while an interactive `ssh box` then
# running the same line works fine. The asymmetry is the same one
# `hal_mary.doctor.UNIT_PATH` and the unit's `Environment=PATH=` exist for, and
# these three must agree; tests/unit/test_deploy.py pins them to each other.
#
# The ORDER is why this matters rather than merely being tidy: `git pull` runs
# before `uv sync`, so without this the checkout advances to the new commit and
# then the script aborts -- leaving the dependencies and the running service on
# the old one, with nothing on the box saying it is half-applied.
PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"
export PATH

# Absolute, and resolved before any `cd`, because the re-exec below runs it again.
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

CHECKOUT="${HAL_MARY_HOME:-$HOME/hal-mary}"
UNIT="${HAL_MARY_UNIT:-hal-mary.service}"
HEALTH_TIMEOUT="${HAL_MARY_HEALTH_TIMEOUT:-90}"
HEALTH_INTERVAL="${HAL_MARY_HEALTH_INTERVAL:-2}"

step() { printf '\n=== %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die() {
  printf '\nDEPLOY ABORTED: %s\n' "$*" >&2
  exit 1
}

# --- 0. the checkout has to be a checkout ------------------------------------

[ -d "$CHECKOUT" ] || die "no checkout at $CHECKOUT (set HAL_MARY_HOME, or run deploy/install.sh first)"
cd "$CHECKOUT"
git rev-parse --git-dir >/dev/null 2>&1 || die "$CHECKOUT is not a git repository"

# Carried across the re-exec below. Recomputing it after the pull would print
# the commit just pulled as the thing to roll back to, which is the one SHA that
# cannot help.
PREVIOUS="${HAL_MARY_PREVIOUS:-$(git rev-parse HEAD)}"
step "hal-mary deploy — $CHECKOUT"
note "current commit $PREVIOUS"
note "to roll back:  git -C $CHECKOUT checkout $PREVIOUS && $0"

# --- 1. refuse a dirty tree --------------------------------------------------
#
# --porcelain lists both modified tracked files and untracked ones, and both are
# reasons to stop: an untracked file here is something a human left behind, and a
# pull that happens to add a file of the same name fails halfway through.
# memory/league.md is gitignored, so the generated file is not what trips this.

DIRTY="$(git status --porcelain)"
if [ -n "$DIRTY" ]; then
  printf '\nUncommitted changes in %s:\n\n%s\n' "$CHECKOUT" "$DIRTY" >&2
  die "the working tree is not clean. Commit, stash or delete the files above, then rerun."
fi

# --- 2. fast-forward only ----------------------------------------------------

step "git pull --ff-only"
git pull --ff-only ||
  die "cannot fast-forward. This checkout has commits that are not on the remote;
     inspect with 'git -C $CHECKOUT log --oneline origin/HEAD..HEAD'.
     This script will not merge or rebase on a production box."
note "now at $(git rev-parse HEAD)"

# --- 2b. re-read this script, because the pull may have just replaced it ------
#
# bash reads a script incrementally and seeks by byte offset. A script rewritten
# underneath a running bash resumes at a stale offset: it can skip every
# remaining step and still exit 0 — no tests, no migration, no restart, and a
# clean exit code reporting a deploy that did none of the things a deploy is for.
#
# Measured, so the comment is not folklore: the git in use replaces a modified
# file by unlinking and creating a new inode, so bash keeps reading the original
# content through its already-open descriptor and the hazard does not currently
# bite. A writer that truncates the same inode does trigger it reliably, at any
# script size. That is an implementation detail of git to be independent of
# rather than to rely on: the cost of this line is one extra `git pull` that
# prints "Already up to date", and what it buys is that every step below runs
# from bytes read after the pull.
if [ -z "${HAL_MARY_REEXEC:-}" ]; then
  note "re-reading $SELF after the pull"
  exec env HAL_MARY_REEXEC=1 HAL_MARY_PREVIOUS="$PREVIOUS" bash "$SELF" "$@"
fi

# --- 3. dependencies ---------------------------------------------------------

step "uv sync"
uv sync || die "uv sync failed"

# --- 4. preflight ------------------------------------------------------------
#
# Before the database is touched, and before the tests. doctor exits nonzero only
# for something fatal — no claude, no login, missing secrets, an unwritable or
# NFS-mounted data directory — so a warning like "no standing memory notes" is
# printed and the deploy continues. See src/hal_mary/doctor.py for why the
# service itself never refuses to boot on these.

# The checks run either way. Skipping the *gate* is a decision someone should be
# able to make; skipping the *output* would mean they made it blind.
step "hal-mary doctor"
if [ "${HAL_MARY_SKIP_DOCTOR:-}" = "1" ]; then
  uv run hal-mary doctor ||
    note "^^ IGNORED, because HAL_MARY_SKIP_DOCTOR=1. Everything listed as FAIL
     above is still wrong on this box; you have only turned off the refusal."
else
  uv run hal-mary doctor ||
    die "preflight found a fatal problem (above). Fix it, or rerun with
     HAL_MARY_SKIP_DOCTOR=1 if you are deliberately deploying to a box that
     cannot yet run — for instance to ship the fix that makes it able to."
fi

# --- 5. migrations -----------------------------------------------------------
#
# Explicitly, rather than leaving them to the service's own startup: a failing
# migration should stop the deploy here, with the SQL error in front of whoever
# ran it, not inside a restarted service that then crash-loops.

step "database migrations"
uv run hal-mary migrate || die "migrations failed — the service has NOT been restarted"

# --- 6. the suite ------------------------------------------------------------
#
# Scrubbed, so that the suite this gates on is the suite CI runs. By the time
# control reaches here the re-exec above has put HAL_MARY_REEXEC=1 and
# HAL_MARY_PREVIOUS in the environment — and tests/unit/test_deploy.py spawns
# *this script* as a subprocess. Those subprocesses inherited both, skipped the
# re-exec they exist to test, and failed; the suite went red and the deploy
# refused to restart the service.
#
# That refusal was correct. The bug was that the suite was red for a reason that
# only existed in here, which made the condition permanent: no deploy could ever
# complete. The tests scrub every HAL_MARY_* variable on their own side, which is
# the general guard; this is the specific one — what this script sets, this
# script takes back off before handing over.

step "uv run pytest"
env -u HAL_MARY_REEXEC -u HAL_MARY_PREVIOUS uv run pytest ||
  die "the test suite is red. Nothing has been restarted; the service is still
     running the previous code. Deploying a red build to the box that advises on
     a live draft is not acceptable."

# --- 7. restart --------------------------------------------------------------

step "systemctl --user restart $UNIT"
systemctl --user restart "$UNIT" ||
  die "restart failed. Look at:  journalctl --user -u ${UNIT%.service} -n 50"

# --- 8. and check that it actually came up -----------------------------------
#
# The port is web.port in config.toml, so it is read from there rather than
# written down twice. HAL_MARY_HEALTH_URL overrides it for the tests.

health_url() {
  if [ -n "${HAL_MARY_HEALTH_URL:-}" ]; then
    printf '%s' "$HAL_MARY_HEALTH_URL"
    return
  fi
  uv run python -c 'from hal_mary.config import load_settings; s = load_settings(); print(f"http://127.0.0.1:{s.web.port}/healthz")'
}

URL="$(health_url || true)"
[ -n "$URL" ] ||
  die "could not work out the health-check URL (web.port in config.toml).
     The service HAS been restarted; check it by hand:
       systemctl --user status ${UNIT%.service}
       journalctl --user -u ${UNIT%.service} -n 50"
step "waiting for $URL"

deadline=$((SECONDS + HEALTH_TIMEOUT))
while :; do
  if curl -fsS --max-time 5 "$URL" >/dev/null 2>&1; then
    note "healthy"
    break
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    printf '\n' >&2
    systemctl --user status "$UNIT" --no-pager --lines=20 >&2 || true
    die "$URL did not answer within ${HEALTH_TIMEOUT}s. The restart was issued and
     the service is not serving. Look at:  journalctl --user -u ${UNIT%.service} -n 50
     To go back:  git -C $CHECKOUT checkout $PREVIOUS && $0"
  fi
  sleep "$HEALTH_INTERVAL"
done

step "deployed"
note "was  $PREVIOUS"
note "now  $(git rev-parse HEAD)"
note "logs journalctl --user -u ${UNIT%.service} -f"
