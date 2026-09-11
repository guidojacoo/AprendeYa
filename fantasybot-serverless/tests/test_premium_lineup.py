"""PREMIUM-only lineup extras: COACH + CAPTAIN + BENCH.

A premium league (config.premiumFeatures.formations) unlocks a coach (positionId 5,
owned like a player), a captain (doubles points), and a bench. When we PUT a premium
lineup we must also carry those, or LaLiga drops the coach and captain. These are added
behind the premium flag only: a NON-premium payload must stay byte-identical to today
(no coach/captain/bench keys).
"""

import unittest

from fantasybot.execute import apply_lineup
from fantasybot.strategy.lineup import optimize, payload_ids


def _player(pid, position_id, value, status="ok", avg=0, name=None):
    return {
        "playerTeamId": pid,
        "playerMaster": {
            "id": f"m{pid}",
            "nickname": name or pid,
            "name": name or pid,
            "positionId": position_id,   # 1 GK, 2 DEF, 3 MED, 4 DEL, 5 COACH
            "marketValue": value,
            "playerStatus": status,
            "averagePoints": avg,
            "lastSeasonPoints": 0,
        },
    }


def _full_squad():
    """A complete, healthy squad (fills a 4-3-3) with varied form for the captain."""
    players = [_player("gk1", 1, 5_000_000, avg=4)]
    players += [_player(f"d{i}", 2, 8_000_000, avg=3 + i) for i in range(1, 6)]     # 5 DEF
    players += [_player(f"m{i}", 3, 8_000_000, avg=5 + i) for i in range(1, 6)]     # 5 MED
    players += [_player(f"s{i}", 4, 10_000_000, avg=6 + i) for i in range(1, 4)]    # 3 DEL
    return players


class TestNonPremiumByteIdentical(unittest.TestCase):
    def test_non_premium_payload_has_no_premium_keys(self):
        best = optimize({"players": _full_squad()}, prob_index={})  # premium defaults False
        payload = best["payload"]
        self.assertNotIn("coach", payload)
        self.assertNotIn("captain", payload)
        self.assertNotIn("bench", payload)
        # exact key set of today's non-premium payload
        self.assertEqual(set(payload), {"goalkeeper", "defender", "midfield",
                                        "striker", "tactical_formation"})

    def test_non_premium_ignores_owned_coach(self):
        players = _full_squad() + [_player("coach1", 5, 3_000_000, avg=9)]
        best = optimize({"players": players}, prob_index={})
        self.assertNotIn("coach", best["payload"])


class TestPremiumCoachCaptainBench(unittest.TestCase):
    def test_premium_payload_has_coach_captain_and_empty_bench(self):
        players = _full_squad() + [_player("coach1", 5, 3_000_000, avg=9)]
        best = optimize({"players": players}, prob_index={}, premium=True)
        payload = best["payload"]
        self.assertEqual(payload.get("coach"), "coach1")
        self.assertIn("captain", payload)
        self.assertEqual(payload.get("bench"), {})

    def test_picks_the_better_of_two_coaches(self):
        players = _full_squad()
        players += [_player("coach_bad", 5, 2_000_000, avg=3),
                    _player("coach_good", 5, 2_000_000, avg=11)]
        best = optimize({"players": players}, prob_index={}, premium=True)
        self.assertEqual(best["payload"]["coach"], "coach_good")

    def test_prefers_available_coach_over_injured_higher_form(self):
        players = _full_squad()
        players += [_player("coach_inj", 5, 2_000_000, avg=15, status="injured"),
                    _player("coach_ok", 5, 2_000_000, avg=6)]
        best = optimize({"players": players}, prob_index={}, premium=True)
        self.assertEqual(best["payload"]["coach"], "coach_ok")

    def test_captain_is_top_scored_starter_in_the_xi(self):
        players = _full_squad() + [_player("coach1", 5, 3_000_000, avg=9)]
        best = optimize({"players": players}, prob_index={}, premium=True)
        captain = best["payload"]["captain"]
        self.assertIsInstance(captain, str)
        self.assertIn(captain, payload_ids(best))          # captain is in the XI
        self.assertEqual(captain, "s3")                    # highest averagePoints (9) starter

    def test_premium_without_coach_does_not_raise_and_omits_coach(self):
        best = optimize({"players": _full_squad()}, prob_index={}, premium=True)
        self.assertIsNone(best["payload"].get("coach"))
        self.assertIn("captain", best["payload"])
        self.assertEqual(best["payload"].get("bench"), {})


class TestApplyLineupCaptainChange(unittest.TestCase):
    def test_changed_when_only_captain_differs(self):
        players = _full_squad() + [_player("coach1", 5, 3_000_000, avg=9)]
        best = optimize({"players": players}, prob_index={}, premium=True)
        current_ids = payload_ids(best)   # same XI
        res = apply_lineup(
            {}, "tid", best, current_ids,
            current_coach=best["payload"].get("coach"),
            current_captain="someone_else",   # only the captain differs
            dry_run=True)
        self.assertTrue(res["changed"])

    def test_unchanged_when_xi_coach_and_captain_all_match(self):
        players = _full_squad() + [_player("coach1", 5, 3_000_000, avg=9)]
        best = optimize({"players": players}, prob_index={}, premium=True)
        res = apply_lineup(
            {}, "tid", best, payload_ids(best),
            current_coach=best["payload"].get("coach"),
            current_captain=best["payload"].get("captain"),
            dry_run=True)
        self.assertFalse(res["changed"])


class TestPremiumPutFallback(unittest.TestCase):
    """If the premium PUT format is rejected, apply_lineup retries XI-only and reports
    premium_applied=False — so a paid user is NEVER told a captain/coach was set that wasn't."""

    class _FC:
        def __init__(self, reject_extras):
            self.reject_extras = reject_extras
            self.puts = []

        def update_lineup(self, tid, payload):
            self.puts.append(payload)
            if self.reject_extras and any(k in payload for k in ("coach", "captain", "bench")):
                raise RuntimeError("400: premium fields rejected")
            return {"ok": True}

    def _best(self):
        players = _full_squad() + [_player("coach1", 5, 3_000_000, avg=9)]
        return optimize({"players": players}, prob_index={}, premium=True)

    def test_fallback_strips_extras_and_flags_not_applied(self):
        import unittest.mock as mock
        from fantasybot import execute
        fc = self._FC(reject_extras=True)
        with mock.patch.object(execute.events, "emit"):
            res = apply_lineup(fc, "tid", self._best(), current_ids=set(), dry_run=False)
        self.assertTrue(res["changed"])
        self.assertFalse(res["premium_applied"])        # must NOT claim the captain/coach
        self.assertEqual(len(fc.puts), 2)               # premium PUT + XI-only retry
        self.assertNotIn("captain", fc.puts[-1])        # retry stripped the extras

    def test_premium_applied_true_when_put_accepts(self):
        import unittest.mock as mock
        from fantasybot import execute
        fc = self._FC(reject_extras=False)
        with mock.patch.object(execute.events, "emit"):
            res = apply_lineup(fc, "tid", self._best(), current_ids=set(), dry_run=False)
        self.assertTrue(res["premium_applied"])
        self.assertEqual(len(fc.puts), 1)
        self.assertIn("captain", fc.puts[-1])


if __name__ == "__main__":
    unittest.main()
