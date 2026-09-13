"""The bounded sniper: what a function does when it runs out of time.

`snipe()` is the only place in the serverless path that WAITS. Getting its exit
conditions wrong is how you either miss a close or bid twice, so each one is
pinned here.
"""

from datetime import timedelta

from fantasybot import bidding
from fantasybot.storage import utcnow
from tests.support import FakeClient, StorageTestCase, listing


class SnipeOutcomes(StorageTestCase):
    def test_waits_and_hands_back_when_the_close_is_far(self):
        close = utcnow() + timedelta(minutes=30)
        client = FakeClient([listing("m1", close.isoformat())])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=0.5, log=lambda m: None)
        self.assertEqual(res["status"], "waiting")
        self.assertEqual(client.bids, [],
                         "running out of budget must never send a bid")

    def test_bids_inside_the_final_window(self):
        close = utcnow() + timedelta(seconds=8)
        client = FakeClient([listing("m1", close.isoformat(), value=10_000_000)])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=10, log=lambda m: None)
        self.assertEqual(res["status"], "bid")
        self.assertEqual(len(client.bids), 1)
        self.assertEqual(client.bids[0]["amount"],
                         10_000_000 + bidding.UNCONTESTED_CUSHION)

    def test_it_bids_rather_than_hand_back_a_watch_nobody_will_take(self):
        """The bug that cost two signings.

        The lead is 60s, a tick can hold about 32 of them, and the final window
        starts at 15s. So the watch was handed back at ~25s to close and the next
        tick — a minute later, on a one-minute clock — found the listing gone.
        The auction was attended by nobody and the bid was never sent.
        """
        close = utcnow() + timedelta(seconds=40)
        client = FakeClient([listing("m1", close.isoformat(), value=10_000_000)])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=0.5, last_call_seconds=70,
                            log=lambda m: None)
        self.assertEqual(res["status"], "bid")
        self.assertTrue(res["last_call"])
        self.assertEqual(client.bids[0]["amount"],
                         10_000_000 + bidding.UNCONTESTED_CUSHION,
                         "the last call pays what the close would have paid")

    def test_the_last_call_does_not_fire_while_another_tick_is_still_coming(self):
        """Bidding early costs the sealed-timing edge, so it is only ever done
        when the alternative is not bidding at all."""
        close = utcnow() + timedelta(minutes=30)
        client = FakeClient([listing("m1", close.isoformat(), value=10_000_000)])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=0.5, last_call_seconds=70,
                            log=lambda m: None)
        self.assertEqual(res["status"], "waiting")
        self.assertEqual(client.bids, [])

    def test_the_last_call_respects_the_cap(self):
        close = utcnow() + timedelta(seconds=40)
        client = FakeClient([listing("m1", close.isoformat(),
                                     value=10_000_000, bids=3)])
        bidding.snipe("L", "m1", 10_020_000, client=client,
                      budget_seconds=0.5, last_call_seconds=70,
                      log=lambda m: None)
        self.assertEqual(client.bids[0]["amount"], 10_020_000)

    def test_without_a_last_call_window_the_old_behaviour_stands(self):
        """The CLI passes no budget and holds to the close itself; nothing there
        should start bidding early."""
        close = utcnow() + timedelta(seconds=40)
        client = FakeClient([listing("m1", close.isoformat(), value=10_000_000)])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=0.5, log=lambda m: None)
        self.assertEqual(res["status"], "waiting")
        self.assertEqual(client.bids, [])

    def test_competition_makes_it_bid_early(self):
        close = utcnow() + timedelta(minutes=10)
        client = FakeClient([listing("m1", close.isoformat(),
                                     value=10_000_000, bids=2)])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=10, log=lambda m: None)
        self.assertEqual(res["status"], "bid", "competition forces a move")
        self.assertEqual(client.bids[0]["amount"], 10_300_000)

    def test_cap_is_never_exceeded(self):
        close = utcnow() + timedelta(minutes=10)
        client = FakeClient([listing("m1", close.isoformat(),
                                     value=10_000_000, bids=5)])
        bidding.snipe("L", "m1", 10_050_000, client=client,
                      budget_seconds=10, log=lambda m: None)
        self.assertEqual(client.bids[0]["amount"], 10_050_000)

    def test_our_existing_bid_stops_it_before_anything_is_sent(self):
        close = utcnow() + timedelta(seconds=5)
        client = FakeClient([listing("m1", close.isoformat(),
                                     mine={"id": "b1", "money": 10_000_000})])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=10, log=lambda m: None)
        self.assertEqual(res["status"], "already")
        self.assertEqual(client.bids, [])

    def test_a_vanished_listing_is_not_an_error(self):
        client = FakeClient([])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=5, log=lambda m: None)
        self.assertEqual(res["status"], "gone")

    def test_an_unpriced_listing_is_refused_not_guessed(self):
        close = utcnow() + timedelta(seconds=5)
        row = listing("m1", close.isoformat())
        row["salePrice"] = 0
        row["playerMaster"]["marketValue"] = 0
        client = FakeClient([row])
        res = bidding.snipe("L", "m1", 11_000_000, client=client,
                            budget_seconds=5, log=lambda m: None)
        self.assertEqual(res["status"], "unpriced")
        self.assertEqual(client.bids, [])

    def test_dry_run_decides_but_sends_nothing(self):
        close = utcnow() + timedelta(seconds=5)
        client = FakeClient([listing("m1", close.isoformat())])
        res = bidding.snipe("L", "m1", 11_000_000, client=client, dry_run=True,
                            budget_seconds=5, log=lambda m: None)
        self.assertEqual(res["status"], "bid")
        self.assertTrue(res["dry_run"])
        self.assertEqual(client.bids, [])


class ApiErrorsDoNotWedgeTheQueue(StorageTestCase):
    def test_a_failing_bid_leaves_the_action_retryable(self):
        from fantasybot import scheduler
        from fantasybot.scheduler import TickContext
        from fantasybot.storage import PENDING

        close = utcnow() + timedelta(seconds=8)

        class Exploding(FakeClient):
            def make_bid(self, *a, **kw):
                raise RuntimeError("503 from LaLiga")

        client = Exploding([listing("m1", close.isoformat())])
        scheduler.schedule_bid("L", "m1", 11_000_000, close)
        ctx = TickContext(client=client, budget_seconds=15, log=lambda m: None)
        res = scheduler.run_due(ctx)
        self.assertEqual(res[0]["status"], PENDING,
                         "a transient API error must stay retryable")
        self.assertIn("503", res[0]["error"])

    def test_it_gives_up_after_max_attempts(self):
        from fantasybot import scheduler
        from fantasybot.scheduler import TickContext
        from fantasybot.storage import FAILED

        close = utcnow() + timedelta(seconds=8)

        class Exploding(FakeClient):
            def make_bid(self, *a, **kw):
                raise RuntimeError("permanently broken")

        client = Exploding([listing("m1", close.isoformat())])
        scheduler.schedule_bid("L", "m1", 11_000_000, close)
        last = None
        for _ in range(4):
            ctx = TickContext(client=client, budget_seconds=15,
                              max_attempts=3, log=lambda m: None)
            got = scheduler.run_due(ctx)
            if got:
                last = got[0]
        self.assertEqual(last["status"], FAILED,
                         "it must stop hammering an endpoint that keeps refusing")


class CapAgainstRivals(StorageTestCase):
    """Winning by ten million when nobody could have paid more than eight is ten
    million that does not buy the next player."""

    def test_it_lowers_a_generous_cap_to_the_field(self):
        self.assertEqual(
            bidding.cap_against_rivals(20_000_000, 10_000_000, 12_000_000),
            13_200_000, "the richest rival's cash plus the 10% margin")

    def test_it_never_raises_a_cap(self):
        self.assertEqual(
            bidding.cap_against_rivals(9_000_000, 8_000_000, 50_000_000),
            9_000_000)

    def test_it_never_goes_below_what_the_listing_requires(self):
        """Bidding under the player's own value is rejected by LaLiga outright."""
        got = bidding.cap_against_rivals(20_000_000, 10_000_000, 1_000_000)
        self.assertEqual(got, 10_000_000 + bidding.UNCONTESTED_CUSHION)

    def test_an_unknown_field_leaves_the_cap_alone(self):
        """Guessing low when we know nothing loses players for no reason."""
        for reach in (0, None, -5):
            self.assertEqual(
                bidding.cap_against_rivals(20_000_000, 10_000_000, reach),
                20_000_000, f"reach={reach}")

    def test_a_valueless_listing_keeps_its_cap(self):
        """These two used to expect the opposite, and that cost a signing.

        With no value there is no floor, so the rival ceiling had nothing to
        stop it: a five-million listing was capped at 1,100 € and LaLiga
        refused the bid outright. The cap came from the listing's own price;
        an unknown value is a reason to leave it alone, not to cut it to a
        number no listing would accept.
        """
        self.assertEqual(bidding.cap_against_rivals(5_000_000, None, 1_000),
                         5_000_000)

    def test_a_poor_field_cannot_cut_a_cap_it_cannot_price(self):
        self.assertEqual(bidding.cap_against_rivals(5_000_000, None, 5),
                         5_000_000)
