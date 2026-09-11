"""Pure tests for the SHIELD advisor (blindaje): who is our most clause-vulnerable
valuable player. Mirrors the offensive clause_targets, defensively over OUR squad."""

import unittest
from datetime import datetime, timedelta, timezone

from fantasybot.strategy import shield

NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)


def _iso(days):
    return (NOW + timedelta(days=days)).isoformat()


def _player(pid, value, clause, shielded=False, unlock=None, pos=2, status=None):
    """An owned-player dict shaped like the real team payload."""
    return {"playerTeamId": f"pt-{pid}",
            "playerMaster": {"id": pid, "nickname": pid, "marketValue": value,
                             "positionId": pos, "playerStatus": status},
            "buyoutClause": clause,
            "buyoutClauseLockedEndTime": unlock,
            "isShielded": shielded}


REACH = 50_000_000


class ShieldCandidate(unittest.TestCase):
    def _pick(self, players):
        return shield.shield_candidate({"players": players}, REACH, now=NOW)

    def test_picks_valuable_unshielded_reachable_over_the_rest(self):
        players = [
            _player("cheap", 500_000, 400_000, unlock=_iso(-1)),          # below value floor
            _player("shielded", 30_000_000, 20_000_000, shielded=True,     # already shielded
                    unlock=_iso(-1)),
            _player("locked", 40_000_000, 25_000_000, unlock=_iso(30)),    # clause locked far
            _player("good", 28_000_000, 22_000_000, unlock=_iso(-1)),      # THE one to shield
        ]
        c = self._pick(players)
        self.assertIsNotNone(c)
        self.assertEqual(c["player_id"], "good")
        self.assertEqual(c["player_team_id"], "pt-good")

    def test_prefers_the_most_valuable_qualifier(self):
        players = [
            _player("okA", 20_000_000, 18_000_000, unlock=_iso(-1)),
            _player("okB", 35_000_000, 30_000_000, unlock=_iso(-2)),  # unlocked, richer
        ]
        self.assertEqual(self._pick(players)["player_id"], "okB")

    def test_clause_beyond_rivals_reach_is_not_vulnerable(self):
        # clause 60M > 50M reach -> no rival can pay it -> nothing to shield
        players = [_player("safe", 40_000_000, 60_000_000, unlock=_iso(-1))]
        self.assertIsNone(self._pick(players))

    def test_already_shielded_only_returns_none(self):
        players = [_player("s", 40_000_000, 20_000_000, shielded=True, unlock=_iso(-1))]
        self.assertIsNone(self._pick(players))

    def test_clause_locked_far_returns_none(self):
        players = [_player("far", 40_000_000, 20_000_000, unlock=_iso(30))]
        self.assertIsNone(self._pick(players))

    def test_clause_still_locked_does_not_qualify(self):
        # a still-locked clause (even unlocking soon) means the player is already protected —
        # the shield API rejects him (400 "Player team protected"), so he must NOT be picked.
        players = [_player("soon", 40_000_000, 20_000_000, unlock=_iso(2))]
        self.assertIsNone(self._pick(players))

    def test_missing_unlock_treated_as_open(self):
        players = [_player("nolock", 40_000_000, 20_000_000, unlock=None)]
        self.assertEqual(self._pick(players)["player_id"], "nolock")

    def test_cheap_only_returns_none(self):
        players = [_player("cheap", 500_000, 100_000, unlock=_iso(-1))]
        self.assertIsNone(self._pick(players))

    def test_zero_reach_shields_nothing(self):
        players = [_player("good", 28_000_000, 22_000_000, unlock=_iso(-1))]
        self.assertIsNone(shield.shield_candidate({"players": players}, 0, now=NOW))

    def test_no_clause_is_not_vulnerable(self):
        players = [_player("noclause", 40_000_000, 0, unlock=_iso(-1))]
        self.assertIsNone(self._pick(players))

    def test_empty_squad_returns_none(self):
        self.assertIsNone(shield.shield_candidate({"players": []}, REACH, now=NOW))

    def test_injured_player_is_skipped_for_a_healthy_one(self):
        # The most valuable is injured (the Isi case): shield the healthy asset instead.
        players = [
            _player("isi", 30_000_000, 25_000_000, unlock=_iso(-1), status="injured"),
            _player("healthy", 20_000_000, 18_000_000, unlock=_iso(-1), status="ok"),
        ]
        c = self._pick(players)
        self.assertEqual(c["player_id"], "healthy")

    def test_only_vulnerable_player_injured_returns_none(self):
        players = [_player("isi", 30_000_000, 25_000_000, unlock=_iso(-1), status="injured")]
        self.assertIsNone(self._pick(players))

    def test_suspended_and_out_of_league_are_skipped(self):
        players = [
            _player("susp", 30_000_000, 25_000_000, unlock=_iso(-1), status="suspended"),
            _player("gone", 28_000_000, 24_000_000, unlock=_iso(-1), status="out_of_league"),
        ]
        self.assertIsNone(self._pick(players))

    def test_doubtful_player_is_still_shieldable(self):
        # "doubtful" (a knock) may still play, so he stays a valid asset to protect.
        players = [_player("dud", 30_000_000, 25_000_000, unlock=_iso(-1), status="doubtful")]
        self.assertEqual(self._pick(players)["player_id"], "dud")

    def test_too_early_before_gameweek_holds_off(self):
        # kickoff 4 days away (>72h): a 48h shield now would lapse before the gameweek's
        # clause window (72h..24h before) -> hold off.
        players = [_player("good", 28_000_000, 22_000_000, unlock=_iso(-1))]
        c = shield.shield_candidate({"players": players}, REACH, now=NOW,
                                    gameweek_kickoff=_iso(4))
        self.assertIsNone(c)

    def test_within_72h_of_gameweek_shields(self):
        # kickoff 2.5 days away (<72h) -> shield now so it covers the clause window.
        players = [_player("good", 28_000_000, 22_000_000, unlock=_iso(-1))]
        c = shield.shield_candidate({"players": players}, REACH, now=NOW,
                                    gameweek_kickoff=_iso(2.5))
        self.assertEqual(c["player_id"], "good")

    def test_unknown_kickoff_does_not_gate(self):
        players = [_player("good", 28_000_000, 22_000_000, unlock=_iso(-1))]
        c = shield.shield_candidate({"players": players}, REACH, now=NOW,
                                    gameweek_kickoff=None)
        self.assertEqual(c["player_id"], "good")


if __name__ == "__main__":
    unittest.main()
