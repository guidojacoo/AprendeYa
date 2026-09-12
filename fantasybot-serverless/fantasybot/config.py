"""Central configuration: endpoints, OAuth credentials and file paths.

Every shared constant lives here so it isn't repeated across the code. Secrets
(tokens) are NOT here: they are stored in tokens.json (gitignored).
"""

import os

# --- .env ---------------------------------------------------------------------
# Read before anything else, because every constant below is computed from the
# environment at import time.
#
# Shipping a .env.example and then not reading the .env is a trap: the variables
# look set and nothing uses them. It is also the only way this works on Windows,
# where there is no `source .env`.


# Where the loader looked, and what it found. Recorded so an error message can
# name the actual cause ("I looked HERE and there was no file") instead of
# telling you to set variables you are sure you already set.
DOTENV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
DOTENV_FOUND = False
DOTENV_KEYS: list = []


def _load_dotenv():
    """Load `.env` from the project root into os.environ.

    Existing environment variables ALWAYS win. That is the important rule: on
    Vercel the real environment is the truth, and a stray .env that got bundled
    must never be able to override it — nor a leftover local file silently point
    a production run at a development database.
    """
    global DOTENV_FOUND
    path = DOTENV_PATH
    try:
        with open(path, encoding="utf-8-sig") as f:
            lines = f.readlines()
    except OSError:
        return          # no .env is the normal case, not a problem
    DOTENV_FOUND = True
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        # Strip one layer of matching quotes, the way a shell would.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value:
            # `KEY=` in a template means "not filled in yet". Setting it to an
            # empty string makes the variable LOOK configured and pushes the
            # failure somewhere far away from the cause.
            continue
        DOTENV_KEYS.append(key)
        os.environ.setdefault(key, value)


_load_dotenv()

# --- LaLiga Fantasy API (unofficial) ---
API_HOST = "https://fantasy-api.llt-services.com"
API_BASE = f"{API_HOST}/api"
STATS_BASE = f"{API_HOST}/stats"   # per-gameweek stats live outside /api
COMPETITION_ID = "1"  # LaLiga

# --- OAuth2 Azure B2C (LaLiga login) ---
OAUTH_BASE = "https://login.laliga.es/laligadspprob2c.onmicrosoft.com/oauth2/v2.0"
AUTHORIZE_ENDPOINT = f"{OAUTH_BASE}/authorize"
TOKEN_ENDPOINT = f"{OAUTH_BASE}/token"
SIGNIN_POLICY = "B2C_1A_5ULAIP_PARAMETRIZED_SIGNIN"
CLIENT_ID = "af88bcff-1157-40a0-b579-030728aacf0b"
REDIRECT_URI = "authredirect://com.lfp.laligafantasy"
# OAuth scope. `offline_access` is what grants the refresh_token — without it the
# session dies in 24h instead of lasting 90 days, so nothing may shadow this name.
SCOPE = "openid offline_access"

# --- External sources ---
FF_MARKET_URL = "https://www.futbolfantasy.com/analytics/laliga-fantasy/mercado"
FF_LINEUPS_INDEX = "https://www.futbolfantasy.com/laliga/posibles-alineaciones"
FF_TEAM_URL = "https://www.futbolfantasy.com/laliga/equipos/{slug}"

# Regular browser UA (so we don't give the bot away). Just a label; doesn't affect speed.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# --- Data paths (tokens, cache, state) ---
# Defaults to the project root (the folder containing the package): convenient
# when running from the checkout. If you install with pip, set FANTASYBOT_HOME to
# avoid writing inside site-packages, e.g. export FANTASYBOT_HOME=~/.fantasybot
ROOT = os.environ.get("FANTASYBOT_HOME") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.expanduser(ROOT)
TOKENS_PATH = os.path.join(ROOT, "tokens.json")
PKCE_PATH = os.path.join(ROOT, ".pkce.json")

# Margin for refreshing the token before it expires (seconds).
TOKEN_EXPIRY_MARGIN = 120


# =============================================================================
# Serverless / hosted configuration
# =============================================================================
# Everything below is additive: with no env vars set, fantasybot behaves exactly
# as it always has (JSON files under .state/, CLI-first, zero dependencies).


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _flag(name, default=False):
    v = _env(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name, default):
    try:
        return int(_env(name, default))
    except (TypeError, ValueError):
        return default


# --- storage backend ---------------------------------------------------------
SUPABASE_URL = (_env("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = _env("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_ANON_KEY = _env("SUPABASE_ANON_KEY")

# "local" (JSON files, the original behaviour) or "supabase" (PostgreSQL).
# Auto-detects: if Supabase credentials are present we use them, else local.
STORAGE_BACKEND = _env(
    "FANTASYBOT_STORAGE",
    "supabase" if (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY) else "local")

# One database can serve several teams/leagues: every row is namespaced.
#
# NOT named SCOPE: that name is already taken above by the OAuth scope, and
# defining it twice in one module silently replaced "openid offline_access" with
# "default" — LaLiga then issued no refresh_token and the login URL 404'd. A
# regression test pins both names now.
STORAGE_SCOPE = _env("FANTASYBOT_SCOPE", "default")

# --- serverless tick ---------------------------------------------------------
BOT_CRON_SECRET = _env("BOT_CRON_SECRET")
# Vercel's own Cron sends `Authorization: Bearer $CRON_SECRET`, so accepting it
# too lets Vercel Cron act as a free daily backstop without a second endpoint.
VERCEL_CRON_SECRET = _env("CRON_SECRET")
VERCEL_APP_URL = (_env("VERCEL_APP_URL") or "").rstrip("/")

# A Vercel Hobby function is killed at 60s. We stop well before that so the tick
# always gets to write its state and close its execution row.
TICK_BUDGET_SECONDS = _int("TICK_BUDGET_SECONDS", 45)
# Sniper ticks are allowed to hold longer (they are the ones racing a close).
SNIPER_BUDGET_SECONDS = _int("SNIPER_BUDGET_SECONDS", 50)
# How long a tick holds the global mutex before it is considered dead.
TICK_LOCK_SECONDS = _int("TICK_LOCK_SECONDS", 90)

# Cadences (seconds). The market scan is deterministic and cheap; the LLM pass is
# neither, so it runs far less often.
REVIEW_INTERVAL = _int("FANTASYBOT_REVIEW_INTERVAL", 3600)        # 1h
LLM_INTERVAL = _int("FANTASYBOT_LLM_INTERVAL", 86400)             # 1 day
# How early (seconds before close) a bid becomes a "sniper" job the scheduler
# must hold the line for.
BID_LEAD_SECONDS = _int("FANTASYBOT_BID_LEAD_SECONDS", 60)

# --- autonomy ----------------------------------------------------------------
# Mirrors the CLI's documented autonomy: lineup + bids yes, buyouts no.
AUTO_EXECUTE = _flag("FANTASYBOT_AUTO_EXECUTE", True)
AUTO_LINEUP = _flag("FANTASYBOT_AUTO_LINEUP", True)
AUTO_BIDS = _flag("FANTASYBOT_AUTO_BIDS", True)
# Pay a rival's buyout clause the moment it unlocks. This is the single biggest
# source of value in the game and the only genuinely irreversible spend the bot
# makes, so it is off until you turn it on — and it is fenced by CASH_RESERVE and
# MAX_CLAUSE below.
AUTO_CLAUSES = _flag("FANTASYBOT_AUTO_CLAUSES", False)
# Shield our own most clause-vulnerable player. Free (a rewarded-ad flow) and
# purely defensive. Off by default only because LaLiga's shield PUT format is
# noted in api.py as not yet confirmed against a live account.
AUTO_SHIELD = _flag("FANTASYBOT_AUTO_SHIELD", False)
# Re-optimise the XI before each kickoff. Players lock when THEIR match starts,
# not when the gameweek does, so a Sunday striker can still be swapped on
# Saturday night — this is free points that an hourly cadence alone misses.
AUTO_MATCHDAY_LINEUP = _flag("FANTASYBOT_AUTO_MATCHDAY_LINEUP", True)
# Keep the whole squad standing on the market. Listing is not selling — it is an
# ask — so this is safe on its own: nothing leaves without AUTO_SELLS.
AUTO_LIST = _flag("FANTASYBOT_AUTO_LIST", True)
# Accept offers that meet a player's reserve price. This is the one that parts
# with players, so it is off unless you turn it on.
AUTO_SELLS = _flag("FANTASYBOT_AUTO_SELLS", False)
# Decline offers below the reserve instead of letting them sit until the listing
# expires. Keeps the decision ours rather than the platform's.
DECLINE_LOWBALLS = _flag("FANTASYBOT_DECLINE_LOWBALLS", True)

# --- LLM ---------------------------------------------------------------------
# "none" keeps the bot 100% deterministic (and 100% free). Anything else turns on
# the strategic pass, which runs at most once every LLM_INTERVAL.
LLM_PROVIDER = (_env("LLM_PROVIDER", "none") or "none").strip().lower()
LLM_API_KEY = _env("LLM_API_KEY")
LLM_MODEL = _env("LLM_MODEL")
LLM_BASE_URL = _env("LLM_BASE_URL")
LLM_TIMEOUT = _int("LLM_TIMEOUT", 25)
LLM_MAX_TOKENS = _int("LLM_MAX_TOKENS", 1500)

# --- spending limits ---------------------------------------------------------
# Cash the bot must always leave in the bank. A clause that would take the
# balance below this is refused: being unable to answer the next opportunity is
# itself a cost, and an empty account cannot bid at a market close.
CASH_RESERVE = _int("FANTASYBOT_CASH_RESERVE", 0)
# Hard ceiling on a single buyout clause. 0 means no ceiling beyond the balance.
MAX_CLAUSE = _int("FANTASYBOT_MAX_CLAUSE", 0)
# How early (minutes) before a kickoff to re-optimise the lineup.
LINEUP_LEAD_MINUTES = _int("FANTASYBOT_LINEUP_LEAD_MINUTES", 25)

# --- notifications -----------------------------------------------------------
# A bot you cannot see is a bot you cannot trust, and every way this one dies is
# silent. Telegram is free, needs no server and reaches a phone in a second:
# message @BotFather to create a bot, then @userinfobot to get your chat id.
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")
# Any incoming webhook (Discord, Slack) as an alternative or an addition.
NOTIFY_WEBHOOK_URL = _env("NOTIFY_WEBHOOK_URL")
# Warn this many days before the 90-day LaLiga refresh token expires.
TOKEN_WARN_DAYS = _int("FANTASYBOT_TOKEN_WARN_DAYS", 7)

# Bootstrap credential: lets a fresh deployment seed tokens into the database
# without ever running the interactive login on the server.
FANTASY_REFRESH_TOKEN = _env("FANTASY_REFRESH_TOKEN")
