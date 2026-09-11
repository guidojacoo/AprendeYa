"""Captain helper (premium leagues): a pure, deterministic pick.

The captain doubles his points this gameweek, so the helper must name the XI's
best expected scorer — recent form tilted by start probability — and only ever an
available starter (an injured filler scores 0, so captaining him wastes the double).

Real user question: does the pick account for the opponent (e.g. the best-form player
facing Real Madrid/Barcelona)? It didn't. `team_strength_index` / `fixture_difficulty_
by_team` / the `fixture_difficulty` argument add that as a NUDGE among comparable
options — never enough to override a clearly better captain.
"""

import unittest

from fantasybot.strategy.captain import (
    captain_value, fixture_difficulty_by_team, form, pick_captain,
    team_strength_index,
)


def _cand(pid, avg=None, points=None, last=None, value=0, prob=None, disponible=True,
         team_id=None):
    pm = {"id": f"m{pid}", "marketValue": value}
    if avg is not None:
        pm["averagePoints"] = avg
    if points is not None:
        pm["points"] = points
    if last is not None:
        pm["lastSeasonPoints"] = last
    if team_id is not None:
        pm["team"] = {"id": team_id}
    return {"playerTeamId": pid, "playerMaster": pm, "prob": prob, "disponible": disponible}


class TestForm(unittest.TestCase):
    def test_prefers_average_then_points_then_last(self):
        self.assertEqual(form({"averagePoints": 7, "points": 100, "lastSeasonPoints": 200}), 7.0)
        self.assertEqual(form({"points": 100, "lastSeasonPoints": 200}), 100.0)
        self.assertEqual(form({"lastSeasonPoints": 200}), 200.0)
        self.assertEqual(form({}), 0.0)


class TestPickCaptain(unittest.TestCase):
    def test_picks_highest_form_starter(self):
        xi = [_cand("a", avg=4), _cand("b", avg=9), _cand("c", avg=6)]
        self.assertEqual(pick_captain(xi), "b")

    def test_probability_breaks_close_form(self):
        # equal form, but one is a nailed starter (95%) vs a rotation risk (20%)
        xi = [_cand("nailed", avg=6, prob=95), _cand("rotates", avg=6, prob=20)]
        self.assertEqual(pick_captain(xi), "nailed")

    def test_ignores_unavailable_players(self):
        # the highest-form player is injured -> he must NOT be captained (scores 0)
        xi = [_cand("star", avg=12, disponible=False), _cand("fit", avg=5, disponible=True)]
        self.assertEqual(pick_captain(xi), "fit")

    def test_none_when_no_eligible(self):
        self.assertIsNone(pick_captain([]))
        self.assertIsNone(pick_captain([_cand("x", avg=5, disponible=False)]))

    def test_captain_value_is_pure_number(self):
        v = captain_value(_cand("a", avg=5, value=10_000_000, prob=80))
        self.assertIsInstance(v, float)


def _player(pid, team_id, value):
    return {"id": pid, "teamId": team_id, "marketValue": value}


class TestTeamStrengthIndex(unittest.TestCase):
    def test_ranks_by_aggregate_squad_value(self):
        players = [
            _player("1", "weak", 1_000_000), _player("2", "weak", 1_000_000),
            _player("3", "mid", 5_000_000),
            _player("4", "rich", 50_000_000), _player("5", "rich", 50_000_000),
        ]
        idx = team_strength_index(players)
        self.assertEqual(idx["weak"], 0.0)
        self.assertEqual(idx["mid"], 0.5)
        self.assertEqual(idx["rich"], 1.0)

    def test_empty_input_is_empty_index(self):
        self.assertEqual(team_strength_index([]), {})

    def test_single_known_team_is_neutral(self):
        idx = team_strength_index([_player("1", "only", 1_000_000)])
        self.assertEqual(idx, {"only": 0.5})

    def test_players_without_team_id_are_ignored(self):
        idx = team_strength_index([_player("1", None, 1_000_000),
                                   _player("2", "a", 1_000_000)])
        self.assertEqual(idx, {"a": 0.5})

    def test_string_market_values_do_not_crash(self):
        # all_players() (the real LaLiga API) returns marketValue as a STRING, always --
        # a bare `+` on the first player used to TypeError immediately (caught live).
        idx = team_strength_index([
            {"id": "1", "teamId": "weak", "marketValue": "1000000"},
            {"id": "2", "teamId": "rich", "marketValue": "50000000"},
        ])
        self.assertEqual(idx, {"weak": 0.0, "rich": 1.0})

    def test_garbage_market_value_does_not_crash(self):
        idx = team_strength_index([{"id": "1", "teamId": "a", "marketValue": "n/a"}])
        self.assertEqual(idx, {"a": 0.5})


class TestFixtureDifficultyByTeam(unittest.TestCase):
    _players = [_player("1", "weak", 1_000_000), _player("2", "rich", 50_000_000)]

    def test_difficulty_is_the_opponents_strength_local_side(self):
        fixtures = [{"localId": "weak", "visitorId": "rich"}]
        out = fixture_difficulty_by_team(self._players, fixtures)
        self.assertEqual(out["weak"], 1.0)   # weak's rival (rich) is the strong one
        self.assertEqual(out["rich"], 0.0)   # rich's rival (weak) is the weak one

    def test_difficulty_is_the_opponents_strength_visitor_side(self):
        fixtures = [{"localId": "rich", "visitorId": "weak"}]
        out = fixture_difficulty_by_team(self._players, fixtures)
        self.assertEqual(out["weak"], 1.0)
        self.assertEqual(out["rich"], 0.0)

    def test_team_with_no_match_is_absent_not_zero(self):
        out = fixture_difficulty_by_team(self._players, [{"localId": "weak", "visitorId": "rich"}])
        self.assertNotIn("bye-team", out)

    def test_empty_fixtures_is_empty(self):
        self.assertEqual(fixture_difficulty_by_team(self._players, []), {})
        self.assertEqual(fixture_difficulty_by_team(self._players, None), {})


class CaptainValuesTheRival(unittest.TestCase):
    """The exact case Jon described: same form, one faces a tough rival."""

    def test_same_form_tough_rival_loses_to_soft_rival(self):
        difficulty = {"herrando_team": 1.0, "alt_team": 0.0}
        herrando = _cand("herrando", avg=6, team_id="herrando_team")
        alt = _cand("alt", avg=6, team_id="alt_team")
        self.assertEqual(
            pick_captain([herrando, alt], fixture_difficulty=difficulty), "alt")

    def test_clearly_better_captain_still_wins_against_the_toughest_rival(self):
        # Herrando's form is far ahead -> even the max penalty (toughest possible
        # rival) must not flip the pick. This is the "nudge, not a veto" guarantee.
        difficulty = {"herrando_team": 1.0, "alt_team": 0.0}
        herrando = _cand("herrando", avg=20, team_id="herrando_team")
        alt = _cand("alt", avg=6, team_id="alt_team")
        self.assertEqual(
            pick_captain([herrando, alt], fixture_difficulty=difficulty), "herrando")

    def test_missing_fixture_difficulty_is_todays_behaviour(self):
        xi = [_cand("a", avg=4, team_id="t1"), _cand("b", avg=9, team_id="t2")]
        self.assertEqual(pick_captain(xi), "b")
        self.assertEqual(pick_captain(xi, fixture_difficulty=None), "b")

    def test_team_missing_from_difficulty_map_is_neutral(self):
        # herrando's team isn't in the map (e.g. bye week) -> no penalty applied to him
        difficulty = {"alt_team": 1.0}
        herrando = _cand("herrando", avg=6, team_id="herrando_team")
        alt = _cand("alt", avg=6, team_id="alt_team")
        self.assertEqual(
            pick_captain([herrando, alt], fixture_difficulty=difficulty), "herrando")

    def test_penalty_is_capped_at_the_configured_weight(self):
        from fantasybot.strategy.captain import RIVAL_DIFFICULTY_WEIGHT
        base = captain_value(_cand("a", avg=6, team_id="t1"))
        worst = captain_value(_cand("a", avg=6, team_id="t1"),
                              fixture_difficulty={"t1": 1.0})
        self.assertAlmostEqual(worst, base * (1 - RIVAL_DIFFICULTY_WEIGHT))


if __name__ == "__main__":
    unittest.main()
