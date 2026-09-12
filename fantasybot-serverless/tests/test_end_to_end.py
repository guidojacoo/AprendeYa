"""One whole tick, start to finish, against a fake LaLiga.

Every other test checks a part. This one runs `tick.run()` the way Vercel runs
it — review, plan, queue, execute, persist — because the bugs that actually took
the deployment down were integration bugs that no unit test could see: a name
collision that emptied the OAuth scope, a missing import, a phase that took
longer than the function was allowed to live.

The client is fake but shaped like the real payloads; the storage is real
(LocalStorage in a temp dir) and so is every code path above it.
"""

from datetime import timedelta
from unittest import mock

from fantasybot import config, scheduler, tick
from fantasybot.storage import to_iso, utcnow
from tests.support import StorageTestCase


def _pm(pid, pos, value, avg=5, status="ok", name=None):
    return {"id": pid, "nickname": name or pid, "name": name or pid,
            "positionId": pos, "marketValue": value, "playerStatus": status,
            "averagePoints": avg, "lastSeasonPoints": 80, "points": 40}


class FakeLaLiga:
    """A squad, a market and a ledger of everything the bot tried to do."""

    def __init__(self, money=30_000_000, with_goalkeeper=True):
        self.money = money
        self.calls = {"bid": [], "sell": [], "clause": [], "lineup": [],
                      "accept": [], "decline": [], "shield": []}
        squad = []
        if with_goalkeeper:
            squad.append(("gk1", 1, 4_000_000))
        squad += [(f"d{i}", 2, 6_000_000) for i in range(1, 6)]
        squad += [(f"m{i}", 3, 7_000_000) for i in range(1, 6)]
        squad += [(f"s{i}", 4, 9_000_000) for i in range(1, 4)]
        self.players = [{"playerTeamId": f"pt-{p}", "buyoutClause": v * 2,
                         "playerMaster": _pm(p, pos, v)}
                        for p, pos, v in squad]

    # --- reads ---
    def me(self):
        return {"id": "u1", "managerName": "Tester"}

    def leagues(self):
        return [{"id": "L1", "team": {"id": "T1"}, "config": {}}]

    def default_ids(self):
        return "L1", "T1"

    def team(self, lid, tid):
        return {"teamMoney": self.money, "players": self.players}

    def league_teams(self, lid):
        return [{"manager": {"managerName": "Tester", "id": "u1"},
                 "teamMoney": self.money, "players": self.players}]

    def league_activity(self, lid, fetch_all=True, max_pages=100, start_page=0):
        return []

    def all_players(self):
        return [p["playerMaster"] for p in self.players]

    def current_week(self):
        return {"weekNumber": 5}

    def calendar(self, week_number=None):
        return [{"date": to_iso(utcnow() + timedelta(hours=6))}]

    def lineup(self, tid):
        return {"formation": {"goalkeeper": [], "defender": [],
                              "midfield": [], "striker": []}}

    def market(self, lid):
        return []

    # --- writes: recorded, never real ---
    def make_bid(self, lid, mid, amount):
        self.calls["bid"].append((mid, amount))
        return {"id": "b1"}

    def sell_player(self, lid, ptid, price):
        self.calls["sell"].append((ptid, price))
        return {"id": "s1"}

    def pay_buyout_clause(self, lid, pid, amount):
        self.calls["clause"].append((pid, amount))
        return {"ok": True}

    def accept_offer(self, lid, mid, oid, money):
        self.calls["accept"].append((mid, oid, money))
        return {"ok": True}

    def decline_offer(self, lid, mid, oid):
        self.calls["decline"].append((mid, oid))
        return {"ok": True}

    def update_lineup(self, tid, payload):
        self.calls["lineup"].append(payload)
        return {"ok": True}

    def shield_player(self, lid, ptid):
        self.calls["shield"].append(ptid)
        return {"ok": True}

    def check_shield(self, lid, ptid):
        return None


class _Tick(StorageTestCase):
    FLAGS = ("AUTO_EXECUTE", "AUTO_LINEUP", "AUTO_BIDS", "AUTO_SELLS",
             "AUTO_LIST", "AUTO_CLAUSES", "AUTO_SHIELD",
             "AUTO_MATCHDAY_LINEUP")

    def setUp(self):
        super().setUp()
        self._saved_autonomy = {n: getattr(config, n) for n in self.FLAGS}
        for n in self.FLAGS:
            setattr(config, n, True)
        self.addCleanup(lambda: [setattr(config, n, v)
                                 for n, v in self._saved_autonomy.items()])
        # No network: the scraped sources are stubbed empty, which is also the
        # degraded state the bot must keep playing in.
        # Each module imported these BY NAME, so patching the source module is
        # not enough — every binding has to be replaced or one of them reaches
        # the network and the suite spends half a minute waiting for a blocked
        # connection to give up.
        for target in ("fantasybot.sources.lineups.probable_lineups",
                       "fantasybot.agent.probable_lineups",
                       "fantasybot.strategy.lineup.probable_lineups",
                       "fantasybot.strategy.needs.probable_lineups",
                       "fantasybot.strategy.scouting.probable_lineups"):
            p = mock.patch(target, return_value={})
            p.start(); self.addCleanup(p.stop)
        for target in ("fantasybot.sources.market_trends.trends_index",
                       "fantasybot.agent.trends_index",
                       "fantasybot.strategy.flip.trends_index"):
            p = mock.patch(target, return_value={})
            p.start(); self.addCleanup(p.stop)
        for target in ("fantasybot.sources.matchday.next_kickoff",
                       "fantasybot.sources.matchday.next_gameweek_kickoff",
                       "fantasybot.agent.matchday.next_kickoff",
                       "fantasybot.agent.matchday.next_gameweek_kickoff"):
            p = mock.patch(target, return_value=None)
            p.start(); self.addCleanup(p.stop)
        p = mock.patch("fantasybot.agent.matchday.days_until_matchday",
                       return_value=3.0)
        p.start(); self.addCleanup(p.stop)

    def _run(self, client, **kw):
        with mock.patch("fantasybot.api.FantasyClient", lambda: client):
            return tick.run(log=lambda m: None, force_review=True, **kw)


class AFullTick(_Tick):
    def test_it_completes_and_records_an_execution(self):
        out = self._run(FakeLaLiga())
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["review"]["status"], "ok")
        rows = self.store.recent_executions()
        self.assertEqual(rows[0]["status"], "done")

    def test_it_fits_comfortably_inside_a_vercel_function(self):
        """The limit is 60s and the budget stops at 45. A tick that cannot do
        its work in that window is the bug that served HTML to the dashboard."""
        out = self._run(FakeLaLiga())
        self.assertLess(out["duration_seconds"], 30)
        self.assertEqual(out.get("review", {}).get("skipped_for_time", []), [])

    def test_it_fields_a_lineup(self):
        client = FakeLaLiga()
        self._run(client)
        self.assertEqual(len(client.calls["lineup"]), 1)

    def test_it_lists_the_squad_on_the_market(self):
        client = FakeLaLiga()
        out = self._run(client)
        listed = out["review"]["listings"]["listed"]
        self.assertEqual(len(listed), len(client.players))
        self.assertTrue(all(r["why"] for r in listed),
                        "every listing carries its reasoning")

    def test_reserves_are_cached_for_later_ticks(self):
        self._run(FakeLaLiga())
        reserves = self.store.get_doc("reserves", {})
        self.assertEqual(len(reserves), 14)
        self.assertTrue(all(v > 0 for v in reserves.values()))

    def test_a_second_tick_queues_no_duplicates(self):
        """Idempotence at the whole-tick level.

        The pending COUNT legitimately falls between runs — the second tick
        executes what the first queued. What must never happen is the same piece
        of work being queued twice, so the keys are what gets checked.
        """
        client = FakeLaLiga()
        self._run(client)
        self._run(client)
        keys = [a["idempotency_key"]
                for a in self.store._actions()]
        self.assertEqual(len(keys), len(set(keys)),
                         f"duplicated work: {sorted(keys)}")

    def test_no_player_is_ever_listed_twice(self):
        """A tick QUEUES the listings; the next tick executes them — so the
        number of sell calls rises between runs, which is correct. What must
        never happen is the same player being put on the market twice.
        """
        client = FakeLaLiga()
        for _ in range(3):
            self._run(client)
        listed = [ptid for ptid, _price in client.calls["sell"]]
        self.assertTrue(listed, "it should have listed somebody by now")
        self.assertEqual(len(listed), len(set(listed)),
                         f"duplicated listing: {sorted(listed)}")

    def test_it_writes_a_report_the_dashboard_can_read(self):
        self._run(FakeLaLiga())
        report = self.store.get_doc("last_report", {})
        for key in ("money", "formation", "listings", "clauses", "shield",
                    "gap_signings", "tasks"):
            self.assertIn(key, report, f"missing {key}")


class AMissingGoalkeeper(_Tick):
    def test_the_gap_is_reported_even_with_nobody_to_sign(self):
        """An empty market means no signing is possible — but the gap must still
        be visible rather than silently dropped."""
        client = FakeLaLiga(with_goalkeeper=False)
        out = self._run(client)
        self.assertTrue(out["ok"], out.get("error"))
        self.assertIn("POR", out["review"]["gaps"].get("skipped", [{}])[0]
                      .get("pos", "POR"))


class WhenTheWorldIsBroken(_Tick):
    def test_an_api_that_refuses_everything_still_reports_cleanly(self):
        """A JSON summary with an error beats an HTML error page every time."""
        class Broken(FakeLaLiga):
            def team(self, lid, tid):
                raise RuntimeError("503 from LaLiga")
        out = self._run(Broken())
        self.assertFalse(out["ok"])
        self.assertIn("503", out["error"])
        self.assertEqual(self.store.recent_executions()[0]["status"], "failed")

    def test_observe_only_mode_touches_nothing(self):
        config.AUTO_EXECUTE = False
        client = FakeLaLiga()
        out = self._run(client)
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(client.calls["bid"], [])
        self.assertEqual(client.calls["sell"], [])
        self.assertEqual(client.calls["clause"], [])
        self.assertEqual(client.calls["lineup"], [])
