"""LaLiga sends numbers as strings on some endpoints, and a dict keyed by int
misses every one of them in silence.

This is not hypothetical. A squad with three goalkeepers counted zero of them:
every line read as short, the bot bid for a keeper nobody needed, and the
optimiser announced "No goalkeeper in the squad" with three sitting in it.
`{1: "POR"}.get("1")` is None, and nothing anywhere raised.
"""

import unittest

from fantasybot.matching import position_id, position_of
from fantasybot.strategy import needs
from fantasybot.strategy.lineup import optimize


def player(pid, position, value=5_000_000):
    return {"playerTeamId": pid,
            "playerMaster": {"id": f"m{pid}", "nickname": pid, "name": pid,
                             "positionId": position, "marketValue": value,
                             "playerStatus": "ok"}}


def squad(position_ids):
    """A full, legal squad whose positionIds are given in LaLiga's own types."""
    gk, df, md, fw = position_ids
    players = [player(f"g{i}", gk) for i in range(3)]
    players += [player(f"d{i}", df) for i in range(5)]
    players += [player(f"m{i}", md) for i in range(5)]
    players += [player(f"f{i}", fw) for i in range(3)]
    return {"teamMoney": 10_000_000, "players": players}


class PositionIds(unittest.TestCase):
    def test_a_string_reads_the_same_as_an_int(self):
        self.assertEqual(position_id({"positionId": "1"}), 1)
        self.assertEqual(position_of({"positionId": "1"}), "POR")
        self.assertEqual(position_of({"positionId": 1}), "POR")

    def test_nonsense_is_no_position_never_a_wrong_one(self):
        self.assertEqual(position_id({"positionId": "portero"}), 0)
        self.assertEqual(position_of({"positionId": None}, "?"), "?")
        self.assertEqual(position_of({}, "?"), "?")


class ASquadWhoseIdsAreStrings(unittest.TestCase):
    def test_three_keepers_count_as_three(self):
        self.assertEqual(needs.squad_counts(squad(("1", "2", "3", "4")))["POR"], 3)

    def test_and_therefore_the_squad_has_no_gaps(self):
        """The bug's real cost: a phantom gap made it bid for a keeper it
        already had three of."""
        self.assertEqual(needs.gaps(squad(("1", "2", "3", "4"))), {})

    def test_the_xi_can_be_built(self):
        best = optimize(squad(("1", "2", "3", "4")), prob_index={})
        self.assertTrue(best["payload"]["goalkeeper"])
        self.assertFalse(best.get("incomplete"))

    def test_ints_still_work_exactly_as_before(self):
        self.assertEqual(needs.gaps(squad((1, 2, 3, 4))), {})
        self.assertTrue(optimize(squad((1, 2, 3, 4)),
                        prob_index={})["payload"]["goalkeeper"])


class WhenThereReallyIsNoKeeper(unittest.TestCase):
    def test_the_error_says_what_it_counted(self):
        """A verdict with no evidence behind it is what made this take a day to
        find: the message was true of the parsed data and false of the world."""
        outfield = {"teamMoney": 0, "players": [player(f"d{i}", 2) for i in range(11)]}
        with self.assertRaises(ValueError) as caught:
            optimize(outfield, prob_index={})
        self.assertIn("11 players", str(caught.exception))
        self.assertIn("positionId values seen", str(caught.exception))
