"""One alert per problem, not one per review.

The squad was short a keeper for an afternoon, the review runs on a clock, and
the phone got nine copies of "Falta un POR: pujando por P. Campos hasta …" with
a different number each time. An alert you swipe away without reading is worse
than no alert: it trains you to ignore the one that matters.
"""

from datetime import timedelta
from unittest import mock

from fantasybot import notify
from fantasybot.storage import get_storage, to_iso, utcnow
from tests.support import StorageTestCase


class GapAlertsAreDaily(StorageTestCase):
    def test_the_same_gap_speaks_once_and_then_stays_quiet(self):
        sent = []
        with mock.patch.object(notify, "enabled", return_value=True), \
             mock.patch.object(notify.config, "TELEGRAM_BOT_TOKEN", "t"), \
             mock.patch.object(notify.config, "TELEGRAM_CHAT_ID", "c"), \
             mock.patch.object(notify, "_telegram",
                               side_effect=lambda text: sent.append(text) or True):
            for amount in (720_455, 721_724, 722_359, 723_629, 634_762):
                notify.send("gap:POR:2026-09-13",
                            f"Me falta POR (1 de 2): pujando hasta {amount:,} €")
        self.assertEqual(len(sent), 1,
                         "five reviews in an afternoon, one message")

    def test_a_different_position_is_a_different_subject(self):
        sent = []
        with mock.patch.object(notify, "enabled", return_value=True), \
             mock.patch.object(notify.config, "TELEGRAM_BOT_TOKEN", "t"), \
             mock.patch.object(notify.config, "TELEGRAM_CHAT_ID", "c"), \
             mock.patch.object(notify, "_telegram",
                               side_effect=lambda text: sent.append(text) or True):
            notify.send("gap:POR:2026-09-13", "falta POR")
            notify.send("gap:DEF:2026-09-13", "falta DEF")
        self.assertEqual(len(sent), 2,
                         "muting a keeper must not mute a defender")

    def test_it_speaks_again_the_next_day(self):
        store = get_storage()
        store.put_doc("notify_seen",
                      {"gap:POR:2026-09-12": to_iso(utcnow() - timedelta(days=1,
                                                                        hours=2))})
        self.assertFalse(notify._muted("gap:POR:2026-09-12"))

    def test_the_cooldown_is_a_full_day(self):
        store = get_storage()
        store.put_doc("notify_seen",
                      {"gap:POR:2026-09-13": to_iso(utcnow() - timedelta(hours=3))})
        self.assertTrue(notify._muted("gap:POR:2026-09-13"),
                        "three hours is still the same afternoon")
