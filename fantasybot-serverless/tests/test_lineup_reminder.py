"""The "set your lineup" reminder must appear ONLY on the day the matchday's first
match is played — not days early during the odd early-season gameweeks.

Bug: a user saw "matchday starts, set your lineup" for a jornada that didn't begin
until 3 days later, because the reminder was created for ANY upcoming kickoff.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

os.environ.setdefault("FANTASYBOT_HOME", tempfile.mkdtemp(prefix="fb-lr-test-"))

from fantasybot import agent  # noqa: E402
from fantasybot.sources import matchday  # noqa: E402

TZ = matchday.SPAIN_TZ


class LineupLockReminder(unittest.TestCase):
    def test_none_when_matchday_is_days_away(self):
        kickoff = (datetime.now(TZ) + timedelta(days=3)).replace(microsecond=0).isoformat()
        self.assertIsNone(agent.lineup_lock_reminder(kickoff))

    def test_present_on_match_day(self):
        # first match is today (in Spain time) -> the notice should show
        kickoff = datetime.now(TZ).replace(hour=21, minute=0, second=0,
                                           microsecond=0).isoformat()
        rem = agent.lineup_lock_reminder(kickoff)
        self.assertIsNotNone(rem)
        self.assertIn("LINEUP", rem["message"])
        self.assertEqual(rem["event_at"], kickoff)

    def test_none_when_no_kickoff(self):
        self.assertIsNone(agent.lineup_lock_reminder(None))

    def test_yesterday_kickoff_not_shown(self):
        kickoff = (datetime.now(TZ) - timedelta(days=1)).replace(microsecond=0).isoformat()
        self.assertIsNone(agent.lineup_lock_reminder(kickoff))


class NextGameweekStart(unittest.TestCase):
    """The balance/lineup deadline is the first match of the next jornada that hasn't
    started — NOT the next match on the calendar. Jornadas can be spread over many days
    and even overlap (a postponed jornada-1 match next week while jornada 2 has begun)."""

    def test_picks_next_unstarted_jornada(self):
        from datetime import timezone
        now = datetime(2026, 8, 17, tzinfo=timezone.utc)  # mid-jornada 1
        first = {
            1: datetime(2026, 8, 15, tzinfo=timezone.utc),  # already started
            2: datetime(2026, 8, 22, tzinfo=timezone.utc),  # the next to start
            3: datetime(2026, 8, 29, tzinfo=timezone.utc),
        }
        self.assertEqual(matchday._pick_next_gameweek_start(first, now),
                         datetime(2026, 8, 22, tzinfo=timezone.utc))

    def test_ignores_current_jornadas_later_matches(self):
        from datetime import timezone
        now = datetime(2026, 8, 17, tzinfo=timezone.utc)
        # jornada 1 first match is in the past even though it has matches on the 27th
        first = {1: datetime(2026, 8, 15, tzinfo=timezone.utc),
                 2: datetime(2026, 8, 22, tzinfo=timezone.utc)}
        self.assertEqual(matchday._pick_next_gameweek_start(first, now),
                         datetime(2026, 8, 22, tzinfo=timezone.utc))

    def test_none_when_all_gameweeks_have_started(self):
        from datetime import timezone
        now = datetime(2026, 8, 30, tzinfo=timezone.utc)
        first = {1: datetime(2026, 8, 15, tzinfo=timezone.utc)}
        self.assertIsNone(matchday._pick_next_gameweek_start(first, now))


if __name__ == "__main__":
    unittest.main()
