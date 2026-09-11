"""agent.captain_fixture_difficulty: the impure wrapper that feeds the captain picker
its rival-difficulty signal (see strategy/captain.py). Own try/except -- a failure here
must degrade to today's form-only captain, never crash the whole review.
"""

import unittest

from fantasybot import agent


class _FakeClient:
    def __init__(self, week=None, fixtures=None, players=None, raise_on=None):
        self._week = week if week is not None else {"weekNumber": 3}
        self._fixtures = fixtures if fixtures is not None else []
        self._players = players if players is not None else []
        self._raise_on = raise_on or ()

    def current_week(self):
        if "current_week" in self._raise_on:
            raise RuntimeError("boom")
        return self._week

    def calendar(self, week_number):
        if "calendar" in self._raise_on:
            raise RuntimeError("boom")
        return self._fixtures

    def all_players(self):
        if "all_players" in self._raise_on:
            raise RuntimeError("boom")
        return self._players


class CaptainFixtureDifficulty(unittest.TestCase):
    def test_happy_path_wires_the_three_calls_into_a_difficulty_map(self):
        players = [{"id": "1", "teamId": "weak", "marketValue": 1_000_000},
                  {"id": "2", "teamId": "rich", "marketValue": 50_000_000}]
        fixtures = [{"localId": "weak", "visitorId": "rich"}]
        client = _FakeClient(players=players, fixtures=fixtures)
        out = agent.captain_fixture_difficulty(client)
        self.assertEqual(out, {"weak": 1.0, "rich": 0.0})

    def test_current_week_failure_returns_empty_not_a_crash(self):
        client = _FakeClient(raise_on=("current_week",))
        self.assertEqual(agent.captain_fixture_difficulty(client), {})

    def test_calendar_failure_returns_empty_not_a_crash(self):
        client = _FakeClient(raise_on=("calendar",))
        self.assertEqual(agent.captain_fixture_difficulty(client), {})

    def test_all_players_failure_returns_empty_not_a_crash(self):
        client = _FakeClient(raise_on=("all_players",))
        self.assertEqual(agent.captain_fixture_difficulty(client), {})

    def test_empty_fixtures_returns_empty_map(self):
        client = _FakeClient(players=[{"id": "1", "teamId": "a", "marketValue": 1}],
                             fixtures=[])
        self.assertEqual(agent.captain_fixture_difficulty(client), {})


if __name__ == "__main__":
    unittest.main()
