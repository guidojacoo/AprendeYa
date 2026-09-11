# Autonomous deployment (VPS or local)

Leave FantasyBot running on its own on top of [Hermes Agent](https://hermes-agent.nousresearch.com):
it reviews daily, decides, acts, and **bids at the last minute** when the market closes.
Single-user (self-host). Container-per-user is a future step.

## Requirements
- Linux (Ubuntu 22/24), macOS, or WSL2.
- **Anthropic** API key (Claude).
- Your LALIGA Fantasy account (Google login).

## Installation (one command)

```bash
git clone <repo> fantasybot && cd fantasybot
export ANTHROPIC_API_KEY=sk-ant-...        # your Claude key
bash deploy/install.sh
```

The script installs Hermes, the `fantasybot` CLI, configures Claude (Anthropic directly),
copies the agent (`SOUL.md`, `fantasy-manager` skill, `USER.md`, `MEMORY.md`), sets up
the **gateway** (so the crons can fire) and creates two crons:
- **`fantasy-daily`** (07:00 UTC by default): agent review + actions.
- **`fantasy-bids`** (20:15 UTC): bids right at the close according to the agent's plan.

Adjust the schedules with the `REVIEW_CRON` / `BID_CRON` variables (cron format, in UTC).
`BID_CRON` should land ~5-15 min before the market closes (the script waits
only until the last moment by reading the real time).

## LALIGA login (one time)

```bash
fantasybot login                       # prints the login URL
# log in with Google; in DevTools > Network (Preserve log) copy the
# 'authredirect://...' URL from the request that shows "(canceled)"
fantasybot login "authredirect://...?code=..."
chmod 600 tokens.json
```

The `refresh_token` lasts 90 days; the CLI renews it on its own. Test: `fantasybot agent --json`.

## Operation

```bash
hermes cron list                 # view the crons and their next run
hermes cron run fantasy-daily    # force a review now (on the next tick)
hermes insights --days 1         # tokens and cost for the day
fantasybot bid-plan              # view the pending last-minute bids
journalctl -u hermes-gateway -f  # gateway logs
```

## Watch a run (Mission Control)

To watch the agent's cycle live (or record a demo), bring up the UI and open it
over an SSH tunnel (it binds to `127.0.0.1`, never in the open):

```bash
# on the VPS:
fantasybot watch --hermes        # brings up the UI and fires the Hermes brain
# on your machine, in another terminal:
ssh -L 9137:127.0.0.1:9137 root@your-vps
# then open http://127.0.0.1:9137
```

`watch` with no flags only monitors: leave the UI open and you'll see the next cron
cycle. `--run` fires the deterministic agent (no LLM) instead of Hermes.

## Security
- `tokens.json`, `.state/`, `.cache/`, `.env` are in `.gitignore`. The API key lives
  only in the server's `~/.hermes/.env`. Treat `tokens.json` like a password.
- The gateway running as root is acceptable on a dedicated VPS/LXC; on bare metal, consider
  a dedicated user (future hardening).
