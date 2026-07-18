import unittest

from reddit_bet_lab.settlement import settle_leg_from_score


EVENT = {
    "id": "e1", "completed": True, "home_team": "France", "away_team": "Spain",
    "scores": [{"name": "France", "score": "2"}, {"name": "Spain", "score": "1"}],
}


class SettlementTests(unittest.TestCase):
    def test_moneyline(self):
        result = settle_leg_from_score(
            {"market_type": "h2h", "side": "France", "home_team": "France", "away_team": "Spain"}, EVENT
        )
        self.assertEqual(result.status, "WON")

    def test_total(self):
        result = settle_leg_from_score(
            {"market_type": "total", "side": "Over", "line_value": 2.5, "sport": "Soccer"}, EVENT
        )
        self.assertEqual(result.status, "WON")

    def test_btts(self):
        result = settle_leg_from_score(
            {"market_type": "btts", "side": "Yes"}, EVENT
        )
        self.assertEqual(result.status, "WON")

    def test_draw_no_bet_push(self):
        draw_event = dict(EVENT)
        draw_event["scores"] = [
            {"name": "France", "score": "1"}, {"name": "Spain", "score": "1"}
        ]
        result = settle_leg_from_score(
            {"market_type": "draw_no_bet", "side": "France"}, draw_event
        )
        self.assertEqual(result.status, "PUSH")

    def test_prop_requires_manual_settlement(self):
        result = settle_leg_from_score(
            {"market_type": "corners_total", "side": "Over", "line_value": 8.5}, EVENT
        )
        self.assertIsNone(result.status)


if __name__ == "__main__":
    unittest.main()

