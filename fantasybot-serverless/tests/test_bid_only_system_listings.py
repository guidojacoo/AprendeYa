"""Two doors in, and bidding against another manager is not one of them.

    LaLiga's own listing  ->  bid on it
    another manager's man ->  pay his buyout clause

The bid planner was taking both. Worse, a rival's row is priced at his CLAUSE in
`flip.evaluate`, so every one of those "bids" offered a ~1.67x premium for a
player the clause pipeline was already tracking at the same number.
"""

from datetime import timedelta

from fantasybot import config, tick
from fantasybot.scheduler import TickContext
from fantasybot.storage import to_iso, utcnow
from tests.support import StorageTestCase


class _Client:
    def market(self, lid):
        return []

    def default_ids(self):
        return "L1", "T1"


def _upgrade(mid, via, nombre):
    return {"market_id": mid, "via": via, "nombre": nombre,
            "player_id": f"p{mid}", "buy_price": 5_000_000,
            "expires_at": to_iso(utcnow() + timedelta(hours=3)),
            "valor_actual": 5_000_000, "gain": 3.0, "gain_per_million": 0.6,
            "affordable": True, "pos": "DEL"}


class OnlyLaLigasOwnListingsAreBidOn(StorageTestCase):
    def setUp(self):
        super().setUp()
        self._flags = (config.AUTO_BIDS, config.AUTO_EXECUTE)
        config.AUTO_BIDS = config.AUTO_EXECUTE = True
        self.addCleanup(lambda: setattr(config, "AUTO_BIDS", self._flags[0]))
        self.addCleanup(lambda: setattr(config, "AUTO_EXECUTE", self._flags[1]))

    def _plan(self, upgrades):
        ctx = TickContext(budget_seconds=20, log=lambda m: None)
        return tick._plan_bids(ctx, _Client(), "L1",
                               {"teamMoney": 50_000_000},
                               {"upgrades": upgrades, "flips": [], "rivals": []})

    def test_a_system_listing_is_scheduled(self):
        got = self._plan([_upgrade("m1", "SISTEMA", "Libre")])
        self.assertEqual(len(got["scheduled"]), 1)
        self.assertEqual(got["scheduled"][0]["nombre"], "Libre")

    def test_a_rivals_listing_is_refused_and_says_why(self):
        got = self._plan([_upgrade("m2", "CLAUSULA", "DeOtro")])
        self.assertEqual(got["scheduled"], [])
        self.assertEqual(len(got["skipped"]), 1)
        self.assertIn("cláusula", got["skipped"][0]["reason"])

    def test_a_mixed_market_keeps_only_the_free_agents(self):
        got = self._plan([_upgrade("m1", "SISTEMA", "Libre"),
                          _upgrade("m2", "CLAUSULA", "DeOtro"),
                          _upgrade("m3", "SISTEMA", "OtroLibre")])
        self.assertEqual(sorted(r["nombre"] for r in got["scheduled"]),
                         ["Libre", "OtroLibre"])

    def test_an_unlabelled_row_is_refused_rather_than_assumed(self):
        """A row whose route we cannot read is not a free agent by default."""
        row = _upgrade("m4", "SISTEMA", "Raro")
        del row["via"]
        self.assertEqual(self._plan([row])["scheduled"], [])
