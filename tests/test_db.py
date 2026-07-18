import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from reddit_bet_lab.db import Database
from reddit_bet_lab.models import BetCandidate, LegCandidate, RawSource, iso_utc, utc_now


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "test.sqlite3")
        self.db.initialize(100)

    def tearDown(self):
        self.temp.cleanup()

    def make_source(self, body="Pick: France ML\nOdds: 1.80"):
        now = utc_now()
        return RawSource(
            reddit_id="t1_unique", subreddit="SoccerBetting", submission_id="s1",
            source_type="comment", parent_title="Daily Picks", author="tipster",
            permalink="https://reddit.test", body=body,
            created_at=now - timedelta(minutes=1), collected_at=now,
        )

    def make_candidate(self, bet_type="SINGLE"):
        legs = [LegCandidate(raw_text="France ML", selection="France ML", market_type="h2h", side="France")]
        if bet_type == "PARLAY":
            legs.append(LegCandidate(raw_text="Spain ML", selection="Spain ML", market_type="h2h", side="Spain"))
        return BetCandidate(
            bet_type=bet_type, description=" + ".join(leg.selection for leg in legs),
            quoted_odds_decimal=2.5 if bet_type == "PARLAY" else 1.8,
            original_odds_text="2.5" if bet_type == "PARLAY" else "1.8",
            sport="Soccer", league=None, legs=legs, parser_confidence=.9,
        )

    def test_first_snapshot_is_immutable_and_edit_is_revision(self):
        source = self.make_source()
        source_id, is_new, revised = self.db.upsert_source(source)
        self.assertTrue(is_new)
        self.assertFalse(revised)
        edited = self.make_source("Pick: Spain ML\nOdds: 2.00")
        source_id_2, is_new_2, revised_2 = self.db.upsert_source(edited)
        self.assertEqual(source_id, source_id_2)
        self.assertFalse(is_new_2)
        self.assertTrue(revised_2)
        original = self.db.query("SELECT body_original FROM source_items WHERE id=?", (source_id,))[0]
        self.assertIn("France", original["body_original"])
        self.assertEqual(len(self.db.query("SELECT * FROM source_revisions")), 1)

    def test_old_reddit_content_is_minimized_with_stable_pseudonym(self):
        now = utc_now()
        first = self.make_source("Pick: France ML @ 1.80")
        first.reddit_id = "t1_old_one"
        first.author = "same_tipster"
        first.collected_at = now - timedelta(hours=49)
        first.created_at = first.collected_at - timedelta(minutes=1)
        second = self.make_source("Pick: Spain ML @ 1.90")
        second.reddit_id = "t1_old_two"
        second.author = "same_tipster"
        second.collected_at = now - timedelta(hours=50)
        second.created_at = second.collected_at - timedelta(minutes=1)
        self.db.upsert_source(first)
        self.db.upsert_source(second)
        self.assertEqual(self.db.minimize_reddit_sources(48), 2)
        rows = self.db.query(
            "SELECT author, permalink, parent_title, body_original FROM source_items ORDER BY id"
        )
        self.assertEqual(rows[0]["author"], rows[1]["author"])
        self.assertTrue(rows[0]["author"].startswith("tipster_"))
        self.assertEqual(rows[0]["permalink"], "")
        self.assertIn("removed", rows[0]["body_original"])
        self.assertIn("removed", rows[0]["parent_title"])

    def test_same_candidate_cannot_be_counted_twice(self):
        source_id, _, _ = self.db.upsert_source(self.make_source())
        first, inserted = self.db.insert_candidate(source_id, 0, self.make_candidate(), 1)
        second, inserted_again = self.db.insert_candidate(source_id, 0, self.make_candidate(), 1)
        self.assertEqual(first, second)
        self.assertTrue(inserted)
        self.assertFalse(inserted_again)

    def test_late_manual_approval_is_rejected(self):
        source_id, _, _ = self.db.upsert_source(self.make_source())
        slip_id, _ = self.db.insert_candidate(source_id, 0, self.make_candidate(), 1)
        self.db.execute(
            "UPDATE slips SET event_start_at=? WHERE id=?",
            (iso_utc(utc_now() - timedelta(minutes=1)), slip_id),
        )
        ok, message = self.db.approve_slip(slip_id, minimum_lead_minutes=2)
        self.assertFalse(ok)
        self.assertIn("started", message)
        self.assertEqual(self.db.get_slip(slip_id)["status"], "REJECTED")

    def test_fixed_stake_profit_uses_slip_price(self):
        source_id, _, _ = self.db.upsert_source(self.make_source())
        slip_id, _ = self.db.insert_candidate(source_id, 0, self.make_candidate("PARLAY"), 1)
        for leg in self.db.get_legs(slip_id):
            self.db.settle_leg(leg["id"], "WON", home_score=2, away_score=0, detail="test")
        status = self.db.recompute_slip_settlement(slip_id)
        slip = self.db.get_slip(slip_id)
        self.assertEqual(status, "WON")
        self.assertEqual(slip["profit_units"], 1.5)

    def test_parlay_settles_lost_as_soon_as_one_leg_loses(self):
        source_id, _, _ = self.db.upsert_source(self.make_source())
        slip_id, _ = self.db.insert_candidate(source_id, 0, self.make_candidate("PARLAY"), 1)
        first_leg = self.db.get_legs(slip_id)[0]
        self.db.settle_leg(first_leg["id"], "LOST", home_score=0, away_score=1, detail="test")
        status = self.db.recompute_slip_settlement(slip_id)
        self.assertEqual(status, "LOST")
        self.assertEqual(self.db.get_slip(slip_id)["profit_units"], -1)

    def test_parlay_push_uses_posted_prices_for_surviving_legs(self):
        source_id, _, _ = self.db.upsert_source(self.make_source())
        slip_id, _ = self.db.insert_candidate(source_id, 0, self.make_candidate("PARLAY"), 1)
        legs = self.db.get_legs(slip_id)
        self.db.execute(
            "UPDATE legs SET quoted_odds_decimal=1.70 WHERE id=?", (legs[0]["id"],)
        )
        self.db.execute(
            "UPDATE legs SET quoted_odds_decimal=1.80 WHERE id=?", (legs[1]["id"],)
        )
        self.db.settle_leg(legs[0]["id"], "WON", home_score=2, away_score=0, detail="test")
        self.db.settle_leg(legs[1]["id"], "PUSH", home_score=1, away_score=1, detail="test")
        status = self.db.recompute_slip_settlement(slip_id)
        slip = self.db.get_slip(slip_id)
        self.assertEqual(status, "WON")
        self.assertAlmostEqual(slip["profit_units"], 0.70)
        self.assertEqual(slip["settlement_source"], "automatic-score-push-adjusted")

    def test_parlay_push_without_individual_price_needs_review(self):
        source_id, _, _ = self.db.upsert_source(self.make_source())
        slip_id, _ = self.db.insert_candidate(source_id, 0, self.make_candidate("PARLAY"), 1)
        legs = self.db.get_legs(slip_id)
        self.db.settle_leg(legs[0]["id"], "WON", home_score=2, away_score=0, detail="test")
        self.db.settle_leg(legs[1]["id"], "PUSH", home_score=1, away_score=1, detail="test")
        self.assertEqual(self.db.recompute_slip_settlement(slip_id), "NEEDS_SETTLEMENT")
        self.assertIn("individual quoted price", self.db.get_slip(slip_id)["review_note"])

    def test_all_push_parlay_returns_the_stake(self):
        source_id, _, _ = self.db.upsert_source(self.make_source())
        slip_id, _ = self.db.insert_candidate(source_id, 0, self.make_candidate("PARLAY"), 1)
        for leg in self.db.get_legs(slip_id):
            self.db.settle_leg(leg["id"], "PUSH", home_score=1, away_score=1, detail="test")
        self.assertEqual(self.db.recompute_slip_settlement(slip_id), "PUSH")
        self.assertEqual(self.db.get_slip(slip_id)["profit_units"], 0)

    def test_stale_in_play_slip_moves_to_manual_settlement(self):
        source_id, _, _ = self.db.upsert_source(self.make_source())
        slip_id, _ = self.db.insert_candidate(source_id, 0, self.make_candidate(), 1)
        self.db.execute(
            "UPDATE slips SET status='IN_PLAY', placed_at=?, event_start_at=? WHERE id=?",
            (
                iso_utc(utc_now() - timedelta(hours=30)),
                iso_utc(utc_now() - timedelta(hours=25)),
                slip_id,
            ),
        )
        self.assertEqual(self.db.flag_stale_in_play(grace_hours=24), 1)
        self.assertEqual(self.db.get_slip(slip_id)["status"], "NEEDS_SETTLEMENT")

    def test_review_candidate_expires_after_event_start(self):
        source_id, _, _ = self.db.upsert_source(self.make_source())
        slip_id, _ = self.db.insert_candidate(source_id, 0, self.make_candidate(), 1)
        self.db.execute(
            "UPDATE slips SET event_start_at=? WHERE id=?",
            (iso_utc(utc_now() - timedelta(seconds=1)), slip_id),
        )
        self.assertEqual(self.db.expire_review_slips(max_age_days=7), 1)
        self.assertEqual(self.db.get_slip(slip_id)["status"], "REJECTED")


if __name__ == "__main__":
    unittest.main()
