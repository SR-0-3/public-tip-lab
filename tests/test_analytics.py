import unittest

from reddit_bet_lab.analytics import bankroll_curve, summary_metrics


ROWS = [
    {"id": 1, "placed_at": "x", "settled_at": "1", "status": "WON", "stake_units": 1, "profit_units": 1, "quoted_odds_decimal": 2},
    {"id": 2, "placed_at": "x", "settled_at": "2", "status": "LOST", "stake_units": 1, "profit_units": -1, "quoted_odds_decimal": 2},
    {"id": 3, "placed_at": "x", "settled_at": "3", "status": "WON", "stake_units": 1, "profit_units": 0.5, "quoted_odds_decimal": 1.5},
    {"id": 4, "placed_at": "x", "settled_at": "4", "status": "LOST", "stake_units": 1, "profit_units": -1, "quoted_odds_decimal": 3},
    {"id": 5, "placed_at": "x", "settled_at": "5", "status": "PUSH", "stake_units": 1, "profit_units": 0, "quoted_odds_decimal": 2},
    {"id": 6, "placed_at": "x", "settled_at": None, "status": "OPEN", "stake_units": 1, "profit_units": None, "quoted_odds_decimal": 2},
    {"id": 7, "placed_at": "x", "settled_at": None, "status": "NEEDS_SETTLEMENT", "stake_units": 1, "profit_units": None, "quoted_odds_decimal": 2},
]


class AnalyticsTests(unittest.TestCase):
    def test_summary(self):
        result = summary_metrics(ROWS, 100)
        self.assertEqual(result["settled"], 5)
        self.assertEqual(result["open"], 2)
        self.assertAlmostEqual(result["profit_units"], -0.5)
        self.assertAlmostEqual(result["roi"], -0.1)
        self.assertEqual(result["available_bank"], 97.5)
        self.assertIsNotNone(result["roi_ci"])

    def test_bankroll_curve(self):
        curve = bankroll_curve(ROWS, 100)
        self.assertEqual(curve[0]["balance"], 100)
        self.assertEqual(curve[-1]["balance"], 99.5)


if __name__ == "__main__":
    unittest.main()
