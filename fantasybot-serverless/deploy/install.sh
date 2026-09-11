#!/usr/bin/env bash
# Installer for fantasybot + Hermes for a single-user deployment (Linux/VPS).
# Autonomous: installs Hermes, the CLI, configures Claude, sets up the gateway and crons.
# Uses sudo only when needed (to install the CLI in /usr/local/bin and the system
# gateway). Idempotent where possible.
#
# Before running, export your Anthropic key:
#   export ANTHROPIC_API_KEY=sk-ant-...
# Optional parameters (with default values):
#   MODEL (anthropic/claude-sonnet-5), REVIEW_CRON ("0 7 * * *"),
#   BID_CRON ("15 20 * * *")   # 5-15 min before the market closes, in UTC
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
MODEL="${MODEL:-anthropic/claude-sonnet-5}"
REVIEW_CRON="${REVIEW_CRON:-0 7 * * *}"
BID_CRON="${BID_CRON:-15 20 * * *}"

echo "== 1/6  Installing Hermes Agent =="
command -v hermes >/dev/null 2>&1 || curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash

echo "== 2/6  Installing the fantasybot CLI =="
BIN_DIR="/usr/local/bin"; SUDO=""
if [ ! -w "$BIN_DIR" ]; then
  if [ "$(id -u)" != 0 ] && command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    BIN_DIR="$HOME/.local/bin"; mkdir -p "$BIN_DIR"
    echo "   (no permission on /usr/local/bin → using $BIN_DIR; add it to your PATH if needed)"
  fi
fi
$SUDO tee "$BIN_DIR/fantasybot" >/dev/null <<EOF
#!/bin/bash
cd "$REPO_DIR" && exec python3 -m fantasybot "\$@"
EOF
$SUDO chmod +x "$BIN_DIR/fantasybot"
"$BIN_DIR/fantasybot" --help >/dev/null && echo "   CLI OK."

echo "== 3/6  Configuring Claude (Anthropic directly) =="
: "${ANTHROPIC_API_KEY:?export ANTHROPIC_API_KEY before running}"
grep -q '^ANTHROPIC_API_KEY=' "$HERMES_HOME/.env" 2>/dev/null \
  || printf 'ANTHROPIC_API_KEY=%s\n' "$ANTHROPIC_API_KEY" >> "$HERMES_HOME/.env"
hermes config set model.provider anthropic >/dev/null
hermes config set model.default "$MODEL" >/dev/null
hermes config set model.base_url https://api.anthropic.com >/dev/null

echo "== 4/6  Copying agent assets =="
mkdir -p "$HERMES_HOME/skills" "$HERMES_HOME/scripts"
cp "$REPO_DIR/hermes/SOUL.md" "$REPO_DIR/hermes/USER.md" "$REPO_DIR/hermes/MEMORY.md" "$HERMES_HOME/"
cp -r "$REPO_DIR/hermes/skills/fantasy-manager" "$HERMES_HOME/skills/"
printf '#!/bin/bash\nfantasybot bid-run\n' > "$HERMES_HOME/scripts/bid-run.sh"
chmod +x "$HERMES_HOME/scripts/bid-run.sh"

echo "== 5/6  Gateway (so the crons can fire) =="
if [ "$(id -u)" = "0" ]; then
  hermes gateway install --system --run-as-user root 2>&1 | tail -2
else
  hermes gateway install 2>&1 | tail -2
fi

echo "== 6/6  Crons (daily review + bids at the close) =="
hermes cron list 2>/dev/null | grep -q fantasy-daily || \
  hermes cron create "$REVIEW_CRON" "Run your LALIGA Fantasy manager routine (fantasy-manager skill): review with fantasybot agent --json, set the best lineup, schedule the bids worth making with bid-plan using a sensible cap, handle sales and clause buyouts with judgment, and update your memory. Be concise." --skill fantasy-manager --name fantasy-daily >/dev/null
hermes cron list 2>/dev/null | grep -q fantasy-bids || \
  hermes cron create "$BID_CRON" --no-agent --script bid-run.sh --name fantasy-bids >/dev/null
hermes cron list 2>&1 | grep -E "Name:|Schedule:|Next run:"

cat <<'EOF'

== TODO: LALIGA login (one time) ==
  fantasybot login                       # prints the login URL
  # log in with Google; in DevTools > Network copy the 'authredirect://' URL
  # from the "(canceled)" request
  fantasybot login "authredirect://...?code=..."
  chmod 600 tokens.json

Then: fantasybot agent --json  (should return the report). Done!
EOF
