"""OAuth2 authentication (Authorization Code + PKCE) against LaLiga's Azure B2C.

Works for Google/Apple accounts (with no password of their own). Flow:
  1) build_authorize_url() -> the user logs in through the browser.
  2) exchange_code() -> swaps the redirect's 'code' for tokens.
  3) refresh() -> renews the access/id token using the refresh_token (lasts 90 days).

Tokens are stored through the storage backend: `tokens.json` locally, a row in
Supabase when deployed. That difference is the whole reason the bot can run on
Vercel at all — the access token lasts 24h and rotates, so a function that could
only write to its own ephemeral disk would lose the refreshed token every single
time and log itself out within a day.
"""

import base64
import hashlib
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request

from . import config
from .storage import get_storage


class AuthError(Exception):
    pass


# --- PKCE / JWT utilities ---
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def make_pkce():
    """Returns (code_verifier, code_challenge) using the S256 method."""
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def jwt_exp(token: str):
    """The 'exp' timestamp of a JWT without verifying its signature (or None)."""
    try:
        return json.loads(_b64url_decode(token.split(".")[1])).get("exp")
    except Exception:
        return None


# --- token persistence ---
def load_tokens() -> dict:
    """The stored token set.

    Falls back to FANTASY_REFRESH_TOKEN when the store is empty: that is how a
    brand-new deployment bootstraps itself without anyone running the browser
    login flow on a server. The first refresh writes a full token set back, and
    the env var is never read again.
    """
    store = get_storage()
    if store.kind == "local":
        if os.path.exists(config.TOKENS_PATH):
            with open(config.TOKENS_PATH, encoding="utf-8") as f:
                return json.load(f)
        tokens = None
    else:
        tokens = store.get_doc("tokens", None)

    if tokens:
        return tokens
    if config.FANTASY_REFRESH_TOKEN:
        return {"refresh_token": config.FANTASY_REFRESH_TOKEN}
    raise AuthError(
        "No LaLiga tokens stored. Log in locally (`python -m fantasybot login`) "
        "and push them with `python scripts/migrate-state.py`, or set "
        "FANTASY_REFRESH_TOKEN.")


def save_tokens(tokens: dict):
    store = get_storage()
    if store.kind == "local":
        with open(config.TOKENS_PATH, "w", encoding="utf-8") as f:
            json.dump(tokens, f, indent=2)
        try:
            os.chmod(config.TOKENS_PATH, 0o600)
        except OSError:
            pass
        return
    store.put_doc("tokens", tokens)


def bearer_token(tokens: dict) -> str:
    token = tokens.get("access_token") or tokens.get("id_token")
    if not token:
        raise AuthError("tokens.json has neither access_token nor id_token.")
    return token


# --- interactive flow ---
def build_authorize_url(code_challenge: str, state: str) -> str:
    params = {
        "p": config.SIGNIN_POLICY,
        "client_id": config.CLIENT_ID,
        "response_type": "code",
        "redirect_uri": config.REDIRECT_URI,
        "scope": config.SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": state,
    }
    return f"{config.AUTHORIZE_ENDPOINT}?{urllib.parse.urlencode(params)}"


def extract_code(pasted: str) -> str:
    """Accepts either the full redirect URL or the bare code."""
    pasted = pasted.strip().strip('"').strip("'")
    if "code=" in pasted:
        query = pasted.split("?", 1)[1] if "?" in pasted else pasted
        params = urllib.parse.parse_qs(query)
        if "code" in params:
            return params["code"][0]
    return pasted


def _post_token(body: dict) -> dict:
    data = urllib.parse.urlencode(body).encode("ascii")
    url = f"{config.TOKEN_ENDPOINT}?p={config.SIGNIN_POLICY}"
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST")
    try:
        # Without a timeout a stalled token refresh hangs the caller forever; a
        # cron-driven bid run wedged this way once and never placed its bids.
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        # invalid_grant / AADB2C90090 ("not a valid 5 segment token"): the grant we sent
        # was rejected. Give a message that fits WHICH grant it was.
        if e.code == 400 and ("invalid_grant" in detail or "AADB2C90090" in detail):
            if body.get("grant_type") == "authorization_code":
                raise AuthError(
                    "The login code was rejected. It's single-use and expires within a "
                    "couple of minutes — run `fantasybot login` again and paste the "
                    "redirect URL right away. Don't reuse an old code, and don't re-run "
                    "step 1 in between (that regenerates the PKCE verifier). If step 1 "
                    "hung or took a while at Google's account-chooser screen before you "
                    "got here, that's usually accumulated Google cookies, not this tool — "
                    "retry in an Incognito/Private window.") from None
            raise AuthError("Your LaLiga session has expired or is no longer valid. "
                            "Run `fantasybot login` again to reconnect your account.") from None
        raise AuthError(f"{e.code}: {detail}") from None


def exchange_code(code: str, code_verifier: str) -> dict:
    tokens = _post_token({
        "grant_type": "authorization_code",
        "client_id": config.CLIENT_ID,
        "code": code,
        "redirect_uri": config.REDIRECT_URI,
        "code_verifier": code_verifier,
        "scope": config.SCOPE,
    })
    save_tokens(tokens)
    return tokens


def refresh(tokens: dict) -> dict:
    """Renews using the refresh_token. Saves and returns the new tokens."""
    rt = tokens.get("refresh_token")
    if not rt:
        raise AuthError("No refresh_token. Log in again.")
    new_tokens = _post_token({
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": config.CLIENT_ID,
        "scope": config.SCOPE,
    })
    new_tokens.setdefault("refresh_token", rt)  # B2C rotates the token; if none comes back, reuse it
    save_tokens(new_tokens)
    return new_tokens


# --- two-step PKCE (for non-interactive terminal login) ---
def start_login() -> str:
    """Generates the login URL and saves the verifier to disk. Returns the URL."""
    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(16)
    with open(config.PKCE_PATH, "w", encoding="utf-8") as f:
        json.dump({"code_verifier": verifier, "state": state}, f)
    return build_authorize_url(challenge, state)


def finish_login(pasted_redirect: str) -> dict:
    """Reads the saved verifier and swaps the code from the redirect URL."""
    if not os.path.exists(config.PKCE_PATH):
        raise AuthError(".pkce.json is missing. Run the login step first.")
    with open(config.PKCE_PATH, encoding="utf-8") as f:
        pkce = json.load(f)
    code = extract_code(pasted_redirect)
    if not code:
        raise AuthError("Could not extract the 'code'.")
    tokens = exchange_code(code, pkce["code_verifier"])
    os.remove(config.PKCE_PATH)
    return tokens
