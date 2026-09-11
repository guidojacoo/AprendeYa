"""Unit tests for rival budget calculation, clause tracking, and activity accounting.
No network calls — mock client and data.
"""

import os
import shutil
import tempfile
import unittest

from fantasybot import state as state_mod
from fantasybot.strategy.rivals import (
    parse_activity,
    analyze_squad_clauses,
    autocalibrate_initial_cash,
    analyze_rivals,
    TYPE_MARKET_BUY,
    TYPE_MARKET_SELL,
    TYPE_DIRECT_TRANSFER,
    TYPE_MATCHDAY_REWARD,
)
from fantasybot.state import diff_rival_clauses


class TestRivalAccounting(unittest.TestCase):
    # analyze_rivals persists to .state via record_activity; redirect those paths to a
    # throwaway temp dir so the suite never reads or writes the real checkout state
    # (otherwise the tests are order-dependent and can pollute production state).
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="fbtest-rivals-")
        self._saved = {k: getattr(state_mod, k) for k in (
            "ACTIVITY_HISTORY_PATH", "PLAYERS_CACHE_PATH",
            "SQUAD_HISTORY_PATH", "RIVALS_SNAPSHOT_PATH")}
        state_mod.ACTIVITY_HISTORY_PATH = os.path.join(self._tmp, "activity_history.json")
        state_mod.PLAYERS_CACHE_PATH = os.path.join(self._tmp, "players_cache.json")
        state_mod.SQUAD_HISTORY_PATH = os.path.join(self._tmp, "squad_history.json")
        state_mod.RIVALS_SNAPSHOT_PATH = os.path.join(self._tmp, "rivals_snapshot.json")

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(state_mod, k, v)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_parse_activity_accounting(self):
        activity = [
            # User 100 buys player from market for 10M
            {"activityTypeId": TYPE_MARKET_BUY, "user1Id": 100, "amount": 10_000_000},
            # User 100 sells player to market for 15M
            {"activityTypeId": TYPE_MARKET_SELL, "user1Id": 100, "amount": 15_000_000},
            # User 100 receives matchday reward of 2M
            {"activityTypeId": TYPE_MATCHDAY_REWARD, "user1Id": 100, "amount": 2_000_000},
            # User 200 buys player from User 100 for 8M
            {"activityTypeId": TYPE_DIRECT_TRANSFER, "user1Id": 200, "user2Id": 100, "amount": 8_000_000},
        ]
        flow = parse_activity(activity)

        u100 = flow[100]
        self.assertEqual(u100["purchases"], 10_000_000)
        self.assertEqual(u100["sales"], 15_000_000 + 8_000_000)  # 23M
        self.assertEqual(u100["prizes"], 2_000_000)
        self.assertEqual(u100["transactions_count"], 4)

        u200 = flow[200]
        self.assertEqual(u200["purchases"], 8_000_000)
        self.assertEqual(u200["sales"], 0)
        self.assertEqual(u200["prizes"], 0)
        self.assertEqual(u200["transactions_count"], 1)

    def test_parse_empty_activity(self):
        flow = parse_activity([])
        self.assertEqual(flow, {})

    def test_analyze_squad_clauses(self):
        players = [
            {
                "playerMaster": {"id": "1", "name": "Vinicius", "nickname": "Vini", "marketValue": 50_000_000, "positionId": 4},
                "buyoutClause": 70_000_000,
            },
            {
                "playerMaster": {"id": "2", "name": "Pedri", "marketValue": 30_000_000, "positionId": 3},
                "buyoutClause": 30_000_000,
            },
            {
                "playerMaster": {"id": "3", "name": "Kubo", "marketValue": 25_000_000, "positionId": 3},
                "buyoutClause": 85_000_000,  # Max clause
            },
        ]
        res = analyze_squad_clauses(players)
        self.assertIsNotNone(res["top_protected"])
        self.assertEqual(res["top_protected"]["name"], "Kubo")
        self.assertEqual(res["top_protected"]["invested"], 60_000_000)
        self.assertEqual(res["max_clause_player"]["buyout_clause"], 85_000_000)

    def test_autocalibrate_initial_cash(self):
        teams = [
            {"managerId": 100, "teamMoney": None},
            {"managerId": 200, "teamMoney": 10_000_000},
        ]
        flow = {
            200: {"purchases": 25_000_000, "sales": 15_000_000, "prizes": 2_000_000},
        }
        # Initial cash = 10M (current) - 15M (sales) - 2M (prizes) + 25M (purchases) = 18M
        init = autocalibrate_initial_cash(teams, flow)
        self.assertEqual(init, 18_000_000)

    def test_analyze_rivals(self):
        teams = [
            {
                "id": "1001",
                "managerId": 100,
                "position": 1,
                "teamPoints": 50,
                "teamValue": 100_000_000,
                "manager": {"id": 100, "managerName": "Leader"},
                "players": [
                    {
                        "playerMaster": {"id": "1", "name": "Star", "marketValue": 40_000_000, "positionId": 4},
                        "buyoutClause": 55_000_000,
                    }
                ],
                "teamMoney": None,  # rival -> money hidden
            },
            {
                "id": "1002",
                "managerId": 200,
                "position": 2,
                "teamPoints": 40,
                "teamValue": 80_000_000,
                "manager": {"id": 200, "managerName": "MyTeam"},
                "players": [],
                "teamMoney": 12_345_678,  # authenticated user
            },
        ]
        activity = [
            {"id": "a1", "activityTypeId": TYPE_MARKET_BUY, "user1Id": 100, "amount": 25_000_000},
            {"id": "a2", "activityTypeId": TYPE_MARKET_SELL, "user1Id": 100, "amount": 10_000_000},
            {"id": "a3", "activityTypeId": TYPE_MATCHDAY_REWARD, "user1Id": 100, "amount": 1_000_000},
            # User 200 activity:
            {"id": "a4", "activityTypeId": TYPE_MARKET_BUY, "user1Id": 200, "amount": 20_000_000},
            {"id": "a5", "activityTypeId": TYPE_MARKET_SELL, "user1Id": 200, "amount": 10_000_000},
        ]

        class MockClient:
            def league_teams(self, lid):
                return teams
            def league_activity(self, lid, fetch_all=True):
                return activity

        rivals = analyze_rivals(MockClient(), "017906460", initial_budget=50_000_000)
        self.assertEqual(len(rivals), 2)

        r1 = rivals[0]
        self.assertEqual(r1["manager_name"], "Leader")
        self.assertEqual(r1["purchases"], 25_000_000)
        self.assertEqual(r1["sales"], 10_000_000)
        self.assertEqual(r1["prizes"], 1_000_000)
        # Expected balance = 50M (initial) + 10M (sales) + 1M (prizes) - 25M (purchases) = 36M
        self.assertEqual(r1["estimated_balance"], 36_000_000)
        self.assertFalse(r1["is_me"])

        r2 = rivals[1]
        self.assertEqual(r2["manager_name"], "MyTeam")
        self.assertTrue(r2["is_me"])
        self.assertEqual(r2["known_balance"], 12_345_678)
        # Pure estimated balance = 50M (initial) + 10M (sales) - 20M (purchases) = 40M
        self.assertEqual(r2["estimated_balance"], 40_000_000)

    def test_analyze_rivals_autocalibrates_when_no_budget(self):
        # With NO explicit initial_budget, the baseline must be DERIVED from my own
        # known balance (teamMoney) — not the 100M hardcoded default. Every manager in
        # a league starts with the same budget, so anchoring to mine is exact.
        teams = [
            {
                "id": "1001", "managerId": 100, "position": 1, "teamPoints": 50,
                "teamValue": 100_000_000, "manager": {"id": 100, "managerName": "Leader"},
                "players": [], "teamMoney": None,  # rival -> money hidden
            },
            {
                "id": "1002", "managerId": 200, "position": 2, "teamPoints": 40,
                "teamValue": 80_000_000, "manager": {"id": 200, "managerName": "MyTeam"},
                "players": [], "teamMoney": 12_345_678,  # authenticated user
            },
        ]
        activity = [
            {"id": "a1", "activityTypeId": TYPE_MARKET_BUY, "user1Id": 100, "amount": 25_000_000},
            {"id": "a2", "activityTypeId": TYPE_MARKET_SELL, "user1Id": 100, "amount": 10_000_000},
            {"id": "a3", "activityTypeId": TYPE_MATCHDAY_REWARD, "user1Id": 100, "amount": 1_000_000},
            {"id": "a4", "activityTypeId": TYPE_MARKET_BUY, "user1Id": 200, "amount": 20_000_000},
            {"id": "a5", "activityTypeId": TYPE_MARKET_SELL, "user1Id": 200, "amount": 10_000_000},
        ]

        class MockClient:
            def league_teams(self, lid):
                return teams
            def league_activity(self, lid, fetch_all=True):
                return activity

        rivals = analyze_rivals(MockClient(), "017906461")  # no initial_budget
        by_name = {r["manager_name"]: r for r in rivals}
        # My initial = 12,345,678 (now) - 10M (sales) - 0 (prizes) + 20M (purchases)
        expected_initial = 12_345_678 - 10_000_000 + 20_000_000  # 22,345,678
        self.assertEqual(by_name["MyTeam"]["initial_cash"], expected_initial)
        # Leader net_profit = 10M sales + 1M prizes - 25M purchases = -14M
        self.assertEqual(by_name["Leader"]["estimated_balance"], expected_initial - 14_000_000)

    def test_estimated_balance_can_be_negative_not_clamped_to_zero(self):
        # LaLiga lets a manager go negative. A heavy spender whose purchases exceed their
        # cash must show a NEGATIVE estimate — the old max(0, ...) hid real -20M/-28M
        # balances behind a bogus 0 (this is the ibairapado case).
        teams = [
            {"id": "1", "managerId": 100, "position": 1, "teamPoints": 10,
             "teamValue": 300_000_000, "manager": {"id": 100, "managerName": "BigSpender"},
             "players": [], "teamMoney": None},
            {"id": "2", "managerId": 200, "position": 2, "teamPoints": 10,
             "teamValue": 100_000_000, "manager": {"id": 200, "managerName": "Me"},
             "players": [], "teamMoney": 5_000_000},
        ]
        activity = [
            {"id": "a1", "activityTypeId": TYPE_MARKET_BUY, "user1Id": 100, "amount": 110_000_000},
            {"id": "a2", "activityTypeId": TYPE_MARKET_SELL, "user1Id": 100, "amount": 40_000_000},
        ]

        class MockClient:
            def league_teams(self, lid):
                return teams
            def league_activity(self, lid, fetch_all=True):
                return activity

        rivals = analyze_rivals(MockClient(), "NEG-LEAGUE", initial_budget=50_000_000)
        big = next(r for r in rivals if r["manager_name"] == "BigSpender")
        # net = 40M sales - 110M purchases = -70M ; est = 50M - 70M = -20M (floor -30M not binding)
        self.assertEqual(big["estimated_balance"], -20_000_000)

    def test_estimated_balance_floored_at_minus_10pct_squad(self):
        # A real balance can't drop below -10% of squad value (LaLiga blocks it), so an
        # incomplete history must not invent an impossible super-negative.
        teams = [
            {"id": "1", "managerId": 100, "position": 1, "teamPoints": 10,
             "teamValue": 100_000_000, "manager": {"id": 100, "managerName": "Broke"},
             "players": [], "teamMoney": None},
        ]
        activity = [
            {"id": "a1", "activityTypeId": TYPE_MARKET_BUY, "user1Id": 100, "amount": 90_000_000},
        ]

        class MockClient:
            def league_teams(self, lid):
                return teams
            def league_activity(self, lid, fetch_all=True):
                return activity

        rivals = analyze_rivals(MockClient(), "FLOOR-LEAGUE", initial_budget=50_000_000)
        # net = -90M ; raw est = 50 - 90 = -40M ; floor = -10% * 100M = -10M -> -10M
        self.assertEqual(rivals[0]["estimated_balance"], -10_000_000)

    def test_diff_rival_clauses(self):
        prev = {
            "managers": {
                "Leader": {
                    "10": {"name": "Mbappe", "clause": 50_000_000},
                    "20": {"name": "Bellingham", "clause": 40_000_000},
                }
            }
        }
        curr = {
            "managers": {
                "Leader": {
                    "10": {"name": "Mbappe", "clause": 65_000_000},  # raised +15M
                    "20": {"name": "Bellingham", "clause": 40_000_000},
                }
            }
        }
        diffs = diff_rival_clauses(prev, curr)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["manager"], "Leader")
        self.assertEqual(diffs[0]["name"], "Mbappe")
        self.assertEqual(diffs[0]["delta"], 15_000_000)


if __name__ == "__main__":
    unittest.main()
