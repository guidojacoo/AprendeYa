"""What the model is allowed to do — and everything it is not.

The LLM sees a summary and returns JSON. `apply_decision` is the only thing that
acts on that JSON, so this file is the security boundary: if a reply can reach
past these clamps, a hallucination (or a prompt injection riding in on a player
name) becomes a real bid.
"""

from datetime import timedelta

from fantasybot import scheduler
from fantasybot.llm import strategy
from fantasybot.storage import utcnow
from tests.support import StorageTestCase


class ApplyDecision(StorageTestCase):
    def setUp(self):
        super().setUp()
        self.close = utcnow() + timedelta(minutes=30)
        scheduler.schedule_bid("L1", "m1", 10_000_000, self.close, nombre="Uno")
        self.context = {
            "balance": 20_000_000,
            "scheduled_bids": [{"market_id": "m1", "nombre": "Uno",
                                "max_bid": 10_000_000,
                                "close_at": self.close.isoformat()}],
        }

    def _caps(self):
        return {(a.get("payload") or {}).get("market_id"):
                (a.get("payload") or {}).get("max_bid")
                for a in self.store.pending_actions()}

    def test_a_modest_raise_is_applied(self):
        out = strategy.apply_decision(
            {"bid_caps": [{"market_id": "m1", "max_bid": 11_000_000}]},
            self.context)
        self.assertEqual(self._caps()["m1"], 11_000_000)
        self.assertEqual(out["caps"][0]["to"], 11_000_000)

    def test_an_absurd_cap_is_clamped_not_obeyed(self):
        strategy.apply_decision(
            {"bid_caps": [{"market_id": "m1", "max_bid": 999_000_000}]},
            self.context)
        self.assertEqual(self._caps()["m1"], 13_000_000,
                         "+30% of the computed cap is the ceiling")

    def test_the_clamp_never_exceeds_the_balance(self):
        self.context["balance"] = 10_500_000
        strategy.apply_decision(
            {"bid_caps": [{"market_id": "m1", "max_bid": 99_000_000}]},
            self.context)
        self.assertEqual(self._caps()["m1"], 10_500_000)

    def test_a_market_id_it_invented_is_ignored(self):
        out = strategy.apply_decision(
            {"bid_caps": [{"market_id": "made-up", "max_bid": 5_000_000}]},
            self.context)
        self.assertEqual(out["caps"], [])
        self.assertEqual(out["ignored"][0]["why"], "not scheduled")
        self.assertEqual(len(self.store.pending_actions()), 1,
                         "it must not be able to CREATE a bid")

    def test_a_non_numeric_cap_is_ignored(self):
        out = strategy.apply_decision(
            {"bid_caps": [{"market_id": "m1", "max_bid": "todo el dinero"}]},
            self.context)
        self.assertEqual(out["caps"], [])
        self.assertEqual(self._caps()["m1"], 10_000_000)

    def test_avoid_cancels_a_queued_bid(self):
        out = strategy.apply_decision({"avoid": ["m1"]}, self.context)
        self.assertEqual(out["dropped"], ["m1"])
        self.assertEqual(self.store.pending_actions(), [])

    def test_a_sell_becomes_a_task_never_a_sale(self):
        from fantasybot import state
        out = strategy.apply_decision(
            {"sell": [{"player_id": "p9", "price": 7_000_000,
                       "reason": "peaked"}]},
            self.context)
        self.assertEqual(out["sell_tasks"], ["p9"])
        texts = [t["text"] for t in state.pending_tasks()]
        self.assertTrue(any("p9" in t for t in texts))
        self.assertEqual(len(self.store.pending_actions()), 1,
                         "no executable action may come out of a sell suggestion")

    def test_an_empty_decision_changes_nothing(self):
        out = strategy.apply_decision({}, self.context)
        self.assertEqual(out, {"caps": [], "dropped": [], "sell_tasks": [],
                               "ignored": []})
        self.assertEqual(self._caps()["m1"], 10_000_000)

    def test_junk_keys_are_not_executable(self):
        """Anything outside the documented schema must be inert, not a new path."""
        out = strategy.apply_decision(
            {"buy_now": [{"player_id": "p1", "amount": 50_000_000}],
             "pay_clause": ["p2"], "transfer_money": 1_000_000},
            self.context)
        self.assertEqual(out["caps"], [])
        self.assertEqual(out["dropped"], [])
        self.assertEqual(len(self.store.pending_actions()), 1)


class DisabledByDefault(StorageTestCase):
    def test_no_provider_means_no_llm(self):
        from fantasybot import config
        saved = config.LLM_PROVIDER
        config.LLM_PROVIDER = "none"
        try:
            self.assertFalse(strategy.enabled())
        finally:
            config.LLM_PROVIDER = saved
