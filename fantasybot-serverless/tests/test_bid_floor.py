"""A bid under the player's current value is not a cheap bid — it is no bid.

LaLiga answers one with

    400 {"code":400,
         "message":"\\"8754920\\" is not a valid money quantity for this player",
         "errorCode":"030.01.01"}

and the listing closes without us. It cost a signing: the rival cap cut the
amount down to what the poorest plausible field could counter, the listing
required more than that, and the request was refused. These pin the floor.
"""

from datetime import timedelta

from fantasybot import bidding
from fantasybot.storage import utcnow
from tests.support import FakeClient, StorageTestCase, listing


class DecideNeverGoesUnderTheValue(StorageTestCase):
    def test_a_cap_below_the_value_produces_no_bid_at_all(self):
        """Not a smaller bid. None — there is no legal amount to send."""
        self.assertIsNone(bidding.decide(10_000_000, 0, 1, 8_754_920))
        self.assertIsNone(bidding.decide(10_000_000, 3, 1, 8_754_920),
                          "competition does not make an illegal bid legal")

    def test_a_cap_at_the_value_bids_the_value(self):
        self.assertEqual(bidding.decide(10_000_000, 0, 1, 10_000_000),
                         10_000_000)

    def test_a_contested_bid_is_still_floored_at_the_value(self):
        got = bidding.decide(10_000_000, 2, 300, 10_000_050)
        self.assertGreaterEqual(got, 10_000_000)
        self.assertLessEqual(got, 10_000_050)


class SnipeAgainstAMovingValue(StorageTestCase):
    def test_it_lifts_the_cap_to_meet_a_value_that_drifted_up(self):
        """The value is re-read at the close; the cap was sized an hour ago."""
        close = utcnow() + timedelta(seconds=5)
        client = FakeClient([listing("m1", close.isoformat(), value=10_200_000)])
        res = bidding.snipe("L", "m1", 10_000_000, client=client,
                            budget_seconds=10, log=lambda m: None)
        self.assertEqual(res["status"], "bid")
        self.assertEqual(client.bids[0]["amount"],
                         10_200_000 + bidding.UNCONTESTED_CUSHION,
                         "a 2% drift must not turn a planned signing into a "
                         "refused bid")

    def test_it_sends_nothing_when_the_value_runs_past_the_ceiling(self):
        close = utcnow() + timedelta(seconds=5)
        client = FakeClient([listing("m1", close.isoformat(), value=20_000_000)])
        res = bidding.snipe("L", "m1", 10_000_000, client=client,
                            budget_seconds=10, log=lambda m: None)
        self.assertEqual(res["status"], "over_cap")
        self.assertEqual(client.bids, [],
                         "a request LaLiga cannot accept is not worth sending")
        self.assertEqual(res["value"], 20_000_000)

    def test_an_explicit_ceiling_beats_the_default_drift(self):
        close = utcnow() + timedelta(seconds=5)
        client = FakeClient([listing("m1", close.isoformat(), value=13_000_000)])
        res = bidding.snipe("L", "m1", 10_000_000, ceiling=14_000_000,
                            client=client, budget_seconds=10,
                            log=lambda m: None)
        self.assertEqual(res["status"], "bid")
        self.assertEqual(client.bids[0]["amount"],
                         13_000_000 + bidding.UNCONTESTED_CUSHION)

    def test_the_last_call_bid_is_floored_too(self):
        """The last-call branch sizes its own amount; it needs the same floor."""
        close = utcnow() + timedelta(seconds=40)
        client = FakeClient([listing("m1", close.isoformat(), value=10_200_000)])
        res = bidding.snipe("L", "m1", 10_000_000, client=client,
                            budget_seconds=0.5, last_call_seconds=70,
                            log=lambda m: None)
        self.assertEqual(res["status"], "bid")
        self.assertGreaterEqual(client.bids[0]["amount"], 10_200_000)


class TheRivalCapCannotCutBelowTheAskingPrice(StorageTestCase):
    def test_an_unknown_value_leaves_the_cap_alone(self):
        """The bug, in one call.

        `valor_actual` comes from a scrape. When the scrape is degraded it is
        None, the floor collapses to nothing, and a nine-million listing gets a
        bid of one million one hundred. The cap has to stand instead.
        """
        self.assertEqual(
            bidding.cap_against_rivals(9_000_000, None, 1_000_000), 9_000_000)

    def test_it_still_lowers_a_generous_cap_when_the_value_is_known(self):
        self.assertEqual(
            bidding.cap_against_rivals(20_000_000, 10_000_000, 12_000_000),
            13_200_000)
