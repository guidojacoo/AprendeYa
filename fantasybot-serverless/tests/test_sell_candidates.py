"""SELL advisor (strategy/sell.py): informational recommendations only — no execution.

Covers: the Aubameyang-case fix (an out-of-XI valuable player must NOT be flagged just
for that — the probable-lineup data lags for recent signings), the out_of_league signal,
lineup-coach protection, and the opt-in low-cash "not playing" exception.
"""
import unittest
from unittest import mock

from fantasybot.strategy import sell

TRENDS = {"_tag": "trends"}
PROBS = {"_tag": "probs"}


def _p(pid, ptid, value, pos=4, status=None):
    p = {"playerTeamId": ptid,
         "playerMaster": {"id": pid, "nickname": pid, "name": pid,
                          "positionId": pos, "marketValue": value}}
    if status is not None:
        p["playerMaster"]["playerStatus"] = status
    return p


def _dual(trend_map=None, prob_map=None):
    """Patch sell.match_name to answer from `trend_map` against TRENDS and from
    `prob_map` (as {'prob': n}) against PROBS — the two different indexes
    `sell_candidates` looks names up against."""
    trend_map, prob_map = trend_map or {}, prob_map or {}

    def fake(nick, name, idx):
        if idx is TRENDS:
            return {"tendencia": trend_map[nick]} if trend_map.get(nick) is not None else None
        if idx is PROBS:
            return {"prob": prob_map[nick]} if prob_map.get(nick) is not None else None
        return None
    return mock.patch.object(sell, "match_name", fake)


def _best(coach=None):
    return {"payload": {"goalkeeper": "gk", "defender": [], "midfield": [], "striker": []},
            "coach": coach}


class SellCandidatesNoTransferRisk(unittest.TestCase):
    """`sell_candidates` must NOT flag a valuable-but-out-of-XI player just for that (the
    'transfer risk' / Aubameyang false positive) — only a clearly falling value."""

    def test_out_of_xi_valuable_not_falling_is_not_flagged(self):
        team = {"teamMoney": 10_000_000, "players": [_p("valuable", "v1", 30_000_000)]}
        with _dual({"valuable": 5}):  # rising/stable
            out = sell.sell_candidates(team, _best(), TRENDS)
        self.assertEqual(out, [])

    def test_falling_out_of_xi_still_flagged(self):
        team = {"teamMoney": 10_000_000, "players": [_p("falling", "f1", 10_000_000)]}
        with _dual({"falling": -40}):
            out = sell.sell_candidates(team, _best(), TRENDS)
        self.assertEqual(len(out), 1)
        self.assertIn("falling", out[0]["reason"])


class SellCandidatesOutOfLeague(unittest.TestCase):
    """out_of_league is an OFFICIAL sell signal — the player LEFT LaLiga, so his fantasy
    value collapses. Priority 0 (ahead of falling value), regardless of trend."""

    def test_out_of_league_flagged_first_ahead_of_falling(self):
        team = {"teamMoney": 10_000_000, "players": [
            _p("falling", "f1", 8_000_000),
            _p("gone", "g1", 12_000_000, status="out_of_league"),
        ]}
        with _dual({"falling": -40, "gone": 5}):  # 'gone' even rising -> still flagged
            out = sell.sell_candidates(team, _best(), TRENDS)
        self.assertEqual([c["nombre"] for c in out], ["gone", "falling"])
        self.assertEqual(out[0]["priority"], 0)
        self.assertIn("fuera de LaLiga", out[0]["reason"])

    def test_ok_status_non_falling_not_flagged(self):
        team = {"teamMoney": 10_000_000, "players": [_p("fine", "fi", 15_000_000, status="ok")]}
        with _dual({"fine": 10}):
            out = sell.sell_candidates(team, _best(), TRENDS)
        self.assertEqual(out, [])


class SellProtectsLineupCoach(unittest.TestCase):
    """Premium: the coach the lineup USES must not be listed for sale, but a SURPLUS
    coach (not the selected one) stays sellable."""

    def test_selected_coach_not_sold_even_if_falling(self):
        team = {"teamMoney": 10_000_000, "players": [_p("coach", "c1", 3_000_000, pos=5)]}
        with _dual({"coach": -40}):
            out = sell.sell_candidates(team, _best(coach="c1"), TRENDS)
        self.assertEqual(out, [])

    def test_surplus_coach_can_be_sold(self):
        team = {"teamMoney": 10_000_000, "players": [_p("coach2", "c2", 3_000_000, pos=5)]}
        with _dual({"coach2": -40}):
            out = sell.sell_candidates(team, _best(coach="c1"), TRENDS)  # c1 selected, c2 surplus
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["nombre"], "coach2")


class SquadValueAndLowCash(unittest.TestCase):
    def test_low_cash_relative_to_squad_value(self):
        team = {"teamMoney": 1_000_000,
                "players": [_p("a", "a1", 40_000_000), _p("b", "b1", 40_000_000)]}
        self.assertEqual(sell.squad_value(team), 80_000_000)
        self.assertTrue(sell.is_low_cash(team))  # 1M < 15% of 80M (12M)

    def test_not_low_cash_when_comfortable(self):
        team = {"teamMoney": 20_000_000,
                "players": [_p("a", "a1", 40_000_000), _p("b", "b1", 40_000_000)]}
        self.assertFalse(sell.is_low_cash(team))  # 20M > 15% of 80M (12M)

    def test_zero_squad_value_is_never_low_cash_unless_negative(self):
        self.assertFalse(sell.is_low_cash({"teamMoney": 0, "players": []}))

    def test_negative_balance_is_always_low_cash(self):
        self.assertTrue(sell.is_low_cash({"teamMoney": -1, "players": []}))
        team = {"teamMoney": -500_000,
                "players": [_p("a", "a1", 40_000_000), _p("b", "b1", 40_000_000)]}
        self.assertTrue(sell.is_low_cash(team))


class SellCandidatesBenchSignalGating(unittest.TestCase):
    """The 'no juega' reason must ONLY fire with prob_index given AND cash thin AND a
    KNOWN low probability — every other combination behaves exactly as before."""

    def _team(self, money):
        return {"teamMoney": money, "players": [_p("benched", "be", 10_000_000)]}

    def test_backcompat_no_prob_index_never_flags_bench(self):
        team = self._team(100_000)  # thin cash
        with _dual({"benched": 5}, {"benched": 3}):  # stable value, very low prob
            out = sell.sell_candidates(team, _best(), TRENDS)  # prob_index omitted
        self.assertEqual(out, [])

    def test_healthy_cash_does_not_flag_low_prob_bench(self):
        team = self._team(10_000_000)  # comfortable cash
        with _dual({"benched": 5}, {"benched": 3}):
            out = sell.sell_candidates(team, _best(), TRENDS, prob_index=PROBS)
        self.assertEqual(out, [])

    def test_thin_cash_unmatched_prob_not_flagged(self):
        team = self._team(100_000)
        with _dual({"benched": 5}, {}):  # name not matched -> prob None
            out = sell.sell_candidates(team, _best(), TRENDS, prob_index=PROBS)
        self.assertEqual(out, [])

    def test_thin_cash_decent_prob_not_flagged(self):
        team = self._team(100_000)
        with _dual({"benched": 5}, {"benched": 50}):  # plays plenty
            out = sell.sell_candidates(team, _best(), TRENDS, prob_index=PROBS)
        self.assertEqual(out, [])

    def test_thin_cash_and_very_low_prob_is_flagged(self):
        team = self._team(100_000)
        with _dual({"benched": 5}, {"benched": 3}):
            out = sell.sell_candidates(team, _best(), TRENDS, prob_index=PROBS)
        self.assertEqual(len(out), 1)
        self.assertIn("no juega", out[0]["reason"])
        self.assertEqual(out[0]["priority"], 2)

    def test_falling_value_still_outranks_bench_signal(self):
        team = {"teamMoney": 100_000, "players": [
            _p("falling", "fa", 10_000_000), _p("benched", "be", 5_000_000)]}
        with _dual({"falling": -40, "benched": None}, {"falling": None, "benched": 3}):
            out = sell.sell_candidates(team, _best(), TRENDS, prob_index=PROBS)
        self.assertEqual([c["nombre"] for c in out], ["falling", "benched"])
        self.assertEqual(out[0]["priority"], 1)
        self.assertEqual(out[1]["priority"], 2)


if __name__ == "__main__":
    unittest.main()
