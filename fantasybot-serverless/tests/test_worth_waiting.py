"""Wait five days for his clause, or sign the best man available today?

The unlock instant is LaLiga's, not ours — the bot cannot pay one second early,
so "wait or not" was never the question. The question is whether to hold the
money for the better player who becomes reachable on Friday, or spend it now on
the best thing actually for sale.

Nobody was asking it. A clause was queued for its unlock and that ended the
thought, even with a gameweek falling in between. Points dropped in a gameweek
you played with a worse eleven are not refunded when the signing lands.
"""

from datetime import datetime, timedelta, timezone

from fantasybot.strategy import timing
from tests.support import StorageTestCase

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def _in(days):
    return (NOW + timedelta(days=days)).isoformat()


class HowManyGameweeksWaitingCosts(StorageTestCase):
    def test_unlocking_before_kickoff_costs_nothing(self):
        self.assertEqual(
            timing.gameweeks_missed(_in(1), _in(2), now=NOW), 0)

    def test_unlocking_just_after_kickoff_costs_one(self):
        self.assertEqual(
            timing.gameweeks_missed(_in(5), _in(2), now=NOW), 1)

    def test_a_fortnight_costs_two(self):
        self.assertEqual(
            timing.gameweeks_missed(_in(14), _in(2), now=NOW), 2)

    def test_without_a_kickoff_it_falls_back_to_whole_weeks(self):
        self.assertEqual(timing.gameweeks_missed(_in(9), None, now=NOW), 1)

    def test_an_unreadable_unlock_costs_nothing_rather_than_guessing(self):
        self.assertEqual(timing.gameweeks_missed("nope", _in(2), now=NOW), 0)


class TheDecision(StorageTestCase):
    def test_a_clearly_better_player_is_worth_a_missed_gameweek(self):
        got = timing.worth_waiting(gain_later=5.0, gain_now=1.0,
                                   unlock_iso=_in(5), next_kickoff_iso=_in(2),
                                   now=NOW)
        self.assertTrue(got["wait"])
        self.assertEqual(got["gameweeks_missed"], 1)

    def test_a_marginally_better_player_is_not(self):
        """This is the case that used to queue anyway."""
        got = timing.worth_waiting(gain_later=2.2, gain_now=2.0,
                                   unlock_iso=_in(5), next_kickoff_iso=_in(2),
                                   now=NOW)
        self.assertFalse(got["wait"])
        self.assertIn("No lo espero", got["why"])

    def test_nothing_available_today_means_waiting_always_wins(self):
        got = timing.worth_waiting(gain_later=1.0, gain_now=0.0,
                                   unlock_iso=_in(20), next_kickoff_iso=_in(2),
                                   now=NOW)
        self.assertTrue(got["wait"])

    def test_a_clause_that_opens_before_the_gameweek_costs_nothing(self):
        got = timing.worth_waiting(gain_later=2.0, gain_now=1.9,
                                   unlock_iso=_in(1), next_kickoff_iso=_in(2),
                                   now=NOW)
        self.assertTrue(got["wait"])
        self.assertEqual(got["cost_of_waiting"], 0.0)
        self.assertIn("no me pierdo", got["why"])

    def test_the_verdict_carries_both_totals(self):
        """It has to be arguable on a page, not just true."""
        got = timing.worth_waiting(3.0, 2.0, _in(5), _in(2), now=NOW)
        self.assertEqual(got["waiting_total"], 12.0)   # 3.0 x 4 playable weeks
        self.assertEqual(got["acting_total"], 10.0)    # 2.0 x 5 weeks
        self.assertEqual(got["cost_of_waiting"], 3.0)

    def test_a_long_wait_beats_nothing_but_loses_to_something_decent(self):
        far = {"unlock_iso": _in(25), "next_kickoff_iso": _in(2), "now": NOW}
        self.assertFalse(timing.worth_waiting(4.0, 2.0, **far)["wait"])
        self.assertTrue(timing.worth_waiting(4.0, 0.2, **far)["wait"])
