"""Tests for the pure logic (no network): matching, bid decision, state.

Run with:  python -m unittest
"""

import unittest

from fantasybot.matching import normalize, index_by_name, match_name
from fantasybot.bidding import decide, UNCONTESTED_CUSHION
from fantasybot.state import diff_snapshots
from fantasybot.strategy.lineup import payload_ids


class TestMatching(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize("Étta Eyóng"), "etta eyong")
        self.assertEqual(normalize("  A. Carreras "), "a. carreras")
        self.assertEqual(normalize(None), "")

    def test_match_by_token(self):
        index = index_by_name([{"nombre": "karl etta eyong", "v": 1}])
        # the API nickname ("Etta Eyong") matches the full name via tokens
        self.assertEqual(match_name("Etta Eyong", "", index)["v"], 1)

    def test_match_exact_full_name(self):
        index = index_by_name([{"nombre": "robert lewandowski", "v": 9}])
        self.assertEqual(match_name("Lewy", "Robert Lewandowski", index)["v"], 9)

    def test_no_match(self):
        index = index_by_name([{"nombre": "vinicius junior", "v": 1}])
        self.assertIsNone(match_name("Mbappe", "Kylian Mbappe", index))


class TestBidDecision(unittest.TestCase):
    def test_wait_when_early_and_uncontested(self):
        self.assertIsNone(decide(1_000_000, 0, 300, 2_000_000))

    def test_last_second_uncontested(self):
        self.assertEqual(decide(1_000_000, 0, 10, 2_000_000),
                         1_000_000 + UNCONTESTED_CUSHION)

    def test_contested_adds_margin(self):
        self.assertEqual(decide(1_000_000, 2, 300, 2_000_000), 1_030_000)

    def test_never_exceeds_max(self):
        self.assertEqual(decide(1_000_000, 2, 300, 1_010_000), 1_010_000)


class TestState(unittest.TestCase):
    def test_first_run(self):
        d = diff_snapshots({}, {"money": 100, "squad": {"1": "A"}})
        self.assertTrue(d["first_run"])

    def test_diff(self):
        prev = {"money": 100, "squad": {"1": "A", "2": "B"}}
        curr = {"money": 150, "squad": {"2": "B", "3": "C"}}
        d = diff_snapshots(prev, curr)
        self.assertEqual(d["added"], ["C"])
        self.assertEqual(d["removed"], ["A"])
        self.assertEqual(d["money_delta"], 50)


class TestLineup(unittest.TestCase):
    def test_payload_ids(self):
        best = {"payload": {"goalkeeper": "g", "defender": ["d1", "d2"],
                            "midfield": ["m1"], "striker": ["s1"]}}
        self.assertEqual(payload_ids(best), {"g", "d1", "d2", "m1", "s1"})


if __name__ == "__main__":
    unittest.main()
