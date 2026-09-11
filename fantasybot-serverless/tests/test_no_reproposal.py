"""Regression: the bot must not propose bidding on a player who already has money on him.

The local bids file only remembers THIS bot's bids. Anything placed from the official
app (or by hand from the CLI on another machine) is invisible to it, so `plan_bids`
kept re-proposing players the manager had already bid on — in a real league, 4 of 7
proposals were things already done, which makes the other 3 look untrustworthy too.
The live market is the truth: every listing carries its own `bid`/`offer` when money
is on it, whoever placed it.
"""

import os
import tempfile
import unittest

os.environ["FANTASYBOT_HOME"] = tempfile.mkdtemp(prefix="fb-noreprop-")

from fantasybot import execute  # noqa: E402


class _Client:
    """Network-free stand-in: a market where Aubameyang already has OUR bid."""

    def __init__(self, entries):
        self._entries = entries

    def market(self, league_id):
        return self._entries


_TEAM = {"teamMoney": 100_000_000}

_OPS = [
    {"market_id": "1", "nombre": "Aubameyang", "buy_price": 31_000_000,
     "margin_pct": 5.0, "via": "SISTEMA"},
    {"market_id": "3", "nombre": "Libre", "buy_price": 1_000_000,
     "margin_pct": 8.0, "via": "SISTEMA"},
]


class PlanRespectsLiveBids(unittest.TestCase):
    def test_listing_with_a_live_bid_is_skipped(self):
        client = _Client([
            {"id": 1, "bid": {"id": "b1", "money": 31_000_000, "status": "pending"}},
            {"id": 3},
        ])
        plan = execute.plan_bids(client, "L", _TEAM, ops=_OPS)
        self.assertEqual([p["nombre"] for p in plan], ["Libre"])

    def test_an_offer_to_a_manager_counts_the_same(self):
        # Different endpoint, different field — but the money is just as committed.
        client = _Client([
            {"id": 1, "offer": {"id": "o1", "money": 31_000_000, "status": "pending"}},
            {"id": 3},
        ])
        plan = execute.plan_bids(client, "L", _TEAM, ops=_OPS)
        self.assertEqual([p["nombre"] for p in plan], ["Libre"])

    def test_market_failure_degrades_to_the_local_file(self):
        class _Broken(_Client):
            def market(self, league_id):
                raise RuntimeError("market down")

        plan = execute.plan_bids(_Broken([]), "L", _TEAM, ops=_OPS)
        self.assertEqual(len(plan), 2)  # no crash: local file (empty) still applies


if __name__ == "__main__":
    unittest.main()
