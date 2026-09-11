import unittest
from fantasybot.strategy.history import compute_manager_trading_history


class TestTradingHistory(unittest.TestCase):

    def test_compute_manager_trading_history_flips(self):
        player_names = {
            "101": {"name": "Player A", "pos": "DEL"},
            "102": {"name": "Player B", "pos": "MED"},
        }
        activity = [
            # Buy Player A for 10M
            {"activityTypeId": 31, "id": "1", "user1Id": 10, "playerMasterId": 101, "amount": 10_000_000, "createdAt": "2026-08-08T10:00:00+02:00"},
            # Sell Player A for 12M (+2M / +20%)
            {"activityTypeId": 33, "id": "2", "user1Id": 10, "playerMasterId": 101, "amount": 12_000_000, "createdAt": "2026-08-10T10:00:00+02:00"},
            # Direct buyout: user 10 buys Player B from user 20 for 5M
            {"activityTypeId": 1, "id": "3", "user1Id": 10, "user2Id": 20, "playerMasterId": 102, "amount": 5_000_000, "createdAt": "2026-08-11T10:00:00+02:00"},
            # User 10 sells Player B for 4M (-1M / -20%)
            {"activityTypeId": 33, "id": "4", "user1Id": 10, "playerMasterId": 102, "amount": 4_000_000, "createdAt": "2026-08-13T10:00:00+02:00"},
            # Initial squad sale: User 10 sells Player C (never bought)
            {"activityTypeId": 33, "id": "5", "user1Id": 10, "playerMasterId": 103, "amount": 3_000_000, "createdAt": "2026-08-09T10:00:00+02:00"},
        ]

        stats = compute_manager_trading_history(activity, 10, player_names)
        self.assertEqual(stats["total_trades"], 2)
        self.assertEqual(stats["winning_trades"], 1)
        self.assertEqual(stats["losing_trades"], 1)
        self.assertAlmostEqual(stats["win_rate_pct"], 50.0)
        self.assertEqual(stats["realized_profit"], 1_000_000)  # +2M - 1M
        self.assertAlmostEqual(stats["avg_roi_pct"], 0.0)  # (+20% + -20%) / 2

        self.assertEqual(len(stats["completed_flips"]), 2)
        flip_a = next(f for f in stats["completed_flips"] if f["pid"] == "101")
        self.assertEqual(flip_a["profit"], 2_000_000)
        self.assertAlmostEqual(flip_a["roi_pct"], 20.0)
        self.assertEqual(flip_a["holding_days"], 2)

        self.assertEqual(len(stats["initial_sales"]), 1)
        self.assertEqual(stats["initial_sales"][0]["pid"], "103")
        self.assertEqual(stats["initial_sales"][0]["sell_price"], 3_000_000)


if __name__ == "__main__":
    unittest.main()
