import unittest
from fantasybot.strategy import scouting


class TestScoutingModule(unittest.TestCase):

    def test_scouting_star_player(self):
        pm = {
            "id": "100",
            "nickname": "Pedri",
            "name": "Pedro González",
            "positionId": 3,
            "team": {"name": "Barcelona"},
            "marketValue": 65_000_000,
            "points": 25,
            "averagePoints": 8.3,
            "lastSeasonPoints": 235,
            "playerStatus": "ok"
        }
        prob_index = {
            "pedri": {"nombre": "pedri", "prob": 90, "lesionado": False, "sancionado": False, "disponible": True}
        }
        report = scouting.analyze_player_profile(pm, prob_index=prob_index)

        self.assertEqual(report["name"], "Pedri")
        self.assertEqual(report["last_season_points"], 235)
        self.assertIn("Estrella Top", report["tier_badge"])
        self.assertIn("Titular Indiscutible", report["starter_status"])
        self.assertTrue(report["is_available"])
        self.assertIn("MUY RECOMENDABLE", report["verdict"])

    def test_scouting_lost_role_warning(self):
        pm = {
            "id": "101",
            "nickname": "Veterano",
            "name": "Jugador Veterano",
            "positionId": 2,
            "team": {"name": "Sevilla"},
            "marketValue": 10_000_000,
            "points": 2,
            "averagePoints": 1.0,
            "lastSeasonPoints": 175,
            "playerStatus": "ok"
        }
        prob_index = {
            "veterano": {"nombre": "veterano", "prob": 10, "lesionado": False, "sancionado": False, "disponible": True}
        }
        report = scouting.analyze_player_profile(pm, prob_index=prob_index)

        self.assertEqual(report["role_shift_level"], "WARNING")
        self.assertIn("Pérdida de Rol", report["role_shift"])

    def test_scouting_injured_player(self):
        pm = {
            "id": "102",
            "nickname": "Baja",
            "name": "Jugador Lesionado",
            "positionId": 4,
            "team": {"name": "Valencia"},
            "marketValue": 15_000_000,
            "points": 0,
            "averagePoints": 0.0,
            "lastSeasonPoints": 120,
            "playerStatus": "injured"
        }
        report = scouting.analyze_player_profile(pm, prob_index={})

        self.assertFalse(report["is_available"])
        self.assertIn("Lesionado", report["physical_status"])
        self.assertIn("NO RECOMENDABLE", report["verdict"])

    def test_search_player_in_list(self):
        catalog = [
            {"id": "1", "nickname": "Lamine Yamal", "name": "Lamine Yamal Nasraoui"},
            {"id": "2", "nickname": "Vinicius Jr", "name": "Vinicius Jose Paixao"},
            {"id": "3", "nickname": "A. Batalla", "name": "Augusto Batalla"},
        ]

        m1 = scouting.search_player_in_list("1", catalog)
        self.assertIsNotNone(m1)
        self.assertEqual(m1["nickname"], "Lamine Yamal")

        m2 = scouting.search_player_in_list("yamal", catalog)
        self.assertIsNotNone(m2)
        self.assertEqual(m2["id"], "1")

        m3 = scouting.search_player_in_list("batalla", catalog)
        self.assertIsNotNone(m3)
        self.assertEqual(m3["id"], "3")

        m4 = scouting.search_player_in_list("desconocido_xyz", catalog)
        self.assertIsNone(m4)

    def test_team_scouting_analysis(self):
        team_data = {
            "name": "Super FC",
            "teamValue": 100_000_000,
            "teamMoney": 5_000_000,
            "players": [
                {"playerMaster": {"id": "1", "nickname": "Courtois", "positionId": 1, "marketValue": 30_000_000, "lastSeasonPoints": 160, "playerStatus": "ok"}},
                {"playerMaster": {"id": "2", "nickname": "Rudiger", "positionId": 2, "marketValue": 25_000_000, "lastSeasonPoints": 180, "playerStatus": "ok"}},
                {"playerMaster": {"id": "3", "nickname": "Bellingham", "positionId": 3, "marketValue": 45_000_000, "lastSeasonPoints": 240, "playerStatus": "injured"}},
            ]
        }
        # prob_index={} keeps the test offline; without it analyze_team_squad falls
        # through to a live FutbolFantasy scrape (network- and cache-dependent).
        ts = scouting.analyze_team_squad(team_data, prob_index={})
        self.assertEqual(ts["total_players"], 3)
        self.assertEqual(ts["total_last_pts"], 580)
        self.assertEqual(len(ts["stars"]), 3)
        self.assertEqual(len(ts["injured_or_suspended"]), 1)


if __name__ == "__main__":
    unittest.main()
