import unittest
from datetime import datetime, timezone

from reddit_bet_lab.models import RawSource
from reddit_bet_lab.parser import decimal_odds, parse_source


UTC = timezone.utc


def source(body: str, title: str = "Daily Picks Thread", subreddit: str = "SoccerBetting") -> RawSource:
    now = datetime.now(UTC)
    return RawSource(
        reddit_id="t1_test", subreddit=subreddit, submission_id="abc",
        source_type="comment", parent_title=title, author="tester",
        permalink="https://reddit.test", body=body, created_at=now,
        collected_at=now,
    )


class OddsConversionTests(unittest.TestCase):
    def test_decimal_american_and_fractional(self):
        self.assertEqual(decimal_odds("1.80"), 1.8)
        self.assertEqual(decimal_odds("+150"), 2.5)
        self.assertAlmostEqual(decimal_odds("-110"), 1.909091)
        self.assertEqual(decimal_odds("5/2"), 3.5)


class ParserTests(unittest.TestCase):
    def test_structured_single(self):
        outcome = parse_source(source(
            "Event: France vs Morocco\nPick: Over 1.5 Goals\nOdds: 1.62\nStake: 5 units"
        ))
        self.assertIsNone(outcome.rejection_reason)
        self.assertEqual(len(outcome.candidates), 1)
        candidate = outcome.candidates[0]
        self.assertEqual(candidate.bet_type, "SINGLE")
        self.assertEqual(candidate.quoted_odds_decimal, 1.62)
        self.assertEqual(candidate.legs[0].market_type, "total")
        self.assertEqual(candidate.legs[0].line_value, 1.5)
        self.assertEqual(candidate.legs[0].event_text, "France vs Morocco")

    def test_independent_parlay(self):
        outcome = parse_source(source(
            "PARLAY @ 3.20\nFrance vs Spain - France ML\nEngland vs Argentina - Over 2.5 goals"
        ))
        candidate = outcome.candidates[0]
        self.assertEqual(candidate.bet_type, "PARLAY")
        self.assertEqual(candidate.quoted_odds_decimal, 3.2)
        self.assertEqual(len(candidate.legs), 2)
        self.assertEqual(candidate.legs[0].event_text, "France vs Spain")
        self.assertEqual(candidate.legs[1].event_text, "England vs Argentina")

    def test_current_section_excludes_prior_results(self):
        body = (
            "Recent Picks ✅ Egypt vs Iran Pick: Over 1.5 Goals @ 1.67 "
            "❌ Brazil vs Japan Pick: BTTS @ 2.20 "
            "Current POTD #22 France vs Morocco Bet Builder "
            "✅ Over 1.5 Goals ✅ France Over 4.5 Shots on Target Odds: 1.62"
        )
        candidate = parse_source(source(body)).candidates[0]
        self.assertEqual(candidate.bet_type, "PARLAY")
        self.assertEqual(candidate.quoted_odds_decimal, 1.62)
        self.assertEqual(len(candidate.legs), 2)
        self.assertTrue(all("Egypt" not in leg.raw_text for leg in candidate.legs))
        self.assertTrue(all(leg.event_text == "France vs Morocco" for leg in candidate.legs))

    def test_past_win_post_is_rejected(self):
        outcome = parse_source(source("Craziest hit of my life ✅ won 10 units yesterday"))
        self.assertEqual(outcome.rejection_reason, "past_result_or_win_post")

    def test_multiple_independently_priced_picks_become_singles(self):
        outcome = parse_source(source(
            "Pick: Arsenal ML @ 1.80\nPick: Barcelona ML @ 1.65"
        ))
        self.assertEqual(len(outcome.candidates), 2)
        self.assertTrue(all(candidate.bet_type == "SINGLE" for candidate in outcome.candidates))

    def test_hyphen_event_and_unlabeled_moneyline_price(self):
        outcome = parse_source(source("Arsenal - Chelsea: Arsenal ML 1.80"))
        candidate = outcome.candidates[0]
        self.assertEqual(candidate.quoted_odds_decimal, 1.8)
        self.assertEqual(candidate.legs[0].event_text, "Arsenal vs Chelsea")

    def test_compact_short_total_is_classified_in_correct_direction(self):
        outcome = parse_source(source(
            "Nuggets vs Suns - o220.5 @ +100", title="NBA Daily Picks", subreddit="sportsbook"
        ))
        candidate = outcome.candidates[0]
        self.assertEqual(candidate.quoted_odds_decimal, 2.0)
        self.assertEqual(candidate.legs[0].market_type, "total")
        self.assertEqual(candidate.legs[0].side, "Over")
        self.assertEqual(candidate.legs[0].line_value, 220.5)

    def test_combined_parlay_price_is_not_copied_to_each_leg(self):
        outcome = parse_source(source(
            "France vs Spain Bet Builder Pick: France ML + Over 1.5 goals Total Odds: 2.40"
        ))
        candidate = outcome.candidates[0]
        self.assertEqual(candidate.bet_type, "PARLAY")
        self.assertEqual(candidate.quoted_odds_decimal, 2.4)
        self.assertTrue(all(leg.quoted_odds_decimal is None for leg in candidate.legs))


if __name__ == "__main__":
    unittest.main()
