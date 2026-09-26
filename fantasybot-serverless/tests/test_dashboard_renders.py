"""The page itself, actually rendered.

Seven hundred and seventy-nine tests covered the bot and not one covered the
thing the user looks at. So `bfClause` — an undeclared variable left behind when
the bidding funnel was rewritten — shipped, and the dashboard showed one line:

    Error: Can't find variable: bfClause · ⟳ 5 min

Not a broken section. The whole page, because every section ran as one
straight-line block and the first throw took the rest with it.

These tests run the real script out of `public/index.html` in node, against a
stub DOM, and call `render()` for real. An undeclared name, a missing helper, a
field the API stopped sending — anything that throws — fails here instead of on
the phone.

The payloads are the shapes that matter, and the third one is this bug: a funnel
present with zero bids scheduled, which is the only path that reached the line.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "public" / "index.html"
HARNESS = Path(__file__).parent / "page" / "render_harness.js"


def _funnel(clause=3):
    return {"anuncios de LaLiga": 5,
            "de otros managers (van por cláusula)": clause,
            "no me alcanza": 1, "suman muy poco": 1, "valen la pena": 0,
            "mejor gana": 0.4, "hace falta ganar": 0.25}


@unittest.skipIf(shutil.which("node") is None, "node not available")
class TheDashboardRenders(unittest.TestCase):
    def _render(self, payload):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as fh:
            json.dump(payload, fh)
            path = fh.name
        try:
            done = subprocess.run(
                ["node", str(HARNESS), str(PAGE), path],
                capture_output=True, text=True, timeout=60)
        finally:
            Path(path).unlink(missing_ok=True)
        if done.returncode != 0:
            self.fail((done.stderr or done.stdout).strip())

    def test_nothing_at_all_is_not_a_crash(self):
        """First load, before any tick has run."""
        self._render({"ok": True, "now": "2026-09-18T10:00:00+00:00"})

    def test_a_funnel_with_no_bids_scheduled(self):
        """The exact shape that blanked the page.

        `bfClause` sat on the branch taken only when a funnel exists AND
        nothing was scheduled — so it rendered fine until the first review that
        found nothing worth bidding on.
        """
        self._render({"ok": True, "now": "2026-09-18T10:00:00+00:00",
                      "report": {"bids": {"funnel": _funnel(), "scheduled": [],
                                          "budget": 44_000_000,
                                          "skipped": [{"nombre": "X",
                                                       "reason": "no es del sistema"}]}}})

    def test_a_funnel_with_bids_scheduled(self):
        self._render({"ok": True, "now": "2026-09-18T10:00:00+00:00",
                      "report": {"bids": {
                          "funnel": _funnel(0), "budget": 44_000_000,
                          "scheduled": [{"market_id": "m1", "nombre": "A",
                                         "amount": 5_000_000}]}}})

    def test_the_mode_picker_with_no_mode_in_the_report(self):
        """An older tick's report carries no mode; the picker still draws."""
        self._render({"ok": True, "now": "2026-09-18T10:00:00+00:00",
                      "report": {"money": 1}})

    def test_the_sell_health_box_with_no_offers_channel(self):
        """The suspicious case: listed, no bids, and no offer key anywhere."""
        self._render({"ok": True, "now": "2026-09-22T10:00:00+00:00",
                      "report": {"listing_shape": {
                          "anuncios_nuestros": 5, "con_ofertas": 0,
                          "ofertas_totales": 0, "claves_de_oferta_vistas": [],
                          "claves_de_la_fila": ["id"]}}})

    def test_the_sell_health_box_with_nothing_listed(self):
        self._render({"ok": True, "now": "2026-09-22T10:00:00+00:00",
                      "report": {"listing_shape": {
                          "anuncios_nuestros": 0, "con_ofertas": 0,
                          "ofertas_totales": 0, "claves_de_oferta_vistas": [],
                          "claves_de_la_fila": []}}})

    def test_the_money_box_in_debt_with_credit(self):
        """Negative bank before a gameweek, credit in use, offers held."""
        self._render({
            "ok": True, "now": "2026-09-26T10:00:00+00:00",
            "offers": {"status": "ok", "pressure": "urgente",
                       "money_before": -3_000_000, "money_after": 1_200_000,
                       "accepted": [{"nombre": "A", "amount": 4_200_000,
                                     "why": "sale", "de_laliga": True}],
                       "held": [{"nombre": "B", "amount": 900_000,
                                 "why": "en pie", "de_laliga": True}]},
            "report": {"finance": {
                "cash": -3_000_000, "credit_line": 48_000_000,
                "credit": 20_000_000, "committed": 5_000_000,
                "spend_cash": 0, "spend_total": 12_000_000,
                "hours_to_gameweek": 41.5, "why_no_credit": None,
                "jornada": {"at": "2026-09-28T19:00:00+00:00",
                            "source": "calendario"},
                "ofertas_vistas": {"at": "2026-09-26T09:00:00+00:00",
                                   "offers": 3},
                "recompensa_diaria": {"status": "claimed", "amount": 100_000}},
                "listing_shape": {"anuncios_nuestros": 20, "con_ofertas": 3,
                                  "ofertas_totales": 3,
                                  "ofertas_segun_laliga": 3,
                                  "lectura_de_ofertas": {"consultados": 3,
                                                         "ofertas": 3,
                                                         "errores": []},
                                  "claves_de_oferta_vistas": ["numberOfOffers"],
                                  "claves_de_la_fila": ["id"]}}})

    def test_the_money_box_without_credit(self):
        self._render({"ok": True, "now": "2026-09-26T10:00:00+00:00",
                      "report": {"finance": {
                          "cash": 1_260_777, "credit_line": 48_000_000,
                          "credit": 0, "committed": 0, "spend_cash": 1_260_777,
                          "spend_total": 1_260_777, "hours_to_gameweek": None,
                          "why_no_credit": "todavía no vi llegar ninguna oferta",
                          "jornada": {}, "ofertas_vistas": None,
                          "recompensa_diaria": None}}})

    def test_a_full_report(self):
        """Every section with something in it."""
        self._render({
            "ok": True, "now": "2026-09-18T10:00:00+00:00",
            "next_deadline": "2026-09-18T12:00:00+00:00",
            "pending_actions": [{"kind": "bid", "id": "b1"}],
            "degraded": [{"part": "executions"}],
            "last_execution": {"status": "ok",
                               "started_at": "2026-09-18T09:00:00+00:00",
                               "summary": {"ok": True, "duration_seconds": 12,
                                           "actions": []}},
            "offers": {"accepted": [{"nombre": "A", "amount": 1, "action": "accept",
                                     "why": "sube", "reason": "r",
                                     "de_laliga": True}],
                       "declined": [], "skipped": []},
            "report": {
                "money": 44_000_000, "xi_points": 51.2,
                "mode": {"mode": "dinero", "label": "Hacer caja",
                         "blurb": "b",
                         "knobs": {"min_gain": 0.0, "rank_by": "margin",
                                   "xi_premium": 1.0, "bench_premium": 0.5,
                                   "flip_target": 0.25, "bid_ceiling": 0.95,
                                   "clause_share": 0.30,
                                   "cash_floor": 5_000_000}},
                "market_census": {"total": 12, "system": 8},
                "listing_shape": {"anuncios_nuestros": 3, "con_ofertas": 1,
                                  "ofertas_totales": 2,
                                  "claves_de_oferta_vistas": ["offers"],
                                  "claves_de_la_fila": ["id", "salePrice"]},
                "market": [{"nombre": "A", "score": 70, "verdict": "comprar",
                            "headline": "h", "reasons": ["r"],
                            "ultimas_jornadas": [4, 6], "media_reciente": 5.0,
                            "rival_texto": "vs. último"}],
                "upgrades": [{"nombre": "A", "gain": 1.2, "buy_price": 5_000_000,
                              "gain_per_million": 0.24,
                              "gain_per_million_neto": 0.16,
                              "congela": 3_000_000,
                              "desplaza": {"nombre": "B", "value": 3_000_000},
                              "via": "SISTEMA"}],
                "rebuild": [{"net_gain": 3.9, "gain": 3.9, "loss": 0.0,
                             "spend": 22_000_000, "cash": 44_000_000,
                             "from_sales": 0, "affordable": True,
                             "left_over": 22_000_000, "why": "por qué",
                             "buy": [{"nombre": "Caro", "market_id": "m1",
                                      "price": 22_000_000}], "sell": []}],
                "leaks": [{"nombre": "D1", "line": "defender", "expected": 0.3,
                           "tag": "not_in_xi"}],
                "listings": {"listed": [
                    {"nombre": "C", "price": 4_600_000, "value": 5_000_000,
                     "sobra_en_su_posicion": True, "premium_pct": -8,
                     "days_listed": 3, "in_xi": False}]},
                "transfers": [{"buy": "A", "price": 1, "net": 1.0,
                               "left_over": 1, "sell": "B"}],
                "bids": {"funnel": _funnel(), "scheduled": [], "budget": 1,
                         "skipped": []},
                "defense": {"exposed": [{"nombre": "C", "clause": 1,
                                         "reason": "r"}], "raises": []},
                "rivals": [{"position": 1, "name": "R", "points": 10}],
                "ledger": [{"what": "sold", "nombre": "Z"}],
                "tasks": [], "sells": [], "clause_targets": [],
                "gaps": {}, "squad": {}, "flips": [],
            },
            "events": [{"kind": "review", "at": "2026-09-18T09:00:00+00:00",
                        "text": "t"}],
        })


if __name__ == "__main__":
    unittest.main()
