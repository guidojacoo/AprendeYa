"""Lineup optimizer: it must always send a COMPLETE, POSITION-VALID XI.

LaLiga's rule (confirmed live): a player fielded OUT of position is silently dropped
(a midfielder sent as a defender comes off the lineup, leaving the slot empty), but a
player who is injured/suspended yet IN position is KEPT — he just scores 0. So the
optimizer must fill each line from its OWN players, using a line's injured players to
backfill that same line rather than borrowing a healthy player from another line.

Regression for the reported bug: a thin, injury-hit defence made the optimizer plug
defender slots with midfielders; LaLiga dropped them and the '11' it applied reached
the pitch as 9. The fix keeps every slot position-valid.

Run with:  python -m unittest discover -s tests
"""

import unittest

from fantasybot.strategy.lineup import optimize, payload_ids


def _player(pid, position_id, value, status="ok", last=0, name=None):
    """A squad member in the shape `optimize` expects (team['players'][i])."""
    return {
        "playerTeamId": pid,
        "playerMaster": {
            "id": f"m{pid}",
            "nickname": name or pid,
            "name": name or pid,
            "positionId": position_id,   # 1 GK, 2 DEF, 3 MED, 4 DEL
            "marketValue": value,
            "playerStatus": status,      # anything != "ok" => unavailable
            "lastSeasonPoints": last,
        },
    }


def _available_ids(best):
    """playerTeamIds that ended up in the XI and are marked available."""
    ok = set()
    for slot in ("goalkeeper", "defender", "midfield", "striker"):
        entries = best[slot]
        entries = entries if isinstance(entries, list) else [entries]
        for e in entries:
            if e["disponible"]:
                ok.add(e["playerTeamId"])
    return ok


def _slot_counts(best):
    d, m, f = best["formation"]
    p = best["payload"]
    return {
        "goalkeeper": 1 if p["goalkeeper"] else 0,
        "defender": len(p["defender"]),
        "midfield": len(p["midfield"]),
        "striker": len(p["striker"]),
    }, (1, d, m, f)


class TestLineupNeverEmptyNeverUnavailable(unittest.TestCase):
    def test_forced_defender_crisis_stays_position_valid(self):
        """Only 1 available defender, plenty of available midfielders.

        Every legal formation needs >= 3 defenders. The squad owns exactly 3 defenders,
        two of them injured. The optimizer must field those injured defenders in the
        defender slots (LaLiga keeps them, they score 0) and NOT plug the holes with
        healthy midfielders — a midfielder sent as a defender is dropped by LaLiga, so
        that 'trick' would reach the pitch as a 9-player XI. The extra midfielders stay
        on the bench.
        """
        players = [
            _player("gk1", 1, 5_000_000),
            _player("d1", 2, 8_000_000),
            _player("d2", 2, 8_000_000, status="injured"),
            _player("d3", 2, 8_000_000, status="injured"),
            _player("m1", 3, 8_000_000),
            _player("m2", 3, 8_000_000),
            _player("m3", 3, 8_000_000),
            _player("m4", 3, 8_000_000),
            _player("m5", 3, 8_000_000),
            _player("m6", 3, 8_000_000),
            _player("s1", 4, 20_000_000),
            _player("s2", 4, 15_000_000),
            _player("s3", 4, 10_000_000),
        ]
        best = optimize({"players": players}, prob_index={})

        counts, (g, d, m, f) = _slot_counts(best)
        # every slot in the chosen formation is filled (never empty)
        self.assertEqual(counts["goalkeeper"], g)
        self.assertEqual(counts["defender"], d)
        self.assertEqual(counts["midfield"], m)
        self.assertEqual(counts["striker"], f)

        # 11 fielded, and EVERY slot is position-valid (this is what LaLiga accepts)
        p = best["payload"]
        self.assertEqual(len(payload_ids(best)), 11)
        defenders, mids, strikers = {"d1", "d2", "d3"}, \
            {"m1", "m2", "m3", "m4", "m5", "m6"}, {"s1", "s2", "s3"}
        self.assertTrue(set(p["defender"]) <= defenders,
                        f"non-defenders in the defender line: "
                        f"{set(p['defender']) - defenders}")
        self.assertTrue(set(p["midfield"]) <= mids)
        self.assertTrue(set(p["striker"]) <= strikers)
        # the injured defenders ARE fielded (kept in position), not swapped for mids
        self.assertEqual(set(p["defender"]), {"d1", "d2", "d3"})

    def test_doubtful_is_fielded_over_injured(self):
        """A 'duda' plays more often than not, so for a contested slot it must be
        preferred over an injured player (who scores 0). Four forwards for three slots:
        two fit, one doubtful, one injured -> the injured one sits, the doubtful starts.
        """
        players = [_player("gk1", 1, 5_000_000)]
        players += [_player(f"d{i}", 2, 8_000_000) for i in range(1, 4)]   # 3 fit DEF
        players += [_player(f"m{i}", 3, 8_000_000) for i in range(1, 5)]   # 4 fit MED
        players += [_player("s1", 4, 20_000_000), _player("s2", 4, 18_000_000)]  # 2 fit DEL
        players += [_player("s_doubt", 4, 15_000_000, status="doubtful"),
                    _player("s_inj", 4, 15_000_000, status="injured")]
        best = optimize({"players": players}, prob_index={})
        strikers = set(best["payload"]["striker"])
        self.assertIn("s_doubt", strikers,
                      "a doubtful player must beat an injured one for the slot")
        self.assertNotIn("s_inj", strikers,
                         "an injured player must not be fielded when a doubtful one is free")

    def test_midfield_shortage_leaves_honest_hole_and_flags_incomplete(self):
        """Squad with only 2 midfielders — FEWER than any legal formation needs (min 3).

        LaLiga has NO formation with fewer than 3 midfielders (the 7 legal shapes all
        need >=3), so a 2-midfielder squad CANNOT field a complete XI. The optimizer must
        NOT paper over it by borrowing a non-midfielder into the empty slot — LaLiga
        silently drops any out-of-position player, so that 'patch' does nothing but lie
        about the lineup. Instead the optimizer must:
          - field ONLY the real midfielders it has (the midfield line stays SHORT),
          - pick the shape with the FEWEST empty holes (a 3-midfield formation: 1 hole),
          - flag the XI as incomplete and say which line is short, so the copy can raise
            it as an URGENT problem (sign a midfielder) instead of 'lineup applied'.
        This is the exact case a real user hit: a 3-5-2 (3 empty central slots) applied
        to a 2-midfielder squad.
        """
        players = [_player("gk1", 1, 5_000_000)]
        players += [_player(f"d{i}", 2, 8_000_000) for i in range(1, 6)]   # 5 DEF
        players += [_player("m1", 3, 6_000_000), _player("m2", 3, 6_000_000)]  # only 2 MED
        players += [_player(f"s{i}", 4, 15_000_000) for i in range(1, 6)]   # 5 DEL
        best = optimize({"players": players}, prob_index={})

        p = best["payload"]
        # NO out-of-position player is smuggled into midfield — only the 2 real ones
        self.assertEqual(set(p["midfield"]), {"m1", "m2"},
                         f"midfield must hold ONLY the real midfielders, got {p['midfield']}")
        # fewest holes: a 3-midfielder formation, leaving exactly 1 empty central slot
        self.assertEqual(best["formation"][1], 3,
                         f"expected a 3-midfielder formation (fewest holes), got {best['formation']}")
        # the XI is flagged incomplete, naming the short line so the copy can act on it
        self.assertTrue(best.get("incomplete"),
                        "a squad that can't fill a line must be flagged incomplete")
        self.assertEqual(best.get("missing", {}).get("midfield"), 1,
                         f"must report 1 missing midfielder, got {best.get('missing')}")
        # def/str lines ARE complete and position-valid (only midfield is short)
        self.assertEqual(len(p["defender"]), best["formation"][0])
        self.assertEqual(len(p["striker"]), best["formation"][2])

    def test_premium_league_completes_two_midfielder_squad_with_523(self):
        """In a PREMIUM league (config.premiumFeatures.formations = true) LaLiga unlocks the
        2-midfielder shapes (4-2-4, 5-2-3, 3-2-5). A squad with 2 MED / 5 DEF / 3 DEL can then
        field a COMPLETE 11 as a 5-2-3 — no holes — instead of leaving a midfield slot empty.
        Verified live against the API: LaLiga saved exactly this 5-2-3.
        """
        players = [_player("gk1", 1, 5_000_000)]
        players += [_player(f"d{i}", 2, 8_000_000) for i in range(1, 6)]   # 5 DEF
        players += [_player("m1", 3, 6_000_000), _player("m2", 3, 6_000_000)]  # 2 MED
        players += [_player(f"s{i}", 4, 12_000_000) for i in range(1, 4)]   # 3 DEL
        best = optimize({"players": players}, prob_index={}, premium=True)
        self.assertEqual(best["formation"], (5, 2, 3),
                         f"a premium league must complete the XI as 5-2-3, got {best['formation']}")
        self.assertFalse(best.get("incomplete"), "the 5-2-3 XI is complete — no holes")
        self.assertEqual(len(payload_ids(best)), 11)
        self.assertEqual(set(best["payload"]["midfield"]), {"m1", "m2"})

    def test_non_premium_league_leaves_hole_for_two_midfielder_squad(self):
        """The SAME squad in a NON-premium league (default) has no 2-midfielder shape
        available, so it stays incomplete — a 3-midfield formation with one honest hole."""
        players = [_player("gk1", 1, 5_000_000)]
        players += [_player(f"d{i}", 2, 8_000_000) for i in range(1, 6)]
        players += [_player("m1", 3, 6_000_000), _player("m2", 3, 6_000_000)]
        players += [_player(f"s{i}", 4, 12_000_000) for i in range(1, 4)]
        best = optimize({"players": players}, prob_index={})  # premium defaults to False
        self.assertEqual(best["formation"][1], 3,
                         f"non-premium must pick a 3-midfielder shape, got {best['formation']}")
        self.assertTrue(best.get("incomplete"))
        self.assertEqual(best.get("missing", {}).get("midfield"), 1)

    def test_complete_squad_is_not_flagged_incomplete(self):
        """A squad that CAN fill a formation must not be flagged incomplete (no false alarm)."""
        players = [_player("gk1", 1, 5_000_000)]
        players += [_player(f"d{i}", 2, 8_000_000) for i in range(1, 6)]
        players += [_player(f"m{i}", 3, 8_000_000) for i in range(1, 6)]
        players += [_player(f"s{i}", 4, 10_000_000) for i in range(1, 4)]
        best = optimize({"players": players}, prob_index={})
        self.assertFalse(best.get("incomplete"),
                         "a fillable squad must not be flagged incomplete")
        self.assertFalse(best.get("missing"),
                         f"a fillable squad has no missing slots, got {best.get('missing')}")
        self.assertEqual(len(payload_ids(best)), 11)

    def test_healthy_squad_unchanged(self):
        """A healthy squad still gets a full, all-available XI (no regression)."""
        players = [_player("gk1", 1, 5_000_000)]
        players += [_player(f"d{i}", 2, 8_000_000) for i in range(1, 6)]
        players += [_player(f"m{i}", 3, 8_000_000) for i in range(1, 6)]
        players += [_player(f"s{i}", 4, 10_000_000) for i in range(1, 4)]
        best = optimize({"players": players}, prob_index={})
        self.assertEqual(len(payload_ids(best)), 11)
        self.assertEqual(payload_ids(best), _available_ids(best))


if __name__ == "__main__":
    unittest.main()
