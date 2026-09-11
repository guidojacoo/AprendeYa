"""Central configuration: endpoints, OAuth credentials and file paths.

Every shared constant lives here so it isn't repeated across the code. Secrets
(tokens) are NOT here: they are stored in tokens.json (gitignored).
"""

import os

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

# One database can serve several teams/leagues: every row is namespaced by scope.
SCOPE = _env("FANTASYBOT_SCOPE", "default")

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
AUTO_CLAUSES = _flag("FANTASYBOT_AUTO_CLAUSES", False)   # irreversible spend
AUTO_SELLS = _flag("FANTASYBOT_AUTO_SELLS", False)       # irreversible-ish

# --- LLM ---------------------------------------------------------------------
# "none" keeps the bot 100% deterministic (and 100% free). Anything else turns on
# the strategic pass, which runs at most once every LLM_INTERVAL.
LLM_PROVIDER = (_env("LLM_PROVIDER", "none") or "none").strip().lower()
LLM_API_KEY = _env("LLM_API_KEY")
LLM_MODEL = _env("LLM_MODEL")
LLM_BASE_URL = _env("LLM_BASE_URL")
LLM_TIMEOUT = _int("LLM_TIMEOUT", 25)
LLM_MAX_TOKENS = _int("LLM_MAX_TOKENS", 1500)

# Bootstrap credential: lets a fresh deployment seed tokens into the database
# without ever running the interactive login on the server.
FANTASY_REFRESH_TOKEN = _env("FANTASY_REFRESH_TOKEN")
