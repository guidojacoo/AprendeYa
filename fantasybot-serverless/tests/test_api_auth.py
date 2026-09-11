"""Nobody gets to run the bot but the scheduler.

/api/tick spends real money and /api/status hands over your squad, your balance
and your bidding plan. These endpoints are on the public internet, so the auth
check is load-bearing — especially the "unset secret fails closed" case, which is
the difference between a misconfigured deploy and an open one.
"""

import unittest
from unittest import mock

from fantasybot import config
from fantasybot.serverless import http


class FakeHandler:
    def __init__(self, headers=None, path="/api/tick"):
        self.headers = headers or {}
        self.path = path


class Authorization(unittest.TestCase):
    def setUp(self):
        self._saved = (config.BOT_CRON_SECRET, config.VERCEL_CRON_SECRET)
        config.BOT_CRON_SECRET = "s3cr3t"
        config.VERCEL_CRON_SECRET = None
        self.addCleanup(self._restore)

    def _restore(self):
        config.BOT_CRON_SECRET, config.VERCEL_CRON_SECRET = self._saved

    def test_bearer_token(self):
        self.assertTrue(http.authorized(
            FakeHandler({"Authorization": "Bearer s3cr3t"})))

    def test_bearer_is_case_insensitive_on_the_scheme(self):
        self.assertTrue(http.authorized(
            FakeHandler({"Authorization": "bearer s3cr3t"})))

    def test_cron_header(self):
        self.assertTrue(http.authorized(FakeHandler({"X-Cron-Secret": "s3cr3t"})))

    def test_query_token(self):
        self.assertTrue(http.authorized(
            FakeHandler(path="/api/tick?token=s3cr3t")))

    def test_wrong_secret(self):
        self.assertFalse(http.authorized(
            FakeHandler({"Authorization": "Bearer nope"})))

    def test_no_credential_at_all(self):
        self.assertFalse(http.authorized(FakeHandler()))

    def test_empty_credential(self):
        self.assertFalse(http.authorized(FakeHandler({"X-Cron-Secret": ""})))

    def test_unset_secret_fails_closed(self):
        """A deployment that forgot BOT_CRON_SECRET must reject EVERYONE, not
        accept everyone. This is the one that would hurt."""
        config.BOT_CRON_SECRET = None
        self.assertFalse(http.authorized(FakeHandler()))
        self.assertFalse(http.authorized(
            FakeHandler({"Authorization": "Bearer anything"})))
        self.assertFalse(http.authorized(FakeHandler({"X-Cron-Secret": ""})))

    def test_vercel_cron_secret_is_also_accepted(self):
        config.BOT_CRON_SECRET = "github-one"
        config.VERCEL_CRON_SECRET = "vercel-one"
        self.assertTrue(http.authorized(
            FakeHandler({"Authorization": "Bearer vercel-one"})))
        self.assertTrue(http.authorized(
            FakeHandler({"Authorization": "Bearer github-one"})))
        self.assertFalse(http.authorized(
            FakeHandler({"Authorization": "Bearer neither"})))

    def test_a_prefix_of_the_secret_is_not_enough(self):
        self.assertFalse(http.authorized(
            FakeHandler({"Authorization": "Bearer s3cr"})))
        self.assertFalse(http.authorized(
            FakeHandler({"Authorization": "Bearer s3cr3t-extra"})))


class GuardedEndpoint(unittest.TestCase):
    """`guarded` is what every function actually calls: deny first, then never
    let an exception escape as HTML."""

    def setUp(self):
        self._saved = config.BOT_CRON_SECRET
        config.BOT_CRON_SECRET = "s3cr3t"
        self.addCleanup(lambda: setattr(config, "BOT_CRON_SECRET", self._saved))

    def test_unauthorized_never_runs_the_handler(self):
        ran = []
        with mock.patch.object(http, "send") as send:
            http.guarded(FakeHandler(), lambda: ran.append(1) or (200, {}))
        self.assertEqual(ran, [], "the work must not happen before the check")
        self.assertEqual(send.call_args[0][1], 401)

    def test_an_exception_becomes_a_json_500(self):
        def boom():
            raise ValueError("kaboom")
        with mock.patch.object(http, "send") as send:
            http.guarded(FakeHandler({"X-Cron-Secret": "s3cr3t"}), boom)
        status, payload = send.call_args[0][1], send.call_args[0][2]
        self.assertEqual(status, 500)
        self.assertFalse(payload["ok"])
        self.assertIn("kaboom", payload["error"])

    def test_success_passes_the_payload_through(self):
        with mock.patch.object(http, "send") as send:
            http.guarded(FakeHandler({"X-Cron-Secret": "s3cr3t"}),
                         lambda: (200, {"ok": True, "hi": 1}))
        self.assertEqual(send.call_args[0][1], 200)
        self.assertEqual(send.call_args[0][2]["hi"], 1)
