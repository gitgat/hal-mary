#!/usr/bin/env bash
#
# First-time setup of hal-mary on a freshly provisioned VM.
#
#   ssh bryan@hal-mary.thehalf.io          # the FQDN, never the bare short name
#   ~/hal-mary/deploy/install.sh
#
# Run deploy/provision-vm.sh first (node, claude, uv, sqlite3, linger, the data
# directory), and log `claude` in interactively once as this user — that is the
# one step no script can do, and this one refuses to proceed without it.
#
# What it does, in order: check, then act. Nothing is installed until every
# precondition has passed, so a box that is not ready ends up in the state it
# started in rather than carrying a unit that cannot make a single model call.
#
# Every environment variable below exists so the test suite can drive this script
# against a fabricated box (tests/unit/test_deploy.py). On the VM none of them
# are set and the defaults are the deployment.
set -euo pipefail

CHECKOUT="${HAL_MARY_HOME:-$HOME/hal-mary}"
REPO_URL="${HAL_MARY_REPO:-}"
UNIT_DIR="${HAL_MARY_UNIT_DIR:-$HOME/.config/systemd/user}"
DATA_DIR="${HAL_MARY_DATA_DIR:-$HOME/hal-mary-data}"
HEALTH_TIMEOUT="${HAL_MARY_HEALTH_TIMEOUT:-90}"
HEALTH_INTERVAL="${HAL_MARY_HEALTH_INTERVAL:-2}"

UNITS=(hal-mary.service hal-mary-backup.service hal-mary-backup.timer)

step() { printf '\n=== %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die() {
  printf '\nINSTALL STOPPED: %s\n' "$*" >&2
  exit 1
}

step "hal-mary install"
note "checkout   $CHECKOUT"
note "units      $UNIT_DIR"
note "data       $DATA_DIR"

# --- 1. uv -------------------------------------------------------------------
#
# provision-vm.sh installs it. Installing it here too means this script works on
# a box someone built by hand, which is the box that will exist in a hurry.

if ! command -v uv >/dev/null 2>&1; then
  if [ -x "$HOME/.local/bin/uv" ]; then
    export PATH="$HOME/.local/bin:$PATH"
  else
    step "installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
fi
command -v uv >/dev/null 2>&1 || die "uv is still not on PATH after trying to install it"
note "uv $(uv --version)"

# --- 2. the checkout ---------------------------------------------------------
#
# Cloned only when HAL_MARY_REPO says where from. A missing checkout with no
# repository named is an error rather than a silent clone from a guessed URL:
# the realistic cause is a wrong HAL_MARY_HOME, and the silent version of that
# mistake is a second, empty install nobody notices.

if [ ! -d "$CHECKOUT/.git" ]; then
  [ -n "$REPO_URL" ] ||
    die "no checkout at $CHECKOUT.
     Clone it first:  git clone <url> $CHECKOUT
     or rerun with:   HAL_MARY_REPO=<url> $0"
  step "cloning $REPO_URL"
  git clone "$REPO_URL" "$CHECKOUT" || die "clone failed"
fi
cd "$CHECKOUT"
note "at commit $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

# --- 3. .env -----------------------------------------------------------------
#
# Checked before doctor because "there is no .env" and "the ESPN cookies are
# missing from .env" have different fixes, and doctor can only say the second.

step "checking .env"
ENV_FILE="$CHECKOUT/.env"
[ -f "$ENV_FILE" ] ||
  die "no $ENV_FILE.
     cp $CHECKOUT/.env.example $ENV_FILE and fill it in — see docs/SETUP.md.
     ESPN cookies, the web password and DB_PATH live there, never in git."

# An assignment with something after the '=', ignoring comments. `cp .env.example
# .env` and never filling it in is the realistic failure, and it is not the same
# as having no file.
if ! grep -Eq '^[[:space:]]*[A-Z_]+=[^[:space:]]' "$ENV_FILE"; then
  die "$ENV_FILE has no values in it. Fill it in — see docs/SETUP.md."
fi
note "$ENV_FILE has values"

# --- 4. the data directory ---------------------------------------------------
#
# On local disk, never on /var/data: that is a TrueNAS NFS export mounted on
# every node in this homelab, and SQLite on NFS corrupts. `hal-mary doctor`
# checks the filesystem under DB_PATH itself; this checks the directory the
# runbook tells people to point DB_PATH at.

step "checking $DATA_DIR"
mkdir -p "$DATA_DIR" || die "cannot create $DATA_DIR"
[ -w "$DATA_DIR" ] || die "$DATA_DIR is not writable by $USER"
note "writable"

# --- 5. dependencies ---------------------------------------------------------

step "uv sync"
uv sync || die "uv sync failed"

# --- 6. preflight ------------------------------------------------------------
#
# The gate. doctor exits nonzero only for something fatal — no claude on PATH,
# claude not logged in, missing secrets, an unwritable or NFS-mounted database
# directory. Warnings (no standing memory notes yet, pending migrations) print
# and do not stop the install.

# By IP, deliberately. On this VM `hostname -f` answers `hal-mary`, which has no
# DNS record: it falls through Pi-hole's wildcard onto the keepalived ingress VIP
# and lands on the swarm manager. Printing `ssh bryan@hal-mary` in the most
# likely failure message would be handing someone the exact command this project
# documented after it went wrong once already.
BOX_ADDRESS="$(hostname -I 2>/dev/null | awk '{print $1}')"

step "hal-mary doctor"
if [ "${HAL_MARY_SKIP_DOCTOR:-}" = "1" ]; then
  # An escape hatch, because the claude-login check reads an undocumented key in
  # Claude Code's own config file: if that format ever drifts, this gate would
  # brick an install with no way through. The checks still run and still print.
  uv run hal-mary doctor ||
    note "^^ IGNORED, because HAL_MARY_SKIP_DOCTOR=1. Everything listed as FAIL
     above is still wrong on this box, and hal-mary will be installed on it
     anyway. If 'claude login' is among them, expect no advice at all."
else
  uv run hal-mary doctor ||
    die "this box is not ready (above). The most likely one, and the only one no
     script can do for you:

         ssh $(whoami)@${BOX_ADDRESS:-this box}
         claude          # log in interactively, once

     The service inherits that subscription session; there is no API key to set.
     Nothing has been installed. If you are certain the check is wrong, rerun
     with HAL_MARY_SKIP_DOCTOR=1."
fi

# --- 7. migrations -----------------------------------------------------------

step "database migrations"
uv run hal-mary migrate || die "migrations failed; nothing has been installed"

# --- 8. the units ------------------------------------------------------------
#
# Copied rather than symlinked. A symlink into the checkout means a `git pull`
# silently changes the unit systemd has already loaded, and the running service
# then no longer matches the file on disk. Copying makes install.sh the thing
# that changes a unit, which is what daemon-reload is for.

# The units say `%h/hal-mary`, which systemd expands to the home of the service user.
# When the checkout is somewhere else, that has to be substituted in rather than
# left to point at a directory that may not exist — a unit silently running the
# wrong code against the wrong config is worse than one that fails to start.
# When it is the default, nothing is substituted and the installed file is
# byte-identical to the source, so `diff` against deploy/ stays meaningful.
step "installing units into $UNIT_DIR"
mkdir -p "$UNIT_DIR"
for unit in "${UNITS[@]}"; do
  source_unit="$CHECKOUT/deploy/$unit"
  [ -f "$source_unit" ] || die "missing $source_unit"
  if [ "$CHECKOUT" = "$HOME/hal-mary" ]; then
    install -m 0644 "$source_unit" "$UNIT_DIR/$unit"
  else
    sed "s|%h/hal-mary|$CHECKOUT|g" "$source_unit" >"$UNIT_DIR/$unit"
    chmod 0644 "$UNIT_DIR/$unit"
    note "(pointing at $CHECKOUT)"
  fi
  note "$unit"
done

# --- 9. linger, so it survives a logout and a reboot -------------------------
#
# Without this the user manager stops when the last session closes: the service
# dies when the ssh session ends and never comes back at boot. It is the single
# difference between a service and a thing that runs while someone is logged in.

step "enabling linger for $USER"
loginctl enable-linger "$USER" || die "could not enable linger for $USER"

# --- 10. start ---------------------------------------------------------------

step "starting"
systemctl --user daemon-reload || die "daemon-reload failed"
systemctl --user enable --now hal-mary.service ||
  die "could not enable hal-mary.service — journalctl --user -u hal-mary -n 50"
systemctl --user enable --now hal-mary-backup.timer ||
  die "could not enable hal-mary-backup.timer"

# --- 11. and check that it is actually serving -------------------------------

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
     The units are installed and started; check by hand:
       systemctl --user status hal-mary
       journalctl --user -u hal-mary -n 50"
step "waiting for $URL"

deadline=$((SECONDS + HEALTH_TIMEOUT))
while :; do
  if curl -fsS --max-time 5 "$URL" >/dev/null 2>&1; then
    note "healthy"
    break
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    printf '\n' >&2
    systemctl --user status hal-mary.service --no-pager --lines=20 >&2 || true
    die "$URL did not answer within ${HEALTH_TIMEOUT}s.
     journalctl --user -u hal-mary -n 50"
  fi
  sleep "$HEALTH_INTERVAL"
done

step "installed"
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
PORT="${URL##*:}"
PORT="${PORT%%/*}"
cat <<EOF
    hal-mary is running and set to start at boot.

    Open it        http://${LAN_IP:-<this box>}:${PORT:-8080}  (from a phone on the LAN)
    Logs           journalctl --user -u hal-mary -f
    Status         systemctl --user status hal-mary
    Is ESPN ok?    the /status page in the app, not systemctl
    Redeploy       $CHECKOUT/deploy/deploy.sh
    Backups        $DATA_DIR/backups, nightly (systemctl --user list-timers)

    Next: open the app, log in with WEB_PASSWORD, and run a sync from the status
    page. See the runbook in README.md.
EOF
