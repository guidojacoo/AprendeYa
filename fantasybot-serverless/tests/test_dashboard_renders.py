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
                                     "why": "sube", "reason": "r"}],
                       "declined": [], "skipped": []},
            "report": {
                "money": 44_000_000, "xi_points": 51.2,
                "market_census": {"total": 12, "system": 8},
                "market": [{"nombre": "A", "score": 70, "verdict": "comprar",
                            "headline": "h", "reasons": ["r"],
                            "ultimas_jornadas": [4, 6], "media_reciente": 5.0,
                            "rival_texto": "vs. último"}],
                "upgrades": [{"nombre": "A", "gain": 1.2, "buy_price": 5_000_000,
                              "gain_per_million": 0.24, "via": "SISTEMA"}],
                "rebuild": [{"net_gain": 3.9, "gain": 3.9, "loss": 0.0,
                             "spend": 22_000_000, "cash": 44_000_000,
                             "from_sales": 0, "affordable": True,
                             "left_over": 22_000_000, "why": "por qué",
                             "buy": [{"nombre": "Caro", "market_id": "m1",
                                      "price": 22_000_000}], "sell": []}],
                "leaks": [{"nombre": "D1", "line": "defender", "expected": 0.3,
                           "tag": "not_in_xi"}],
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
