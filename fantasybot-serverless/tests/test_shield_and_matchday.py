"""Defence, and the free points an hourly cadence misses.

Shielding is the mirror of clause-sniping: it stops a rich rival taking OUR best
player the same way we take theirs. It is free, so the only question is whether
we remember to do it.

The per-match lineup refresh matters because a player locks when HIS match
starts, not when the gameweek does. A Sunday striker who picks up a knock on
Saturday can still be swapped — an hourly review catches that only by luck.
"""

from datetime import timedelta

from fantasybot import config, scheduler, tick
from fantasybot.scheduler import TickContext
from fantasybot.storage import to_iso, utcnow
from tests.support import StorageTestCase


class ShieldClient:
    def __init__(self, shielded=None, fixtures=None):
        self.shielded, self.fixtures = shielded, fixtures or []
        self.calls = []

    def default_ids(self):
        return "L1", "T1"

    def check_shield(self, lid, ptid):
        return self.shielded

    def shield_player(self, lid, ptid):
        self.calls.append(ptid)
        return {"ok": True}

    def calendar(self, week_number=None):
        return self.fixtures


class _Flags(StorageTestCase):
    NAMES = ("AUTO_SHIELD", "AUTO_EXECUTE", "AUTO_LINEUP",
             "AUTO_MATCHDAY_LINEUP")

    def setUp(self):
        super().setUp()
        self._flags = {n: getattr(config, n) for n in self.NAMES}
        for n in self.NAMES:
            setattr(config, n, True)
        self.addCleanup(lambda: [setattr(config, n, v)
                                 for n, v in self._flags.items()])


class Shielding(_Flags):
    CAND = {"player_team_id": "pt-7", "player_id": "p7", "nombre": "Joya",
            "value": 20_000_000, "clause": 25_000_000,
            "reason": "clause within reach"}

    def _run(self, client):
        scheduler.schedule(scheduler.SHIELD, {"league_id": "L1", **self.CAND},
                           execute_at=utcnow() - timedelta(seconds=1),
                           idempotency_key="shield:L1:pt-7:today")
        ctx = TickContext(client=client, budget_seconds=20, log=lambda m: None)
        return scheduler.run_due(ctx)[0]["result"]

    def test_it_shields_an_exposed_player(self):
        c = ShieldClient(shielded=None)
        self.assertEqual(self._run(c)["status"], "shielded")
        self.assertEqual(c.calls, ["pt-7"])

    def test_it_does_not_waste_a_shield_on_someone_already_protected(self):
        c = ShieldClient(shielded={"until": "later"})
        self.assertEqual(self._run(c)["status"], "already_shielded")
        self.assertEqual(c.calls, [])

    def test_autonomy_off_stops_it(self):
        config.AUTO_SHIELD = False
        c = ShieldClient()
        self.assertEqual(self._run(c)["status"], "skipped")
        self.assertEqual(c.calls, [])

    def test_planning_queues_the_reports_candidate(self):
        ctx = TickContext(budget_seconds=20, log=lambda m: None)
        res = tick._plan_shield(ctx, "L1", {"shield": self.CAND})
        self.assertEqual(res["mode"], "on")
        self.assertEqual(self.store.pending_actions()[0]["type"],
                         scheduler.SHIELD)

    def test_nothing_to_shield_is_not_an_error(self):
        ctx = TickContext(budget_seconds=20, log=lambda m: None)
        self.assertIsNone(tick._plan_shield(ctx, "L1", {})["queued"])


class MatchdayLineups(_Flags):
    def _plan(self, fixtures):
        ctx = TickContext(budget_seconds=20, log=lambda m: None)
        return tick._plan_matchday_lineups(ctx, ShieldClient(fixtures=fixtures),
                                           "L1", "T1")

    def test_one_refresh_is_queued_before_each_kickoff(self):
        k1 = utcnow() + timedelta(hours=5)
        k2 = utcnow() + timedelta(hours=30)
        res = self._plan([{"date": to_iso(k1)}, {"matchDate": to_iso(k2)}])
        self.assertEqual(len(res["queued"]), 2)
        queued = self.store.pending_actions()
        self.assertEqual({a["type"] for a in queued}, {scheduler.LINEUP})

    def test_it_lands_shortly_before_the_whistle(self):
        ko = utcnow() + timedelta(hours=5)
        self._plan([{"date": to_iso(ko)}])
        at = self.store.pending_actions()[0]["execute_at"]
        gap = (ko - tick.parse_iso(at)).total_seconds() / 60
        self.assertAlmostEqual(gap, config.LINEUP_LEAD_MINUTES, delta=1)

    def test_kickoffs_already_past_are_ignored(self):
        self.assertEqual(self._plan([{"date": to_iso(utcnow() -
                                                     timedelta(hours=2))}])["queued"], [])

    def test_kickoffs_far_in_the_future_are_left_for_a_later_review(self):
        self.assertEqual(self._plan([{"date": to_iso(utcnow() +
                                                     timedelta(days=20))}])["queued"], [])

    def test_an_unparseable_calendar_does_not_break_the_review(self):
        """The date field has several plausible names across payload versions.
        Dropping what we cannot read beats crashing a review over it."""
        self.assertEqual(self._plan([{"weird": "shape"}, "not a dict", None])["queued"],
                         [])

    def test_a_calendar_that_raises_is_survivable(self):
        class Broken(ShieldClient):
            def calendar(self, week_number=None):
                raise RuntimeError("500 from LaLiga")
        ctx = TickContext(budget_seconds=20, log=lambda m: None)
        self.assertEqual(
            tick._plan_matchday_lineups(ctx, Broken(), "L1", "T1")["queued"], [])

    def test_the_same_kickoff_is_never_queued_twice(self):
        ko = utcnow() + timedelta(hours=5)
        self._plan([{"date": to_iso(ko)}])
        self._plan([{"date": to_iso(ko)}])
        self.assertEqual(len(self.store.pending_actions()), 1)

    def test_it_is_off_when_the_feature_is_off(self):
        config.AUTO_MATCHDAY_LINEUP = False
        self.assertEqual(self._plan([{"date": to_iso(utcnow() +
                                                     timedelta(hours=5))}])["mode"], "off")
