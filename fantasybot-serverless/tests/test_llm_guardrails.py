"""What the model is allowed to do — and everything it is not.

The LLM sees a summary and returns JSON. `apply_decision` is the only thing that
acts on that JSON, so this file is the security boundary: if a reply can reach
past these clamps, a hallucination (or a prompt injection riding in on a player
name) becomes a real bid.
"""

import unittest
from datetime import timedelta
from unittest import mock

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


class RequestHeaders(unittest.TestCase):
    """How the client announces itself.

    urllib says "Python-urllib/3.x" by default, and providers behind Cloudflare
    fingerprint that and refuse outright — Groq answers 403 "error code: 1010",
    which reads exactly like a bad API key and is nothing of the sort.
    """

    def _headers_for(self, provider, extra=None):
        from fantasybot import config
        from fantasybot.llm import client as llm_client

        saved = (config.LLM_PROVIDER, config.LLM_API_KEY, config.LLM_MODEL)
        config.LLM_PROVIDER, config.LLM_API_KEY = provider, "k"
        config.LLM_MODEL = "m"
        seen = {}

        class FakeResp:
            def read(self):
                return b'{"choices":[{"message":{"content":"ok"}}],' \
                       b'"content":[{"type":"text","text":"ok"}],' \
                       b'"candidates":[{"content":{"parts":[{"text":"ok"}]}}]}'
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            seen.update(req.headers)
            return FakeResp()

        try:
            with mock.patch.object(llm_client.urllib.request, "urlopen",
                                   fake_urlopen):
                llm_client.complete("s", "u")
        finally:
            config.LLM_PROVIDER, config.LLM_API_KEY, config.LLM_MODEL = saved
        # urllib title-cases header names on the Request object.
        return {k.lower(): v for k, v in seen.items()}

    def test_it_does_not_announce_itself_as_urllib(self):
        for provider in ("groq", "openai", "anthropic", "gemini"):
            ua = self._headers_for(provider).get("user-agent", "")
            self.assertIn("fantasybot", ua, f"{provider} sent {ua!r}")
            self.assertNotIn("urllib", ua.lower())

    def test_the_api_key_still_goes_where_each_provider_expects_it(self):
        self.assertIn("authorization", self._headers_for("groq"))
        self.assertIn("x-api-key", self._headers_for("anthropic"))


class ErrorsExplainThemselves(unittest.TestCase):
    """A provider's HTTP status is not a diagnosis. These translate."""

    def _raise(self, code, body):
        import urllib.error
        from fantasybot.llm import client as llm_client

        err = urllib.error.HTTPError("u", code, "e", {}, None)
        err.read = lambda: body.encode()
        with mock.patch.object(llm_client.urllib.request, "urlopen",
                               side_effect=err):
            with self.assertRaises(llm_client.LLMError) as ctx:
                llm_client._post("https://x", {}, {}, 5)
        return str(ctx.exception)

    def test_cloudflare_1010_is_not_reported_as_a_bad_key(self):
        msg = self._raise(403, '{"error":"error code: 1010"}')
        self.assertIn("Cloudflare", msg)
        self.assertIn("no a la clave", msg)

    def test_a_real_401_points_at_the_key(self):
        self.assertIn("LLM_API_KEY", self._raise(401, "unauthorized"))

    def test_a_404_points_at_the_model(self):
        self.assertIn("LLM_MODEL", self._raise(404, "model not found"))

    def test_a_429_says_it_is_a_rate_limit(self):
        self.assertIn("límite", self._raise(429, "slow down"))
