"""Smoke tests for the `rivals` and `history` CLI commands.

These commands were shipped calling their strategy functions with the wrong
signature (an extra team-id arg) and reading fields the functions never return, so
`rivals --json` / `history --json` crashed on the very first line. The strategy
modules are unit-tested in isolation, but nothing exercised the CLI glue — these
tests close that gap: they run the actual command handlers end to end against a
mock client and assert they emit valid JSON without raising.
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

from fantasybot import cli
from fantasybot import state as state_mod


_TEAMS = [
    {"id": "1001", "managerId": 100, "position": 1, "teamPoints": 50,
     "teamValue": 100_000_000, "manager": {"id": 100, "managerName": "Leader"},
     "players": [{"playerMaster": {"id": "1", "nickname": "Star", "name": "Star",
                                   "marketValue": 40_000_000, "positionId": 4},
                  "buyoutClause": 55_000_000}],
     "teamMoney": None},
    {"id": "1002", "managerId": 200, "position": 2, "teamPoints": 40,
     "teamValue": 80_000_000, "manager": {"id": 200, "managerName": "MyTeam"},
     "players": [], "teamMoney": 12_345_678},
]
_ACTIVITY = [
    {"id": "a1", "activityTypeId": 31, "user1Id": 100, "playerMasterId": "1", "amount": 25_000_000,
     "createdAt": "2026-08-01T10:00:00"},
    {"id": "a2", "activityTypeId": 33, "user1Id": 100, "playerMasterId": "1", "amount": 30_000_000,
     "createdAt": "2026-08-05T10:00:00"},
    {"id": "a3", "activityTypeId": 6, "user1Id": 200, "amount": 1_000_000,
     "createdAt": "2026-08-06T10:00:00"},
]


class MockClient:
    def default_ids(self):
        return "017906460", "1002"

    def league_teams(self, lid):
        return _TEAMS

    def league_activity(self, lid, fetch_all=True):
        return _ACTIVITY

    def get(self, path):  # player-name resolution in history
        return {"nickname": "Star", "name": "Star", "positionId": 4, "marketValue": 40_000_000}


class _Args:
    def __init__(self, **kw):
        self.json = kw.get("json", True)
        self.manager = kw.get("manager")
        self.initial_budget = kw.get("initial_budget")


class TestCliAnalytics(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="fbtest-cli-")
        self._saved = {k: getattr(state_mod, k) for k in (
            "ACTIVITY_HISTORY_PATH", "PLAYERS_CACHE_PATH")}
        state_mod.ACTIVITY_HISTORY_PATH = os.path.join(self._tmp, "activity_history.json")
        state_mod.PLAYERS_CACHE_PATH = os.path.join(self._tmp, "players_cache.json")
        self._orig_fc = cli.FantasyClient
        cli.FantasyClient = lambda: MockClient()

    def tearDown(self):
        cli.FantasyClient = self._orig_fc
        for k, v in self._saved.items():
            setattr(state_mod, k, v)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_rivals_json_runs_and_is_valid(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_rivals(_Args(json=True))
        data = json.loads(buf.getvalue())
        self.assertIsInstance(data, list)
        names = {r["manager_name"] for r in data}
        self.assertIn("Leader", names)
        me = next(r for r in data if r["is_me"])
        self.assertEqual(me["manager_name"], "MyTeam")

    def test_history_json_runs_and_is_valid(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_history(_Args(json=True))
        data = json.loads(buf.getvalue())
        self.assertIn("managers", data)
        self.assertTrue(any(m["manager_name"] == "Leader" for m in data["managers"]))

    def test_rivals_text_runs(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_rivals(_Args(json=False))
        self.assertIn("Leader", buf.getvalue())

    def test_history_text_runs(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_history(_Args(json=False))
        self.assertIn("Leader", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
