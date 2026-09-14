"""How much the richest rival could actually pay — and the key that says so.

This number gates two things: how high a bid may chase a contested listing, and
which of our players are worth defending with a clause raise. Both read it from
the review's `rivals` rows, where the field is `estimated_balance`.

Both read `cash` instead, which is not a name the rivals analysis produces
anywhere. `cash` exists only in the DASHBOARD payload, renamed on the way out —
so the planners were reading the shape of the page instead of the shape of the
report, and got 0 every time. A wrong key fails exactly like a poor league: the
bid cap never capped anything and the defence found nobody exposed, both while
reporting perfectly normal numbers. Nothing caught it because the tests had
copied the same invented key.
"""

from fantasybot import tick
from tests.support import StorageTestCase


def _rival(balance, spent=None, squad=0, **kw):
    """A rival row. `spent` is the biggest transfer they have actually made —
    the observed fact the reach now leads with; it defaults to something
    comfortably above the estimate so tests about the ESTIMATE are not
    accidentally testing the headroom clamp."""
    row = {"estimated_balance": balance, "is_me": False,
           "partial_history": False, "manager_name": "Rival",
           "team_value": squad,
           "max_purchase": balance if spent is None else spent}
    row.update(kw)
    return row


class ReachReadsTheProducersKey(StorageTestCase):
    def test_the_richest_rival_wins(self):
        got, why = tick._rival_reach({"rivals": [
            _rival(3_000_000), _rival(8_400_000), _rival(1_000_000)]})
        self.assertEqual(got, 8_400_000)
        self.assertIsNone(why)

    def test_the_dashboards_key_is_not_the_reports_key(self):
        """The bug, pinned. A row carrying only `cash` is a row we cannot read,
        and it has to say so rather than quietly answer zero."""
        got, why = tick._rival_reach({"rivals": [
            {"cash": 8_400_000, "is_me": False, "partial_history": False}]})
        self.assertEqual(got, 0)
        self.assertIsNotNone(why, "an unreadable estimate must explain itself")

    def test_our_own_balance_is_not_the_competition(self):
        got, _ = tick._rival_reach({"rivals": [
            _rival(50_000_000, is_me=True), _rival(2_000_000)]})
        self.assertEqual(got, 2_000_000)

    def test_a_half_read_history_is_not_a_ceiling(self):
        got, why = tick._rival_reach({"rivals": [
            _rival(8_400_000, partial_history=True)]})
        self.assertEqual(got, 0)
        self.assertIn("reconstruyendo", why)

    def test_every_zero_carries_its_reason(self):
        """Silence is what let this run for a day. Each way of reaching zero
        has to name itself so the next one is visible the first time."""
        for report, expect in (({"rivals": []}, "actividad"),
                               ({"rivals": [_rival(0, spent=0)]}, "gastar"),
                               ({"rivals": [_rival(9, is_me=True)]}, "rivales")):
            got, why = tick._rival_reach(report)
            self.assertEqual(got, 0)
            self.assertIn(expect, why)

    def test_a_string_balance_is_still_a_number(self):
        """LaLiga sends numbers as strings on half its endpoints."""
        got, _ = tick._rival_reach({"rivals": [_rival("8400000")]})
        self.assertEqual(got, 8_400_000)


class TheKeyMatchesWhatTheAnalysisActuallyEmits(StorageTestCase):
    def test_rivals_analysis_emits_estimated_balance(self):
        """The test that would have caught it: read the producer, not the page."""
        import inspect

        from fantasybot.strategy import rivals as rivals_mod
        src = inspect.getsource(rivals_mod)
        self.assertIn('"estimated_balance"', src)
        self.assertNotIn('"cash":', src,
                         "if the analysis ever emits `cash`, this test is the "
                         "place to find out which name won")


class AnEstimateThatDoesNotAddUpIsNotACeiling(StorageTestCase):
    """The estimate is `initial + sales - purchases + prizes`, so a PURCHASE the
    history has not reached yet is money never subtracted — the error only runs
    UPWARD. Unbounded it produced a rival holding 159 MILLION from a 15M start,
    which marked a whole squad as reachable and would have had the defence
    raising every clause in it to a number nobody could pay.

    And the MAXIMUM of a noisy estimator picks whichever manager's history is
    most incomplete, which is the opposite of what a ceiling is for."""

    def test_a_suspect_estimate_does_not_top_the_list(self):
        got, why = tick._rival_reach({"rivals": [
            _rival(159_647_614, spent=3_000_000, estimate_suspect=True),
            _rival(6_200_000, spent=3_500_000)]})
        self.assertEqual(got, 6_200_000)
        self.assertIsNone(why)

    def test_a_suspect_estimate_takes_its_manager_out_of_the_reckoning(self):
        got, why = tick._rival_reach({"rivals": [
            _rival(159_647_614, spent=6_000_000, estimate_suspect=True),
            _rival(7_000_000, spent=5_000_000)]})
        self.assertEqual(got, 7_000_000)
        self.assertIsNone(why)

    def test_the_estimate_is_bounded_by_what_could_exist(self):
        """Starting money, plus everything they could have sold, plus winnings."""
        from fantasybot.strategy.rivals import bound_balance
        est, raw, cap, suspect = bound_balance(
            initial_cash=15_000_000, net_profit=200_000_000,
            squad_value=40_000_000, prizes=0)
        self.assertTrue(suspect, "215M from a 15M start does not add up")
        self.assertEqual(raw, 215_000_000, "the raw figure stays diagnosable")
        self.assertEqual(cap, 55_000_000)
        self.assertEqual(est, cap)

    def test_an_ordinary_estimate_is_left_alone(self):
        from fantasybot.strategy.rivals import bound_balance
        est, _, _, suspect = bound_balance(15_000_000, -8_000_000, 40_000_000)
        self.assertEqual(est, 7_000_000)
        self.assertFalse(suspect)

    def test_a_real_negative_survives_but_an_invented_one_does_not(self):
        """LaLiga lets a balance go negative, but only to -10% of squad value."""
        from fantasybot.strategy.rivals import bound_balance
        est, _, _, _ = bound_balance(15_000_000, -20_000_000, 40_000_000)
        self.assertEqual(est, -4_000_000)


class WhatTheLeagueHasActuallyPaid(StorageTestCase):
    """The estimate did not survive contact with the data: one manager came out
    at 159M holding the SMALLEST squad in the league, while a rival with three
    times his squad estimated at 4.9M. The ordering correlates with nothing,
    because the error is unread purchases and that varies per manager instead of
    cancelling out.

    The biggest transfer a rival has actually completed is a price somebody
    paid, in the league's own feed. It cannot be wrong, only stale — and stale
    in the safe direction, since it grows the moment anyone spends more."""

    def test_demonstrated_spending_sets_the_floor(self):
        got, _ = tick._rival_reach({"rivals": [
            _rival(3_000_000, spent=9_000_000)]})
        self.assertGreaterEqual(got, 9_000_000,
                                "somebody paid this; it is not a guess")

    def test_an_estimate_may_raise_the_bar_within_reach_of_the_evidence(self):
        got, _ = tick._rival_reach({"rivals": [
            _rival(14_000_000, spent=9_000_000)]})
        self.assertEqual(got, 14_000_000)

    def test_it_never_goes_far_past_what_anyone_has_spent(self):
        got, _ = tick._rival_reach({"rivals": [
            _rival(40_000_000, spent=9_000_000)]})
        self.assertEqual(got, 18_000_000)


class TwoReadingsThatDisagreeAreNotAnAnswer(StorageTestCase):
    """They measure the same thing from the same feed, so a wide gap is not
    caution versus boldness -- it is a feed being misread. The live one gives
    exactly that: a manager estimated at 159M who has never been recorded buying
    anything, next to two different managers whose largest purchase is the same
    141,030,000 to the euro."""

    def test_a_wild_disagreement_spends_nothing(self):
        got, why = tick._rival_reach({"rivals": [
            _rival(159_647_614, spent=9_000_000)]})
        self.assertEqual(got, 0)
        self.assertIn("no entiendo", why)

    def test_readings_that_roughly_agree_are_believed(self):
        got, why = tick._rival_reach({"rivals": [
            _rival(11_000_000, spent=9_000_000)]})
        self.assertEqual(got, 11_000_000)
        self.assertIsNone(why)

    def test_a_manager_we_cannot_read_does_not_veto_one_we_can(self):
        """The aggregate check compared maxima from different people; this is
        the case it got wrong."""
        got, why = tick._rival_reach({"rivals": [
            _rival(159_647_614, spent=0, squad=92_861_661),
            _rival(63_707_634, spent=55_490_510, squad=226_654_714)]})
        self.assertEqual(got, 63_707_634)
        self.assertIsNone(why)

    def test_week_one_believes_the_starting_budget(self):
        """No squad and no purchases is not a gap in the history — it is a
        league that has not started trading."""
        got, why = tick._rival_reach({"rivals": [
            _rival(6_000_000, spent=0, squad=0)]})
        self.assertEqual(got, 6_000_000)
        self.assertIsNone(why)

    def test_a_squad_with_no_purchases_behind_it_is_a_missing_history(self):
        """Nobody assembles ninety-three million of footballers for free. This
        is mercho40, estimated at 159M with nothing ever recorded."""
        got, why = tick._rival_reach({"rivals": [
            _rival(159_647_614, spent=0, squad=92_861_661)]})
        self.assertEqual(got, 0)
        self.assertIn("no entiendo", why)
