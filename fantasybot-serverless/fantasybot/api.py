"""LALIGA Fantasy API client.

Manages the token (auto-refreshes it before expiry and retries on 401) and
exposes read and write methods. The rest of the project builds on this.
"""

import json
import os
import time
import urllib.error
import urllib.request

from . import auth, config


class FantasyError(Exception):
    pass


class FantasyClient:
    def __init__(self):
        self.tokens = auth.load_tokens()

    # --- token ---
    def _bearer(self) -> str:
        return auth.bearer_token(self.tokens)

    def _is_expiring(self) -> bool:
        exp = auth.jwt_exp(self._bearer())
        if exp is None:
            return False  # if we don't know, the retry on 401 covers us
        return time.time() > (exp - config.TOKEN_EXPIRY_MARGIN)

    def refresh(self):
        self.tokens = auth.refresh(self.tokens)

    # --- requests ---
    def _request(self, method: str, path: str, body=None):
        if self._is_expiring():
            self.refresh()
        return self._do(method, path, body, retry_on_401=True)

    def _do(self, method, path, body, retry_on_401):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self._bearer()}",
            "Accept": "application/json",
            "x-lang": "es",
            "User-Agent": config.USER_AGENT,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        # An absolute URL passes straight through: the stats service lives on a
        # different base than /api, and gluing it on with "/.." would depend on
        # URL normalization we'd rather not bet on.
        url = path if path.startswith("http") else config.API_BASE + path
        req = urllib.request.Request(url, data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry_on_401:
                self.refresh()
                return self._do(method, path, body, retry_on_401=False)
            detail = e.read().decode("utf-8", "replace")[:400]
            raise FantasyError(f"{method} {path} -> {e.code}: {detail}") from None

    def get(self, path):
        return self._request("GET", path)

    def post(self, path, body=None):
        return self._request("POST", path, body)

    def put(self, path, body=None):
        return self._request("PUT", path, body)

    def delete(self, path):
        return self._request("DELETE", path)

    # --- path helpers ---
    def _cmp(self, tail):
        return f"/v1/competition/{config.COMPETITION_ID}{tail}"

    # --- reads ---
    def me(self):
        return self.get("/v4/user/me?x-lang=es")

    def leagues(self):
        return self.get(self._cmp("/leagues?x-lang=es"))

    def default_ids(self):
        """(league_id, team_id) of the league to operate on.

        Defaults to the user's first league, so single-league setups (the OSS self-host
        case) work with no config. Set FANTASYBOT_LEAGUE=<id> to pin a specific league —
        that's how the hosted service drives an account that has several leagues: it runs
        the agent once per league, exporting this var each time. Every command resolves
        ids through here, so one env var steers them all without touching call sites.
        """
        leagues = self.leagues()
        if not leagues:
            raise FantasyError("The user has no leagues.")
        want = os.environ.get("FANTASYBOT_LEAGUE")
        if want:
            for lg in leagues:
                if str(lg["id"]) == str(want):
                    return lg["id"], str(lg["team"]["id"])
            raise FantasyError(f"League {want} is not in this account.")
        lg = leagues[0]
        return lg["id"], str(lg["team"]["id"])

    def team(self, league_id, team_id):
        return self.get(self._cmp(f"/leagues/{league_id}/teams/{team_id}?x-lang=es"))

    def lineup(self, team_id):
        return self.get(self._cmp(f"/teams/{team_id}/lineup?x-lang=es"))

    def market(self, league_id):
        return self.get(self._cmp(f"/league/{league_id}/market?x-lang=es"))

    def league_teams(self, league_id):
        return self.get(self._cmp(f"/leagues/{league_id}/teams?x-lang=es"))

    def league_activity(self, league_id, fetch_all=True, max_pages=100,
                        start_page=0):
        """League activity, paginated.

        `start_page` lets a caller resume a backfill it could not finish. A full
        history is ~100 requests against an unofficial API, which no serverless
        function has time for in one go — so the review walks it a few pages at a
        time across several runs instead of trying and being killed.
        """
        if not fetch_all:
            res = self.get(self._cmp(f"/leagues/{league_id}/activity/0?x-lang=es"))
            return res if isinstance(res, list) else []
        all_acts = []
        for idx in range(start_page, start_page + max_pages):
            try:
                r = self.get(self._cmp(f"/leagues/{league_id}/activity/{idx}?x-lang=es"))
                if not r or not isinstance(r, list):
                    break
                all_acts.extend(r)
                if len(r) == 0:
                    break
            except (FantasyError, OSError, json.JSONDecodeError):
                if idx == 0:
                    raise
                break
        return all_acts

    def all_players(self):
        """Fetches master list of all players in the competition with past season points and valuations."""
        return self.get(self._cmp("/players?x-lang=es"))

    # --- reads: gameweek & calendar ---
    # agent.captain_fixture_difficulty() has always called current_week()/calendar()
    # behind a bare `except Exception`, so before these existed the premium captain
    # picker silently fell back to "no fixture awareness" on every single run.
    def current_week(self):
        """The gameweek in progress: {weekNumber, ...}."""
        return self.get(self._cmp("/week/current?x-lang=es"))

    def calendar(self, week_number=None):
        """A gameweek's fixtures. Defaults to the gameweek in progress."""
        if week_number is None:
            week_number = (self.current_week() or {}).get("weekNumber")
        if week_number is None:
            return []
        return self.get(self._cmp(f"/calendar?weekNumber={week_number}&x-lang=es"))

    def week_stats(self, week_number):
        """Per-gameweek player stats. Lives outside /competition, under /stats."""
        return self._request(
            "GET", f"{config.STATS_BASE}/v1/competition/{config.COMPETITION_ID}"
                   f"/stats/week/{week_number}")

    # --- reads: league & team detail ---
    def standing(self, league_id, week=None):
        """League table — overall, or for one gameweek."""
        tail = f"/leagues/{league_id}/standing"
        if week is not None:
            tail += f"/{week}"
        return self.get(self._cmp(f"{tail}?x-lang=es"))

    def team_money(self, team_id):
        """{teamMoney, teamInvestment} — the cheap way to read the balance when
        the full squad payload is not needed."""
        return self.get(self._cmp(f"/teams/{team_id}/money?x-lang=es"))

    def lineup_week(self, team_id, week_number):
        """The lineup fielded in a given gameweek."""
        return self.get(self._cmp(
            f"/teams/{team_id}/lineup/week/{week_number}?x-lang=es"))

    # --- reads: competition reference data (outside /competition) ---
    def teams_master(self):
        """The 20 LaLiga clubs: id, name, shortName, slug."""
        return self.get("/v3/teams-master?x-lang=es")

    def activity_types(self):
        """The activity-type dictionary league activity rows refer to by id."""
        return self.get("/v5/activity-types?x-lang=es")

    # --- writes: market ---
    def make_bid(self, league_id, market_id, money):
        return self.post(self._cmp(
            f"/league/{league_id}/market/{market_id}/bid?x-lang=es"), {"money": money})

    def modify_bid(self, league_id, market_id, bid_id, money):
        return self.put(self._cmp(
            f"/league/{league_id}/market/{market_id}/bid/{bid_id}?x-lang=es"),
            {"money": money})

    def cancel_bid(self, league_id, market_id, bid_id):
        return self.delete(self._cmp(
            f"/league/{league_id}/market/{market_id}/bid/{bid_id}/cancel?x-lang=es"))

    def sell_player(self, league_id, player_id, sale_price):
        return self.post(self._cmp(f"/league/{league_id}/market/sell?x-lang=es"),
                         {"playerId": player_id, "salePrice": sale_price})

    def accept_offer(self, league_id, market_id, offer_id, money):
        return self.post(self._cmp(
            f"/league/{league_id}/market/{market_id}/offer/{offer_id}/accept?x-lang=es"),
            {"offerMoney": money})

    def decline_offer(self, league_id, market_id, offer_id):
        return self.post(self._cmp(
            f"/league/{league_id}/market/{market_id}/offer/{offer_id}/reject?x-lang=es"))

    # --- writes: buyout clauses ---
    def pay_buyout_clause(self, league_id, player_id, amount):
        """Buyout: pays the release clause of another manager's player."""
        return self.post(self._cmp(
            f"/league/{league_id}/buyout/{player_id}/pay?x-lang=es"),
            {"buyoutClauseToPay": amount})

    def increase_buyout_clause(self, league_id, player_id, amount):
        """Raises the clause of one of your players to protect them."""
        return self.post(self._cmp(
            f"/league/{league_id}/buyout/{player_id}/increase?x-lang=es"),
            {"buyoutClause": amount})

    # --- shield (blindaje): protect a player from a rival's buyout clause ---
    def check_shield(self, league_id, player_team_id):
        """Whether one of your players is shielded (blindado). Returns null when he is NOT
        shielded, else the shield info. Keyed on the playerTeamId (your roster-slot id)."""
        return self.get(self._cmp(
            f"/league/{league_id}/player-team/{player_team_id}/check-shield?x-lang=es"))

    def shield_player(self, league_id, player_team_id):
        """Shield (blindar) one of your players so a rival can't buy him via his buyout
        clause. FREE — done through a rewarded-ad flow.

        NOTE: like sell_player, LaLiga keys this on the playerTeamId (your roster-slot id),
        NOT the playerMaster id — and the request FIELD is still named `playerId` while its
        VALUE is the playerTeamId (field-name-vs-value mismatch).

        CONFIRMED LIVE. This was marked "to be confirmed before deploy" on the open
        question of whether the PUT is accepted without a rewarded ad actually
        having been watched. It is: the league activity feed recorded two shields
        placed by this flow, at 02:02 and 06:14, hours apart and at times nobody
        is shielding players by hand.
        """
        return self.put(self._cmp(f"/league/{league_id}/shield/player?x-lang=es"),
                        {"playerId": player_team_id, "rewardedAdType": "Blindaje",
                         "rewardedAd": 1})

    # --- writes: lineup ---
    def update_lineup(self, team_id, lineup_data):
        return self.put(self._cmp(f"/teams/{team_id}/lineup?x-lang=es"), lineup_data)
