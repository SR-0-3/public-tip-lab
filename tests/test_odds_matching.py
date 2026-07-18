import unittest
from datetime import timedelta
from types import SimpleNamespace

from reddit_bet_lab.models import MarketPrice, ProviderEvent, utc_now
from reddit_bet_lab.odds_client import OddsClient


class FakeOddsClient(OddsClient):
    def __init__(self, event, prices):
        self.settings = SimpleNamespace(
            minimum_lead_minutes=2,
            max_event_horizon_days=7,
            event_match_threshold=78,
            odds_relative_tolerance=.35,
            odds_region="uk",
        )
        self.event = event
        self.prices = prices
        self._sports = None
        self._events_cache = {}
        self.last_quota = {}

    def sports(self):
        return [{
            "key": "soccer_test", "group": "Soccer", "title": "Test League",
            "description": "Test League", "active": True,
        }]

    def events(self, sport_key):
        return [self.event]

    def event_prices(self, event, market_key):
        return self.prices


class OddsMatchingTests(unittest.TestCase):
    def setUp(self):
        self.now = utc_now()
        self.event = ProviderEvent(
            event_id="e1", sport_key="soccer_test", sport_title="Test League",
            commence_time=self.now + timedelta(hours=4),
            home_team="France", away_team="Spain",
        )
        self.prices = [
            MarketPrice("Book A", "h2h", "France", 1.80),
            MarketPrice("Book B", "h2h", "France", 1.90),
            MarketPrice("Book A", "h2h", "Spain", 2.40),
        ]

    def leg(self):
        return {
            "sport": "Soccer", "league": "Test League",
            "home_team_hint": "France", "away_team_hint": "Spain",
            "market_type": "h2h", "side": "France", "selection": "France ML",
            "line_value": None,
        }

    def test_future_event_market_and_price_match(self):
        client = FakeOddsClient(self.event, self.prices)
        result = client.validate_leg(
            self.leg(), collected_at=self.now, quoted_odds_decimal=1.85
        )
        self.assertEqual(result.status, "ODDS_MATCH")
        self.assertEqual(result.matched_event.event_id, "e1")
        self.assertAlmostEqual(result.verified_price, 1.85)

    def test_implausible_quote_is_not_auto_verified(self):
        client = FakeOddsClient(self.event, self.prices)
        result = client.validate_leg(
            self.leg(), collected_at=self.now, quoted_odds_decimal=5.00
        )
        self.assertEqual(result.status, "MARKET_MATCH")

    def test_started_event_cannot_match(self):
        past_event = ProviderEvent(
            event_id="past", sport_key="soccer_test", sport_title="Test League",
            commence_time=self.now - timedelta(minutes=1),
            home_team="France", away_team="Spain",
        )
        client = FakeOddsClient(past_event, self.prices)
        result = client.validate_leg(
            self.leg(), collected_at=self.now, quoted_odds_decimal=1.85
        )
        self.assertEqual(result.status, "UNVERIFIED")

    def test_common_team_aliases_still_require_the_two_team_event(self):
        event = ProviderEvent(
            event_id="e2", sport_key="soccer_test", sport_title="Test League",
            commence_time=self.now + timedelta(hours=4),
            home_team="Manchester United", away_team="Tottenham Hotspur",
        )
        prices = [
            MarketPrice("Book A", "h2h", "Manchester United", 1.90),
            MarketPrice("Book B", "h2h", "Manchester United", 2.00),
        ]
        client = FakeOddsClient(event, prices)
        leg = self.leg()
        leg.update({
            "home_team_hint": "Man Utd", "away_team_hint": "Spurs",
            "side": "Man Utd", "selection": "Man Utd ML",
        })
        result = client.validate_leg(leg, collected_at=self.now, quoted_odds_decimal=1.95)
        self.assertEqual(result.status, "ODDS_MATCH")
        self.assertEqual(result.matched_event.event_id, "e2")

    def test_same_event_market_is_fetched_once_per_validation_run(self):
        client = OddsClient.__new__(OddsClient)
        client.settings = SimpleNamespace(odds_region="uk")
        client._price_cache = {}
        calls = []

        def fake_get(path, params):
            calls.append((path, params))
            return {
                "bookmakers": [{
                    "key": "book_a", "title": "Book A",
                    "markets": [{
                        "key": "h2h",
                        "outcomes": [{"name": "France", "price": 1.8}],
                    }],
                }]
            }

        client._get = fake_get
        first = client.event_prices(self.event, "h2h")
        second = client.event_prices(self.event, "h2h")
        self.assertIs(first, second)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
