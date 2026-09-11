"""last_minute_bid must bid at LEAST the player's CURRENT market value.

A system auction freezes its listing `salePrice`, but a player's value is re-valued daily;
once the value climbs above the frozen salePrice, LaLiga rejects any bid below the new value
("can't bid below the player's value"). Regression test: the bot used to bid off the stale,
lower salePrice and get the bid rejected.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from fantasybot import bidding


def _iso(seconds_from_now):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).isoformat()


class _FakeClient:
    """Network-free stand-in for FantasyClient."""
    entries = []

    def market(self, league_id):
        return _FakeClient.entries

    def make_bid(self, league_id, market_id, amount):
        return {"ok": True, "amount": amount}


class BidValueFloor(unittest.TestCase):
    def setUp(self):
        self._patch = mock.patch.object(bidding, "FantasyClient", _FakeClient)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_bids_at_least_current_value_when_saleprice_is_stale(self):
        _FakeClient.entries = [{
            "id": "M1", "expirationDate": _iso(10), "numberOfBids": 0,
            "salePrice": 41_000_000,
            "playerMaster": {"nickname": "Barrios", "marketValue": 42_000_000}}]
        res = bidding.last_minute_bid("L", "M1", 50_000_000, dry_run=True)
        self.assertIsNotNone(res)
        self.assertGreaterEqual(res["amount"], 42_000_000)  # never below the current value

    def test_missing_price_does_not_crash(self):
        _FakeClient.entries = [{
            "id": "M1", "expirationDate": _iso(10), "numberOfBids": 0,
            "salePrice": None, "playerMaster": {"nickname": "X", "marketValue": None}}]
        self.assertIsNone(bidding.last_minute_bid("L", "M1", 50_000_000, dry_run=True))


if __name__ == "__main__":
    unittest.main()
