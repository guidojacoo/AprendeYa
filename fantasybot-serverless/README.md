# fantasybot

An agent for **LALIGA Fantasy**: it connects to the game's (unofficial) API and to
external sources (futbolfantasy.com) to read your squad, the market and value
trends, and to operate (bid, sell, buyout clauses, set your lineup).

## Two ways to run it

**As a CLI, on your machine** — everything below still works exactly as it did.
State in `.state/`, `python -m fantasybot agent`, the lot.

**As a serverless bot, 24/7, for €0** — GitHub Actions wakes a Vercel function
every few minutes, the function does whatever is due, and dies. State lives in
Supabase, so nothing needs to stay running: no VPS, no daemon, no PC left on.

```
GitHub Actions  ──HTTP──>  Vercel Function  ──>  LaLiga Fantasy API
   (the clock)                (the bot)              (the game)
                                  │
                                  └──> Supabase PostgreSQL (the memory)
```

Same code, same decisions, same strategies — only the persistence layer and the
scheduler differ, and both are chosen from the environment. **[→ DEPLOY.md](DEPLOY.md)**
has the full walkthrough, including an honest accounting of every free-tier limit
and exactly how precise a free scheduler can be.

## Getting started

Login (Google account, 2-step OAuth flow):

```bash
# Step 1: generate the login URL
python -m fantasybot login
# Log in with Google in the browser. In DevTools > Network (Preserve log),
# copy the 'authredirect://...' URL from the request that shows "(canceled)".

# Step 2: exchange the code for tokens
python -m fantasybot login "authredirect://com.lfp.laligafantasy?code=..."
```

The token lasts 24h and refreshes itself. If you ever need to do it by hand:
`python -m fantasybot refresh`. The `refresh_token` lasts 90 days; when it expires,
log in again.

**Stuck on Google's account-chooser screen, or "the login code was rejected" every
time?** This is almost always Google's account cookies, not fantasybot — a browser
signed into several Google accounts over time can pile up enough cookie state that
the account-chooser step hangs or errors out (Google's well-known "header too long"
class of issue). By the time it finally resolves, the code has often expired.
**Try step 1 again in an Incognito/Private window** (or a browser you're not signed
into much) — this fixes it for most people immediately. If it still fails, an
`AADB2Cxxxxx` code in the error detail (rerun with the actual redirect URL and check
the exception) narrows it down further.

## Commands

```bash
python -m fantasybot me           # your user
python -m fantasybot leagues      # your leagues
python -m fantasybot team         # your squad
python -m fantasybot market       # your league's market
python -m fantasybot lineup       # your lineup
python -m fantasybot trends       # who's rising/falling in value (futbolfantasy)
python -m fantasybot value-snapshot      # bank today's OFFICIAL LaLiga market values
python -m fantasybot onces real-madrid   # a team's likely starting XI
python -m fantasybot flip [--horizon N]  # resale opportunities
python -m fantasybot optimize [--apply]  # best lineup (apply with --apply)
python -m fantasybot needs [--days N]    # squad gaps and signings
python -m fantasybot rivals              # rivals' finances, cash estimate & clause audit
python -m fantasybot history             # trading/flip ROI leaderboard & P&L
python -m fantasybot scout <player>      # multi-season scouting report (points, tier, starter %)
python -m fantasybot scout --team        # full squad scouting audit
python -m fantasybot sell <playerId> <price>     # list a player for sale
python -m fantasybot bid <marketId> <amount>     # bid on a market player
python -m fantasybot cancel-bid <marketId> <bidId>
python -m fantasybot clause <playerId> <amount>  # pay a buyout clause
python -m fantasybot bid-plan <marketId> <max>   # schedule a last-minute bid
python -m fantasybot bid-run                      # run the bid plan (fired by the cron)
python -m fantasybot watch [--run|--hermes]       # live monitoring UI
python -m fantasybot tick [--dry-run] [--force]   # one serverless tick, locally
python -m fantasybot queue                        # what the cloud will do, and when
```

## Live monitoring (Mission Control)

To watch **in real time** what the agent reads, decides and executes —ideal for
supervising a run or recording a demo— there's a small web UI:

```bash
python -m fantasybot watch          # open the UI and supervise (you'll see the next cycle)
python -m fantasybot watch --run    # open the UI and trigger the deterministic agent
python -m fantasybot watch --hermes # open the UI and trigger the Hermes brain (LLM)
```

Every meaningful action (review, lineup, bid, sale, buyout) writes a line to
`.state/events.jsonl`; the UI follows it over SSE and renders it as a timeline.
It's fantasybot's **native** trace: it reflects what the CLI actually did, with or
without Hermes on top. It binds to `127.0.0.1`; on a VPS, open it through a tunnel:
`ssh -L 9137:127.0.0.1:9137 <server>`.

**Last-minute bidding:** instead of bidding early (and revealing your bid), the
agent schedules its flips into a "plan" (`bid-plan`) and a cron bids right at the
close: if there's no competition, the value plus a touch; if there is, up to your
max. Deterministic and free of tokens.

The decision commands (`agent`, `flip`, `needs`, `optimize`) accept `--json` for
programmatic consumption (that's how the autonomous agent reads them).

## Autonomous agent (Hermes)

To have it run on its own on a VPS —reviewing, deciding, acting and scheduling its
own reminders— fantasybot is deployed on top of
[Hermes Agent](https://hermes-agent.nousresearch.com): an agent runtime that
provides the **brain** (an LLM — Claude — plus persistent memory, native cron and
code execution), while `fantasybot` is its **toolbox**, called via the CLI.

What Hermes adds on top of the deterministic agent:

- **It keeps its own memory.** In `hermes/MEMORY.md` the agent maintains, between
  runs, the week's plan, the decisions it made and *why*, and the outcomes it
  learns from (did the flip I bought go up? did I read the starter right?). It's
  its working notebook, not a static file.
- **It schedules itself.** Beyond a daily review, it registers its own cron jobs
  and reminders for the key moments the report already computes: market close,
  clause windows opening, and the lineup deadline before the first match.
- **It acts with judgment.** Routine moves (lineups, bids) run without asking;
  the bigger, irreversible ones (buyout clauses, large sales) are done with care
  and recorded. How autonomous it is on those is configured in `hermes/USER.md`.

The agent's assets live in `hermes/`: `SOUL.md` (persona), `USER.md` (your
preferences), `MEMORY.md` (its working memory) and `skills/fantasy-manager/SKILL.md`
(the playbook). The full setup guide is in [`deploy/README.md`](deploy/README.md).

## The agent (a human-like review)

```bash
python -m fantasybot agent [--days N]  # review + PLAN of actions (touches nothing)
python -m fantasybot agent --execute   # ACTS: sets the lineup and bids for real
python -m fantasybot tasks             # the week's pending tasks
python -m fantasybot tasks --done 3    # mark task #3 as done
python -m fantasybot due               # fire due reminders (for the cron)
```

### Autonomy

With `--execute`, the agent does ON ITS OWN (without asking): sets the best
**lineup** and places/cancels **bids** on the market for profitable flips (it may
use the whole balance). **Buyout clauses are NOT automatic** (irreversible spend):
they're left as a notice and a task for you to confirm. Without `--execute`, it
just shows the plan.

> This applies to the **deterministic agent** (`fantasybot agent`). When it's
> piloted by **Hermes** (LLM), the autonomy for buyouts and sales is configured in
> `hermes/USER.md` (default: automatic, with judgment).

## Automation (so it connects on its own)

The recommended way is the serverless deployment ([DEPLOY.md](DEPLOY.md)): it
needs no machine of yours at all. What follows is the local-cron alternative.

With Windows Task Scheduler:

- **Daily review:** run `python -m fantasybot agent` at your time (e.g. 09:00). It
  detects changes, decides and schedules the day's reminders.
- **Autonomous acting:** use `python -m fantasybot agent --execute` in the daily
  task so it sets the lineup and bids on its own.
- **Firing reminders:** run `python -m fantasybot due` every minute. When a
  reminder is due (a clause opening, market close), it prints the notice.

For an always-on deployment **without** a VPS, see [DEPLOY.md](DEPLOY.md). For an
always-on Linux/VPS deployment with Hermes as the brain, see above.

## Structure

Layers in a single direction (adding a source or a strategy = one new file):

```
fantasybot/
  config.py            constants: endpoints, OAuth, paths
  auth.py              OAuth login (PKCE) + token refresh
  api.py               FantasyClient: read and write against the API
  net.py / cache.py    HTTP with backoff (429) + on-disk cache of scrapes
  matching.py          name normalization and cross-source matching
  sources/             external data: market_trends, lineups, matchday (futbolfantasy)
  strategy/            decisions: flip, lineup (optimizer), needs, sell
  state.py             snapshot + tasks + reminders + bid plan (.state/)
  events.py            native action trace (for the monitoring UI)
  agent.py             the "brain": review() = a human-like cycle
  execute.py           execution layer: sets the lineup and bids for real
  bidding.py           last-minute bidding (deterministic, testable)
  monitor.py + web/    live monitoring UI (SSE, local only)
  cli.py               command-line interface

  storage/             WHERE state lives — the seam that makes serverless work
    base.py              the contract (documents, cache, events, queue, locks)
    local.py             JSON files under .state/ (the CLI default)
    supabase.py          PostgreSQL over PostgREST, stdlib urllib only
  scheduler.py         actions with a deadline + exactly-once guarantees
  tick.py              one serverless wake-up: run what is due, then die
  llm/                 the optional brain (client.py, strategy.py)
  serverless/http.py   auth and JSON plumbing for the Vercel functions

api/                   Vercel functions: /api/tick, /api/status, /api/admin, /api
public/index.html      the monitoring dashboard (polling, no SSE)
supabase/migrations/   reproducible schema
scripts/               setup-db, migrate-state, tick
hermes/                autonomous-agent assets (SOUL, USER, MEMORY, skill)
deploy/                installer and VPS deployment guide
```

### Why the storage seam

The bot's logic never learns where its state is. `state.py`, `events.py`,
`cache.py` and `auth.py` all ask `fantasybot.storage` for a backend, and every
strategy above them is untouched. On a laptop that backend writes the same JSON
files it always did; on Vercel it writes PostgreSQL, because a function that dies
after 45 seconds has no disk worth writing to. One env var switches it, and with
none set it auto-detects.

That is also why the CLI did not have to be rewritten: `python -m fantasybot
agent` on your machine and `/api/tick` in the cloud run the *same* `agent.review()`.

## Disclaimer

**Unofficial API:** LaLiga may change it without notice, and automating the game
may go against its terms of use. This is a personal, non-commercial project with no
intention whatsoever to act against LaLiga or to harm the game in any way. The
software is provided "as is", without warranty of any kind; you use it at your own
risk and are solely responsible for its use.

## Contributing

Issues and PRs are welcome — as long as they're thoughtful and actually make sense.
Please, no AI slop.
