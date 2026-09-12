"""The reasoning shown next to every decision.

A decision you cannot audit is one you take on faith, and nobody leaves a bot
spending their money on faith. These sentences are built from the same numbers
the decision was built from, so they are true by construction — they cannot
describe a reason the bot did not actually have.

What is pinned here is mostly the failure mode: a missing number must drop out of
the sentence, never appear as "None" in front of the user.
"""

import unittest

from fantasybot import explain


class Money(unittest.TestCase):
    def test_it_reads_like_a_person_would_say_it(self):
        self.assertEqual(explain._m(5_200_000), "5.2M")
        self.assertEqual(explain._m(5_000_000), "5M")
        self.assertEqual(explain._m(850_000), "850k")
        self.assertEqual(explain._m(300), "300")

    def test_unknown_money_is_nothing_not_None(self):
        for bad in (None, "", "abc", [1]):
            self.assertIsNone(explain._m(bad), f"for {bad!r}")


class Bids(unittest.TestCase):
    FLIP = {"nombre": "Gabriel Jesus", "valor_actual": 5_000_000,
            "proyeccion": 5_800_000, "margin_pct": 11.2, "tendencia": 40,
            "oficial_trend_pct": 3.1}

    def test_it_names_the_player_the_cap_and_the_projection(self):
        got = explain.bid(self.FLIP, 5_200_000, 4_700_000)
        self.assertIn("Gabriel Jesus", got)
        self.assertIn("5.2M", got)
        self.assertIn("+11.2%", got)

    def test_it_says_when_the_cap_was_lowered_to_the_field(self):
        self.assertIn("nadie puede pagar más",
                      explain.bid(self.FLIP, 5_200_000, 4_700_000))
        self.assertNotIn("nadie puede pagar",
                         explain.bid(self.FLIP, 5_200_000, None))

    def test_a_falling_price_is_explained_not_hidden(self):
        got = explain.bid({**self.FLIP, "tendencia": -30}, 5_000_000)
        self.assertIn("bajando", got)

    def test_an_empty_flip_still_produces_a_sentence(self):
        got = explain.bid({}, 1_000_000)
        self.assertTrue(got.endswith("."))
        self.assertNotIn("None", got)


class GapSignings(unittest.TestCase):
    def test_it_says_why_profit_is_not_the_point(self):
        got = explain.gap_signing("POR", {"nombre": "Unai", "prob": 85},
                                  6_000_000, have=0, want=2)
        self.assertIn("No tengo ningún POR", got)
        self.assertIn("cuesta puntos", got)
        self.assertIn("85%", got)

    def test_a_squad_that_has_one_is_not_told_it_has_none(self):
        """A gap means "below the recommended minimum", and the minimum for
        keepers is two. Saying "no tengo ningún POR" over a squad with a
        goalkeeper reads as a counting bug — and sent a day into chasing one."""
        got = explain.gap_signing("POR", {"nombre": "Unai"}, 6_000_000,
                                  have=1, want=2)
        self.assertIn("Tengo 1 POR y quiero 2", got)
        self.assertNotIn("No tengo ningún", got)
        self.assertIn("recambio", got)

    def test_without_the_count_it_claims_nothing_it_cannot_back(self):
        got = explain.gap_signing("POR", {"nombre": "Unai"}, 6_000_000)
        self.assertIn("Me falta un POR", got)
        self.assertNotIn("No tengo ningún", got)

    def test_an_unknown_probability_is_simply_omitted(self):
        got = explain.gap_signing("DEF", {"nombre": "X"}, 1_000_000)
        self.assertNotIn("None", got)
        self.assertNotIn("%", got)


class Clauses(unittest.TestCase):
    def test_it_explains_the_timing(self):
        got = explain.clause({"nombre": "Baena", "pos": "MED", "prob": 78},
                             12_000_000)
        self.assertIn("12M", got)
        self.assertIn("MED", got)
        self.assertIn("primero que paga", got)


class Offers(unittest.TestCase):
    def test_accepting_quotes_the_reserve_it_beat(self):
        got = explain.offer({"action": "accept", "nombre": "X",
                             "amount": 15_000_000, "reserve": 14_000_000})
        self.assertIn("Acepto", got)
        self.assertIn("14M", got)

    def test_declining_says_how_short_it_fell(self):
        got = explain.offer({"action": "decline", "nombre": "Pedri",
                             "amount": 9_000_000, "reserve": 14_000_000,
                             "in_xi": True})
        self.assertIn("Rechazo", got)
        self.assertIn("titular", got)

    def test_being_outbid_is_a_different_reason(self):
        got = explain.offer({"action": "decline", "nombre": "X", "amount": 1,
                             "reserve": 2, "reason": "outbid by 9,000,000"})
        self.assertIn("otra oferta mejor", got)


class Listings(unittest.TestCase):
    def test_a_starter_is_explained_as_a_high_reserve(self):
        got = explain.listing({"nombre": "Sorloth", "price": 14_000_000,
                               "value": 10_000_000, "in_xi": True,
                               "premium_pct": 40})
        self.assertIn("titular", got)
        self.assertIn("no es venderlo", got)

    def test_a_stale_listing_explains_the_decay(self):
        got = explain.listing({"nombre": "X", "price": 1_000_000,
                               "value": 1_000_000, "premium_pct": 5,
                               "days_listed": 4})
        self.assertIn("4 días sin ofertas", got)


class Lineup(unittest.TestCase):
    def test_no_change_says_so_plainly(self):
        got = explain.lineup({"changed": False}, {"formation": (4, 3, 3)})
        self.assertIn("4-3-3", got)
        self.assertIn("ya era la mejor", got)

    def test_an_incomplete_xi_is_flagged_not_glossed(self):
        got = explain.lineup({"changed": True, "incomplete": True},
                             {"formation": (4, 4, 2)})
        self.assertIn("incompleta", got)

    def test_no_formation_at_all_is_explained(self):
        self.assertIn("falta cubrir", explain.lineup({}, {}))


class NeverLeaksNone(unittest.TestCase):
    def test_every_explainer_survives_empty_input(self):
        """A missing number must drop out of the sentence, never be printed."""
        for out in (explain.bid({}, None), explain.gap_signing("POR", {}, None),
                    explain.clause({}, None), explain.listing({}),
                    explain.offer({}), explain.shield({}),
                    explain.lineup({}, {})):
            self.assertNotIn("None", out)
            self.assertTrue(out.strip())
