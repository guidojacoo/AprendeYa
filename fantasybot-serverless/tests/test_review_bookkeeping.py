"""Which write goes first when a review finishes.

The page sat on yesterday's market, presented exactly like today's. The review
was stamping its clock BEFORE storing its report, so anything that threw in
between marked the hour as reviewed and left the old report standing. A market
that closes every day is worthless read from yesterday.

  report stored, clock not -> the next tick reviews again. Harmless.
  clock stored, report not -> blind for an hour, and lying about it.
"""

import inspect

from fantasybot import tick
from tests.support import StorageTestCase


class TheReportIsWrittenBeforeTheClock(StorageTestCase):
    def test_the_order_is_report_then_clock(self):
        src = inspect.getsource(tick.run_review)
        report_at = src.index('put_doc("last_report"')
        clock_at = src.index('put_doc("last_review_at", to_iso(now))')
        self.assertLess(report_at, clock_at,
                        "stamping the clock first buys an hour of stale data "
                        "every time the report write fails")

    def test_both_writes_still_happen(self):
        src = inspect.getsource(tick.run_review)
        self.assertIn('put_doc("last_report"', src)
        self.assertIn('put_doc("last_review_at"', src)
