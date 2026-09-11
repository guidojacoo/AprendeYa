"""Integration wiring for value_history — the part test_value_history.py doesn't cover:
`agent.review()` banks a daily snapshot of LaLiga's OFFICIAL market values (best-effort,
never crashes the review), and `strategy.flip` reads that history back as an informational
cross-check (`oficial_trend_pct`) next to the futbolfantasy trend. Isolated from the real
.state dir via a temp VALUE_HISTORY_DIR.
"""

import tempfile
import unittest
from unittest import mock

from fantasybot import agent as agent_mod
from fantasybot import state
from fantasybot.strategy import flip


def _element(player_id, nickname, value, sale_price=None):
    return {
        "id": f"m{player_id}",
        "discr": "marketPlayerLeague",
        "salePrice": sale_price,
        "playerMaster": {"id": player_id, "nickname": nickname, "name": nickname,
                         "marketValue": value, "positionId": 4},
    }


class OfficialTrendCrossCheck(unittest.TestCase):
    """flip.evaluate()'s oficial_trend_pct — purely informational, never feeds margin/via."""

    def setUp(self):
        d = self.enterContext(tempfile.TemporaryDirectory(prefix="fb-vh-"))
        p = mock.patch.object(state, "VALUE_HISTORY_DIR", d)
        p.start()
        self.addCleanup(p.stop)

    def test_none_without_enough_banked_history(self):
        index = {"riser": {"valor": 10_000_000, "tendencia": 1}}
        el = _element("p1", "riser", 10_000_000)
        out = flip.evaluate(el, index, horizon=7, today_iso="2026-08-20")
        self.assertIsNone(out["oficial_trend_pct"])
        # decision fields are untouched by the (missing) official trend
        self.assertEqual(out["via"], "SISTEMA")

    def test_reads_banked_history_once_two_snapshots_exist(self):
        state.save_value_snapshot("2026-08-13", {"p1": {"v": 8_000_000, "s": "ok"}})
        state.save_value_snapshot("2026-08-20", {"p1": {"v": 10_000_000, "s": "ok"}})
        index = {"riser": {"valor": 10_000_000, "tendencia": 1}}
        el = _element("p1", "riser", 10_000_000)
        out = flip.evaluate(el, index, horizon=7, today_iso="2026-08-20")
        self.assertEqual(out["oficial_trend_pct"], 25.0)

    def test_missing_today_iso_is_none_not_a_crash(self):
        index = {"riser": {"valor": 10_000_000, "tendencia": 1}}
        el = _element("p1", "riser", 10_000_000)
        out = flip.evaluate(el, index, horizon=7)  # today_iso omitted
        self.assertIsNone(out["oficial_trend_pct"])

    def test_opportunities_wires_todays_date_through(self):
        class _FC:
            def market(self, lid):
                return [_element("p1", "riser", 10_000_000)]

        state.save_value_snapshot("2026-08-13", {"p1": {"v": 8_000_000, "s": "ok"}})
        state.save_value_snapshot("2026-08-20", {"p1": {"v": 10_000_000, "s": "ok"}})
        with mock.patch.object(flip, "trends_index",
                               lambda: {"riser": {"valor": 10_000_000, "tendencia": 1}}), \
             mock.patch.object(flip, "date") as fake_date:
            fake_date.today.return_value.isoformat.return_value = "2026-08-20"
            ops = flip.opportunities(_FC(), "L")
        self.assertEqual(ops[0]["oficial_trend_pct"], 25.0)


def _squad():
    gk = {"playerTeamId": "ptg", "playerMaster": {
        "id": "pg", "nickname": "GK", "name": "GK", "positionId": 1,
        "marketValue": 5_000_000, "playerStatus": "ok"}}
    return {"teamMoney": 3_000_000, "players": [gk]}


class _FakeClient:
    """Mirrors test_no_gk_resilience._FakeClient, plus a swappable all_players()."""

    def __init__(self, all_players_fn):
        self._all_players_fn = all_players_fn

    def default_ids(self):
        return ("L", "T")

    def team(self, lid, tid):
        return _squad()

    def market(self, lid):
        return []

    def lineup(self, tid):
        return {"formation": {}}

    def all_players(self):
        return self._all_players_fn()


class AgentBanksDailySnapshot(unittest.TestCase):
    def setUp(self):
        state_dir = self.enterContext(tempfile.TemporaryDirectory(prefix="fb-vh-agent-"))
        for name, filename in [
            ("STATE_DIR", None),
            ("SNAPSHOT_PATH", "snapshot.json"),
            ("TASKS_PATH", "tasks.json"),
            ("REMINDERS_PATH", "reminders.json"),
            ("BIDS_PATH", "bids.json"),
            ("BID_PLAN_PATH", "bid_plan.json"),
            ("VALUE_HISTORY_DIR", "value_history"),
        ]:
            value = state_dir if filename is None else f"{state_dir}/{filename}"
            p = mock.patch.object(agent_mod.state, name, value)
            p.start()
            self.addCleanup(p.stop)
        for target, val in [
            ("probable_lineups", lambda *a, **k: {}),
            ("trends_index", lambda *a, **k: {}),
        ]:
            p = mock.patch.object(agent_mod, target, val)
            p.start()
            self.addCleanup(p.stop)
        for mod, name, val in [
            (agent_mod.matchday, "next_kickoff", lambda: None),
            (agent_mod.matchday, "next_gameweek_kickoff", lambda: None),
            (agent_mod.matchday, "days_until_matchday", lambda: None),
            (agent_mod.flip, "opportunities", lambda *a, **k: []),
            (agent_mod.needs_mod, "advise",
             lambda *a, **k: {"gaps": {}, "urgency_multiplier": 1, "suggestions": {}}),
        ]:
            p = mock.patch.object(mod, name, val)
            p.start()
            self.addCleanup(p.stop)

    def test_review_banks_todays_official_values(self):
        client = _FakeClient(lambda: [{"id": "pg", "marketValue": "5000000", "playerStatus": "ok"}])
        agent_mod.review(client)
        from datetime import date
        loaded = state.value_history.load_snapshot(state.VALUE_HISTORY_DIR, date.today().isoformat())
        self.assertEqual(loaded, {"pg": {"v": 5000000, "s": "ok"}})

    def test_review_survives_all_players_failure(self):
        def boom():
            raise RuntimeError("API down")
        client = _FakeClient(boom)
        rep = agent_mod.review(client)  # must not raise
        self.assertIn("lineup", rep)


if __name__ == "__main__":
    unittest.main()
