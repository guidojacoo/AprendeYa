"""Three reasons nothing sold, and they used to render identically.

Reported live: "y no vendió a nadie."

The offer handler found no offers and said nothing, which reads the same
whether nothing of ours was on the market, our listings were up and nobody bid,
or offers were arriving somewhere the handler never looks. Only the last is a
bug, and there was no way to tell.

The handler assumes every bid travels embedded in the market row, under `offers`
or `offer`. Nothing ever checked that against a real payload — and this project
has already lost a whole feature to exactly that (numbers arriving as strings,
weekPoints arriving as a dict). If LaLiga puts bids elsewhere, every listing
reads as "no offers" forever, in silence, which is what a season of never
selling looks like.
"""

from fantasybot.strategy import offers
from tests.support import StorageTestCase


def _mine(pid, offers_payload=None, key="offers"):
    row = {"discr": "marketPlayerTeam", "id": f"mk-{pid}",
           "playerMaster": {"id": pid, "nickname": pid},
           "salePrice": "5000000", "expirationDate": "2026-09-23T10:00:00"}
    if offers_payload is not None:
        row[key] = offers_payload
    return row


RESERVES = {"m1": 5_000_000, "m2": 5_000_000}


class ItSaysWhichOfTheThreeItIs(StorageTestCase):
    def test_nothing_of_ours_is_on_the_market(self):
        got = offers.inspect_our_listings([], reserves=RESERVES)
        self.assertEqual(got["anuncios_nuestros"], 0)

    def test_listed_but_nobody_bid(self):
        got = offers.inspect_our_listings(
            [_mine("m1"), _mine("m2")], reserves=RESERVES)
        self.assertEqual(got["anuncios_nuestros"], 2)
        self.assertEqual(got["ofertas_totales"], 0)
        self.assertEqual(got["claves_de_oferta_vistas"], [],
                         "no offer key at all is the suspicious case")

    def test_offers_arriving_and_being_read(self):
        got = offers.inspect_our_listings(
            [_mine("m1", [{"id": "o1", "money": 6_000_000}]), _mine("m2")],
            reserves=RESERVES)
        self.assertEqual(got["con_ofertas"], 1)
        self.assertEqual(got["ofertas_totales"], 1)
        self.assertEqual(got["claves_de_oferta_vistas"], ["offers"])

    def test_an_offer_key_present_but_empty_still_proves_the_channel(self):
        """The distinction the diagnostic exists for.

        `offers: []` means LaLiga does send bids this way and there are none —
        a quiet market. NO offers key on any row means they travel somewhere
        else, and the handler has been reading a place bids never arrive.
        """
        got = offers.inspect_our_listings([_mine("m1", [])], reserves=RESERVES)
        self.assertEqual(got["ofertas_totales"], 0)
        self.assertEqual(got["claves_de_oferta_vistas"], ["offers"])

    def test_the_singular_shape_counts_too(self):
        got = offers.inspect_our_listings(
            [_mine("m1", {"id": "o1", "money": 1}, key="offer")],
            reserves=RESERVES)
        self.assertEqual(got["ofertas_totales"], 1)
        self.assertEqual(got["claves_de_oferta_vistas"], ["offer"])

    def test_an_unknown_offer_key_is_still_reported(self):
        """If bids arrive under a name we do not parse, name it anyway.

        This is the row that would have explained a silent season.
        """
        got = offers.inspect_our_listings(
            [_mine("m1", [{"id": "o1", "money": 1}], key="offersReceived")],
            reserves=RESERVES)
        self.assertEqual(got["ofertas_totales"], 0, "we do not parse it")
        self.assertEqual(got["claves_de_oferta_vistas"], ["offersReceived"],
                         "but we SAY we saw it")

    def test_a_rivals_listing_is_not_counted_as_ours(self):
        got = offers.inspect_our_listings(
            [_mine("m1"), _mine("ajeno")], reserves=RESERVES)
        self.assertEqual(got["anuncios_nuestros"], 1)

    def test_a_laliga_listing_is_not_ours_either(self):
        got = offers.inspect_our_listings(
            [{"discr": "marketPlayerLeague", "playerMaster": {"id": "m1"}}],
            reserves=RESERVES)
        self.assertEqual(got["anuncios_nuestros"], 0)

    def test_it_records_the_keys_the_row_carries(self):
        """So the next wrong assumption is answerable without a deploy."""
        got = offers.inspect_our_listings([_mine("m1")], reserves=RESERVES)
        self.assertIn("salePrice", got["claves_de_la_fila"])
        self.assertIn("expirationDate", got["claves_de_la_fila"])

    def test_junk_is_not_a_crash(self):
        self.assertEqual(
            offers.inspect_our_listings(None)["anuncios_nuestros"], 0)
        self.assertEqual(
            offers.inspect_our_listings([{}, None if False else {"discr": "x"}],
                                        reserves=RESERVES)["anuncios_nuestros"],
            0)


if __name__ == "__main__":
    import unittest
    unittest.main()
