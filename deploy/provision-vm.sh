#!/usr/bin/env bash
# Provision the hal-mary production VM. Idempotent: safe to re-run.
#
# Deliberately smaller than swarm-config/provision-dev.sh. Two things it does NOT do:
#
#   * No Docker. The design of record runs hal-mary as a systemd user service, not a container,
#     because the `claude` binary carries a subscription login tied to a user account.
#   * No Claude Code plugins or marketplaces. hal-mary invokes `claude` with
#     `--setting-sources "" --strict-mcp-config`, so every call ignores installed plugins, skills and
#     MCP servers by design. Installing them would cost provisioning time and change nothing.
#
# Run from dev-scratch:  ssh bryan@hal-mary.thehalf.io 'bash -s' < deploy/provision-vm.sh
set -euo pipefail

log() { printf '\n=== %s\n' "$*"; }

log "host $(hostname) $(. /etc/os-release && echo "$PRETTY_NAME") $(uname -m)"

log "base packages"
sudo DEBIAN_FRONTEND=noninteractive apt-get update -y -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  ca-certificates curl gnupg git jq sqlite3

log "node 22"
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - >/dev/null
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs
fi
node --version

log "per-user npm prefix (no sudo for global installs, keeps claude self-update working)"
mkdir -p "$HOME/.npm-global"
npm config set prefix "$HOME/.npm-global"
grep -q '.npm-global/bin' "$HOME/.bashrc" 2>/dev/null ||
  echo 'export PATH="$HOME/.npm-global/bin:$PATH"' >>"$HOME/.bashrc"
export PATH="$HOME/.npm-global/bin:$PATH"

log "claude code"
npm install -g @anthropic-ai/claude-code >/dev/null 2>&1 || npm install -g @anthropic-ai/claude-code
claude --version

log "uv"
if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
fi
grep -q '.local/bin' "$HOME/.bashrc" 2>/dev/null ||
  echo 'export PATH="$HOME/.local/bin:$PATH"' >>"$HOME/.bashrc"
export PATH="$HOME/.local/bin:$PATH"
uv --version

log "linger (so the user service survives logout and reboot)"
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" | grep -i Linger

log "data directory on local disk (never NFS — SQLite on NFS corrupts)"
mkdir -p "$HOME/hal-mary-data"
findmnt -no FSTYPE --target "$HOME/hal-mary-data"

log "done"
cat <<'EOF'

Remaining step, and only a human can do it:

    ssh bryan@hal-mary.thehalf.io
    claude          # log in interactively, once

The service inherits that subscription session. There is no API key to configure, and hal-mary
cannot make a single model call until this is done.
EOF
