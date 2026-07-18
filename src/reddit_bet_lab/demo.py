from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from .db import Database
from .models import BetCandidate, LegCandidate, RawSource, iso_utc, utc_now


DEMO_ROWS = (
    ("SoccerBetting", "pitchprofessor", "Soccer", "SINGLE", "Arsenal vs Chelsea: Over 2.5 goals", 1.91, "WON", 1),
    ("sportsbook", "valuehunter", "Basketball", "SINGLE", "Boston Celtics moneyline", 1.72, "LOST", 2),
    ("SportsBetting", "accaking", "Soccer", "PARLAY", "Liverpool win + Barcelona over 1.5 goals", 2.64, "WON", 3),
    ("SoccerBetting", "pitchprofessor", "Soccer", "SINGLE", "Inter vs Milan: BTTS yes", 1.80, "LOST", 4),
    ("sportsbook", "numbersguy", "Baseball", "SINGLE", "Yankees moneyline", 2.05, "WON", 5),
    ("SportsBetting", "accaking", "Soccer", "PARLAY", "Real Madrid win + Bayern win", 2.21, "LOST", 6),
    ("sportsbook", "valuehunter", "Tennis", "SINGLE", "Sinner moneyline", 1.55, "WON", 7),
    ("SoccerBetting", "cornermerchant", "Soccer", "SINGLE", "Roma vs Napoli: over 8.5 corners", 1.88, "LOST", 8),
    ("sportsbook", "numbersguy", "Basketball", "SINGLE", "Lakers +4.5", 1.95, "WON", 9),
    ("SportsBetting", "accaking", "Soccer", "PARLAY", "PSG win + over 2.5 goals", 2.42, "WON", 10),
    ("SoccerBetting", "pitchprofessor", "Soccer", "SINGLE", "Dortmund draw no bet", 1.70, "PUSH", 11),
    ("sportsbook", "valuehunter", "American Football", "SINGLE", "Chiefs moneyline", 1.66, "LOST", 12),
    ("SportsBetting", "steadyeddie", "Ice Hockey", "SINGLE", "Oilers moneyline", 1.82, "WON", 13),
    ("SoccerBetting", "cornermerchant", "Soccer", "SINGLE", "Ajax vs PSV: BTTS yes", 1.62, "WON", 14),
    ("sportsbook", "numbersguy", "Baseball", "SINGLE", "Dodgers -1.5", 2.08, "LOST", 15),
)


def seed_demo(path: Path, starting_bank: float = 100.0, *, replace: bool = False) -> Database:
    if path.exists():
        if not replace:
            raise FileExistsError(f"Demo database already exists: {path}")
        path.unlink()
    db = Database(path)
    db.initialize(starting_bank)
    now = utc_now()
    for subreddit, author, sport, bet_type, description, odds, status, index in DEMO_ROWS:
        collected = now - timedelta(days=20 - index)
        source = RawSource(
            reddit_id=f"demo_{index}",
            subreddit=subreddit,
            submission_id=f"demo_thread_{index}",
            source_type="demo",
            parent_title="Demo forward-test pick",
            author=author,
            permalink="https://www.reddit.com/",
            body=f"Pick: {description}\nOdds: {odds}",
            created_at=collected - timedelta(minutes=2),
            collected_at=collected,
        )
        source_id, _, _ = db.upsert_source(source)
        leg = LegCandidate(raw_text=description, selection=description, market_type="custom", sport=sport)
        candidate = BetCandidate(
            bet_type=bet_type,
            description=description,
            quoted_odds_decimal=odds,
            original_odds_text=str(odds),
            sport=sport,
            league=None,
            legs=[leg],
            parser_confidence=0.92,
            parser_notes=["Synthetic demo row"],
        )
        slip_id, _ = db.insert_candidate(source_id, 0, candidate, 1.0)
        placed = collected + timedelta(minutes=1)
        event_start = placed + timedelta(hours=2)
        settled = event_start + timedelta(hours=2)
        profit = round(odds - 1, 6) if status == "WON" else (-1.0 if status == "LOST" else 0.0)
        db.execute(
            """
            UPDATE slips SET verification_status='DEMO_VERIFIED', validation_confidence=.95,
                placed_at=?, event_start_at=?, settled_at=?, status=?, profit_units=?,
                settlement_source='demo', updated_at=? WHERE id=?
            """,
            (
                iso_utc(placed), iso_utc(event_start), iso_utc(settled), status,
                profit, iso_utc(settled), slip_id,
            ),
        )
    # Add one upcoming and one review item so every operational state is visible.
    _seed_open_or_review(db, now, 101, "OPEN")
    _seed_open_or_review(db, now, 102, "IN_PLAY")
    _seed_open_or_review(db, now, 103, "REVIEW")
    return db


def _seed_open_or_review(db: Database, now, index: int, status: str) -> None:
    source = RawSource(
        reddit_id=f"demo_{index}",
        subreddit="SoccerBetting",
        submission_id="demo_live",
        source_type="demo",
        parent_title="Demo daily picks",
        author="forwardtester",
        permalink="https://www.reddit.com/",
        body="France vs Spain\nPick: Over 2.5 goals\nOdds: 1.90",
        created_at=now - timedelta(minutes=20),
        collected_at=now - timedelta(minutes=18),
    )
    source_id, _, _ = db.upsert_source(source)
    leg = LegCandidate(
        raw_text="Over 2.5 goals", selection="Over 2.5 goals", market_type="total",
        side="Over", line_value=2.5, event_text="France vs Spain", sport="Soccer"
    )
    candidate = BetCandidate(
        bet_type="SINGLE", description="France vs Spain: Over 2.5 goals",
        quoted_odds_decimal=1.90, original_odds_text="1.90", sport="Soccer",
        league="Demo League", legs=[leg], parser_confidence=.90,
    )
    slip_id, _ = db.insert_candidate(source_id, 0, candidate, 1.0)
    if status == "REVIEW":
        db.execute(
            "UPDATE slips SET event_start_at=?, verification_status='EVENT_MATCH' WHERE id=?",
            (iso_utc(now + timedelta(days=1)), slip_id),
        )
    else:
        start = now - timedelta(minutes=10) if status == "IN_PLAY" else now + timedelta(hours=3)
        db.execute(
            """
            UPDATE slips SET event_start_at=?, placed_at=?, status=?,
                verification_status='ODDS_MATCH', validation_confidence=.92 WHERE id=?
            """,
            (iso_utc(start), iso_utc(now - timedelta(minutes=15)), status, slip_id),
        )
