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


def _rival(balance, **kw):
    row = {"estimated_balance": balance, "is_me": False,
           "partial_history": False, "manager_name": "Rival"}
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
                               ({"rivals": [_rival(0)]}, "estimar"),
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
