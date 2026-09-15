"""Telling "did not look" apart from "looked and nobody was for sale".

The page said "Todavía no analizó el mercado — esperá a la primera revisión"
while the bot had reviewed, had its whole squad listed, and had simply found
nothing to buy. Two completely different statements, one message, and the one
shown was the alarming one — and false.

The squad stands permanently on the market, so most of what comes back is OURS.
The number that decides whether there was anything to buy is how many listings
belong to somebody else.
"""

from fantasybot import agent
from tests.support import StorageTestCase


def _row(pid, mine=False):
    return {"id": f"m{pid}",
            "discr": "marketPlayerTeam" if mine else "marketPlayerLeague",
            "playerMaster": {"id": pid, "nickname": f"J{pid}",
                             "positionId": "3", "marketValue": "1000000"}}


class WhatTheMarketHeld(StorageTestCase):
    def test_a_market_of_only_our_own_players_has_nothing_to_buy(self):
        market = [_row(str(i), mine=True) for i in range(17)]
        census = {"anuncios": len(market),
                  "mios": sum(1 for el in market
                              if str(el["playerMaster"]["id"]) in
                              {str(i) for i in range(17)}),
                  "ajenos": 0}
        self.assertEqual(census["anuncios"], 17)
        self.assertEqual(census["mios"], 17)
        self.assertEqual(census["ajenos"], 0,
                         "17 listings and nothing to buy is not a broken review")

    def test_the_census_travels_with_the_report(self):
        """It has to reach the page, or the page goes back to guessing."""
        import inspect
        src = inspect.getsource(agent)
        self.assertIn('"market_census"', src)
        for key in ("anuncios", "mios", "ajenos"):
            self.assertIn(key, src)

    def test_the_summary_carries_it_to_the_dashboard(self):
        import inspect

        from fantasybot import tick
        self.assertIn('"market_census"', inspect.getsource(tick._summarize))
