from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Sequence

from .models import BetCandidate, RawSource, RunSummary, iso_utc, parse_utc, utc_now
from .parser import PARSER_VERSION


SCHEMA_VERSION = "1"
FINAL_LEG_STATUSES = {"WON", "LOST", "PUSH", "VOID"}


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    counters_json TEXT NOT NULL DEFAULT '{}',
    errors_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS source_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reddit_id TEXT NOT NULL UNIQUE,
    subreddit TEXT NOT NULL,
    submission_id TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('submission', 'comment', 'manual', 'demo')),
    parent_title TEXT NOT NULL,
    author TEXT NOT NULL,
    permalink TEXT NOT NULL,
    body_original TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    edited_at TEXT,
    score INTEGER,
    flair TEXT,
    processing_state TEXT NOT NULL DEFAULT 'RAW',
    rejection_reason TEXT,
    extractor_version TEXT NOT NULL DEFAULT 'heuristic-v2'
);

CREATE INDEX IF NOT EXISTS idx_source_subreddit_collected
    ON source_items(subreddit, collected_at);

CREATE TABLE IF NOT EXISTS source_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    body TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    edited_at TEXT,
    UNIQUE(source_id, body_sha256)
);

CREATE TABLE IF NOT EXISTS slips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
    candidate_index INTEGER NOT NULL,
    bet_type TEXT NOT NULL CHECK (bet_type IN ('SINGLE', 'PARLAY')),
    sport TEXT,
    league TEXT,
    description TEXT NOT NULL,
    quoted_odds_decimal REAL,
    original_odds_text TEXT,
    verified_odds_decimal REAL,
    stake_units REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'REVIEW',
    verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
    parser_confidence REAL NOT NULL DEFAULT 0,
    validation_confidence REAL NOT NULL DEFAULT 0,
    parser_notes_json TEXT NOT NULL DEFAULT '[]',
    validation_notes_json TEXT NOT NULL DEFAULT '[]',
    review_note TEXT,
    placed_at TEXT,
    event_start_at TEXT,
    settled_at TEXT,
    profit_units REAL,
    settlement_source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_id, candidate_index)
);

CREATE INDEX IF NOT EXISTS idx_slips_status ON slips(status);
CREATE INDEX IF NOT EXISTS idx_slips_placed ON slips(placed_at);
CREATE INDEX IF NOT EXISTS idx_slips_sport ON slips(sport);

CREATE TABLE IF NOT EXISTS legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slip_id INTEGER NOT NULL REFERENCES slips(id) ON DELETE CASCADE,
    leg_no INTEGER NOT NULL,
    raw_text TEXT NOT NULL,
    selection TEXT NOT NULL,
    market_type TEXT NOT NULL,
    market_key TEXT,
    side TEXT,
    line_value REAL,
    event_text TEXT,
    home_team_hint TEXT,
    away_team_hint TEXT,
    quoted_odds_decimal REAL,
    verified_odds_decimal REAL,
    provider_event_id TEXT,
    sport_key TEXT,
    sport TEXT,
    league TEXT,
    home_team TEXT,
    away_team TEXT,
    event_start_at TEXT,
    verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
    validation_confidence REAL NOT NULL DEFAULT 0,
    validation_notes_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'PENDING',
    home_score REAL,
    away_score REAL,
    settlement_detail TEXT,
    settled_at TEXT,
    UNIQUE(slip_id, leg_no)
);

CREATE INDEX IF NOT EXISTS idx_legs_provider_event ON legs(provider_event_id);
CREATE INDEX IF NOT EXISTS idx_legs_status ON legs(status);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    leg_id INTEGER NOT NULL REFERENCES legs(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    bookmaker TEXT NOT NULL,
    market_key TEXT NOT NULL,
    outcome_name TEXT NOT NULL,
    point REAL,
    price_decimal REAL NOT NULL,
    captured_at TEXT NOT NULL,
    is_last_prestart INTEGER NOT NULL DEFAULT 0,
    UNIQUE(leg_id, bookmaker, market_key, outcome_name, point, captured_at)
);

CREATE INDEX IF NOT EXISTS idx_odds_leg_time
    ON odds_snapshots(leg_id, captured_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    details_json TEXT NOT NULL DEFAULT '{}'
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def initialize(self, starting_bank_units: float | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES('created_at', ?)",
                (iso_utc(utc_now()),),
            )
            if starting_bank_units is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO metadata(key, value) VALUES('starting_bank_units', ?)",
                    (str(float(starting_bank_units)),),
                )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.connect() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount

    def metadata(self) -> dict[str, str]:
        return {row["key"]: row["value"] for row in self.query("SELECT key, value FROM metadata")}

    def set_metadata(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO metadata(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def start_run(self, summary: RunSummary) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs(run_type, started_at, status) VALUES(?, ?, ?)",
                (summary.run_type, iso_utc(summary.started_at), summary.status),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, summary: RunSummary) -> None:
        summary.ended_at = summary.ended_at or utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET ended_at=?, status=?, counters_json=?, errors_json=?
                WHERE id=?
                """,
                (
                    iso_utc(summary.ended_at),
                    summary.status,
                    json.dumps(summary.counters, sort_keys=True),
                    json.dumps(summary.errors),
                    run_id,
                ),
            )

    @staticmethod
    def _body_hash(body: str) -> str:
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    @staticmethod
    def _pseudonymize_author_conn(conn: sqlite3.Connection, author: str) -> str:
        if author.startswith("tipster_"):
            return author
        salt_row = conn.execute(
            "SELECT value FROM metadata WHERE key='author_pseudonym_salt'"
        ).fetchone()
        if salt_row:
            salt = str(salt_row["value"])
        else:
            salt = secrets.token_hex(32)
            conn.execute(
                "INSERT INTO metadata(key, value) VALUES('author_pseudonym_salt', ?)",
                (salt,),
            )
        digest = hmac.new(
            salt.encode("utf-8"), author.encode("utf-8"), hashlib.sha256
        ).hexdigest()[:12]
        return f"tipster_{digest}"

    def upsert_source(self, source: RawSource) -> tuple[int, bool, bool]:
        """Insert an immutable first-seen snapshot; later edits become revisions."""
        body_hash = self._body_hash(source.body)
        with self.connect() as conn:
            stored_author = source.author
            if source.source_type in {"submission", "comment"}:
                stored_author = self._pseudonymize_author_conn(conn, source.author)
            existing = conn.execute(
                "SELECT id, body_sha256 FROM source_items WHERE reddit_id=?",
                (source.reddit_id,),
            ).fetchone()
            if existing:
                revised = existing["body_sha256"] != body_hash
                if revised:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO source_revisions
                        (source_id, observed_at, body, body_sha256, edited_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            existing["id"],
                            iso_utc(source.collected_at),
                            source.body,
                            body_hash,
                            iso_utc(source.edited_at),
                        ),
                    )
                return int(existing["id"]), False, revised

            cur = conn.execute(
                """
                INSERT INTO source_items(
                    reddit_id, subreddit, submission_id, source_type,
                    parent_title, author, permalink, body_original, body_sha256,
                    created_at, collected_at, edited_at, score, flair, extractor_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source.reddit_id,
                    source.subreddit,
                    source.submission_id,
                    source.source_type,
                    source.parent_title,
                    stored_author,
                    source.permalink,
                    source.body,
                    body_hash,
                    iso_utc(source.created_at),
                    iso_utc(source.collected_at),
                    iso_utc(source.edited_at),
                    source.score,
                    source.flair,
                    PARSER_VERSION,
                ),
            )
            return int(cur.lastrowid), True, False

    def set_source_processing(
        self, source_id: int, state: str, rejection_reason: str | None = None
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE source_items SET processing_state=?, rejection_reason=? WHERE id=?",
                (state, rejection_reason, source_id),
            )

    def minimize_reddit_sources(self, retention_hours: int = 48) -> int:
        """Remove old raw Reddit content while retaining structured experiment rows.

        Reddit's current guidance recommends routinely deleting stored user content.
        Stable one-way pseudonyms preserve longitudinal tipster grouping without
        retaining the public username in the working database.
        """
        retention_hours = max(1, retention_hours)
        cutoff = iso_utc(utc_now() - timedelta(hours=retention_hours))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, author FROM source_items
                WHERE source_type IN ('submission', 'comment')
                  AND collected_at < ?
                  AND body_original != '[raw Reddit text removed after retention window]'
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                author = str(row["author"] or "[deleted]")
                pseudonym = self._pseudonymize_author_conn(conn, author)
                conn.execute(
                    """
                    UPDATE source_items
                    SET author=?, permalink='', parent_title='[removed after retention window]',
                        body_original='[raw Reddit text removed after retention window]',
                        flair=NULL, score=NULL
                    WHERE id=?
                    """,
                    (pseudonym, row["id"]),
                )
                conn.execute("DELETE FROM source_revisions WHERE source_id=?", (row["id"],))
                conn.execute(
                    "UPDATE legs SET raw_text='[removed after retention window]' "
                    "WHERE slip_id IN (SELECT id FROM slips WHERE source_id=?)",
                    (row["id"],),
                )
                self._audit_conn(
                    conn,
                    "source_content_minimized",
                    "source",
                    int(row["id"]),
                    {"retention_hours": retention_hours},
                )
            return len(rows)

    def insert_candidate(
        self,
        source_id: int,
        candidate_index: int,
        candidate: BetCandidate,
        stake_units: float,
    ) -> tuple[int, bool]:
        now = iso_utc(utc_now())
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM slips WHERE source_id=? AND candidate_index=?",
                (source_id, candidate_index),
            ).fetchone()
            if existing:
                return int(existing["id"]), False

            cur = conn.execute(
                """
                INSERT INTO slips(
                    source_id, candidate_index, bet_type, sport, league,
                    description, quoted_odds_decimal, original_odds_text,
                    stake_units, parser_confidence, parser_notes_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    candidate_index,
                    candidate.bet_type,
                    candidate.sport,
                    candidate.league,
                    candidate.description,
                    candidate.quoted_odds_decimal,
                    candidate.original_odds_text,
                    stake_units,
                    candidate.parser_confidence,
                    json.dumps(candidate.parser_notes),
                    now,
                    now,
                ),
            )
            slip_id = int(cur.lastrowid)
            for index, leg in enumerate(candidate.legs, start=1):
                conn.execute(
                    """
                    INSERT INTO legs(
                        slip_id, leg_no, raw_text, selection, market_type,
                        side, line_value, event_text, home_team_hint,
                        away_team_hint, quoted_odds_decimal, sport, league
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        slip_id,
                        index,
                        leg.raw_text,
                        leg.selection,
                        leg.market_type,
                        leg.side,
                        leg.line_value,
                        leg.event_text,
                        leg.home_team_hint,
                        leg.away_team_hint,
                        leg.quoted_odds_decimal,
                        leg.sport or candidate.sport,
                        leg.league or candidate.league,
                    ),
                )
            self._audit_conn(
                conn,
                "candidate_created",
                "slip",
                slip_id,
                {"source_id": source_id, "candidate_index": candidate_index},
            )
            return slip_id, True

    def get_slip(self, slip_id: int) -> dict[str, Any] | None:
        rows = self.query(
            """
            SELECT s.*, src.subreddit, src.author, src.permalink,
                   src.body_original, src.collected_at, src.created_at AS source_created_at,
                   src.parent_title
            FROM slips s JOIN source_items src ON src.id=s.source_id
            WHERE s.id=?
            """,
            (slip_id,),
        )
        return rows[0] if rows else None

    def get_legs(self, slip_id: int) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM legs WHERE slip_id=? ORDER BY leg_no", (slip_id,))

    def review_slips(self) -> list[dict[str, Any]]:
        return self.query(
            """
            SELECT s.*, src.subreddit, src.author, src.permalink,
                   src.parent_title, src.body_original, src.collected_at
            FROM slips s JOIN source_items src ON src.id=s.source_id
            WHERE s.status IN ('REVIEW', 'NEEDS_SETTLEMENT')
            ORDER BY src.collected_at, s.id
            """
        )

    def update_leg_validation(
        self,
        leg_id: int,
        *,
        verification_status: str,
        confidence: float,
        notes: list[str],
        market_key: str | None = None,
        verified_odds_decimal: float | None = None,
        provider_event_id: str | None = None,
        sport_key: str | None = None,
        sport: str | None = None,
        league: str | None = None,
        home_team: str | None = None,
        away_team: str | None = None,
        event_start_at: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE legs SET
                    verification_status=?, validation_confidence=?,
                    validation_notes_json=?, market_key=COALESCE(?, market_key),
                    verified_odds_decimal=COALESCE(?, verified_odds_decimal),
                    provider_event_id=COALESCE(?, provider_event_id),
                    sport_key=COALESCE(?, sport_key), sport=COALESCE(?, sport),
                    league=COALESCE(?, league), home_team=COALESCE(?, home_team),
                    away_team=COALESCE(?, away_team),
                    event_start_at=COALESCE(?, event_start_at)
                WHERE id=?
                """,
                (
                    verification_status,
                    confidence,
                    json.dumps(notes),
                    market_key,
                    verified_odds_decimal,
                    provider_event_id,
                    sport_key,
                    sport,
                    league,
                    home_team,
                    away_team,
                    event_start_at,
                    leg_id,
                ),
            )

    def add_odds_snapshots(self, leg_id: int, prices: list[Any]) -> None:
        with self.connect() as conn:
            for price in prices:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO odds_snapshots(
                        leg_id, provider, bookmaker, market_key, outcome_name,
                        point, price_decimal, captured_at
                    ) VALUES (?, 'the-odds-api', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        leg_id,
                        price.bookmaker,
                        price.market_key,
                        price.outcome_name,
                        price.point,
                        price.price_decimal,
                        iso_utc(price.captured_at),
                    ),
                )

    def update_slip_validation(
        self,
        slip_id: int,
        *,
        verification_status: str,
        confidence: float,
        notes: list[str],
        verified_odds_decimal: float | None,
        sport: str | None,
        league: str | None,
        event_start_at: str | None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE slips SET verification_status=?, validation_confidence=?,
                    validation_notes_json=?,
                    verified_odds_decimal=COALESCE(?, verified_odds_decimal),
                    sport=COALESCE(?, sport), league=COALESCE(?, league),
                    event_start_at=COALESCE(?, event_start_at), updated_at=?
                WHERE id=?
                """,
                (
                    verification_status,
                    confidence,
                    json.dumps(notes),
                    verified_odds_decimal,
                    sport,
                    league,
                    event_start_at,
                    iso_utc(utc_now()),
                    slip_id,
                ),
            )

    def approve_slip(
        self,
        slip_id: int,
        *,
        verification_status: str | None = None,
        review_note: str | None = None,
        placed_at: datetime | None = None,
        minimum_lead_minutes: int = 0,
    ) -> tuple[bool, str]:
        placed_at = placed_at or utc_now()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM slips WHERE id=?", (slip_id,)).fetchone()
            if not row:
                return False, "Slip does not exist."
            if row["status"] not in {"REVIEW", "NEEDS_SETTLEMENT"}:
                return False, f"Slip is already {row['status']}."
            if row["quoted_odds_decimal"] is None or row["quoted_odds_decimal"] <= 1:
                return False, "A valid quoted decimal price is required."
            start = parse_utc(row["event_start_at"])
            if start is None:
                return False, "An event start time is required."
            if start <= placed_at + timedelta(minutes=minimum_lead_minutes):
                conn.execute(
                    "UPDATE slips SET status='REJECTED', review_note=?, updated_at=? WHERE id=?",
                    (
                        "Rejected because approval occurred after the placement cutoff.",
                        iso_utc(utc_now()),
                        slip_id,
                    ),
                )
                self._audit_conn(
                    conn,
                    "late_rejection",
                    "slip",
                    slip_id,
                    {"placed_at_attempt": iso_utc(placed_at), "event_start_at": row["event_start_at"]},
                )
                return False, "The earliest event has already started or is inside the lead-time cutoff."

            conn.execute(
                """
                UPDATE slips SET status='OPEN', placed_at=?, review_note=?,
                    verification_status=COALESCE(?, verification_status), updated_at=?
                WHERE id=?
                """,
                (
                    iso_utc(placed_at),
                    review_note,
                    verification_status,
                    iso_utc(utc_now()),
                    slip_id,
                ),
            )
            self._audit_conn(
                conn,
                "paper_bet_placed",
                "slip",
                slip_id,
                {"placed_at": iso_utc(placed_at), "stake_units": row["stake_units"]},
            )
            return True, "Paper bet placed."

    def reject_slip(self, slip_id: int, reason: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE slips SET status='REJECTED', review_note=?, updated_at=? WHERE id=?",
                (reason, iso_utc(utc_now()), slip_id),
            )
            self._audit_conn(conn, "slip_rejected", "slip", slip_id, {"reason": reason})

    def edit_review_slip(
        self,
        slip_id: int,
        *,
        description: str,
        quoted_odds_decimal: float,
        bet_type: str,
        sport: str | None,
        league: str | None,
        event_start_at: str,
        review_note: str | None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE slips SET description=?, quoted_odds_decimal=?, bet_type=?,
                    sport=?, league=?, event_start_at=?, review_note=?, updated_at=?
                WHERE id=? AND status IN ('REVIEW', 'NEEDS_SETTLEMENT')
                """,
                (
                    description,
                    quoted_odds_decimal,
                    bet_type,
                    sport,
                    league,
                    event_start_at,
                    review_note,
                    iso_utc(utc_now()),
                    slip_id,
                ),
            )
            self._audit_conn(conn, "review_edit", "slip", slip_id, {"note": review_note})

    def refresh_time_statuses(self, now: datetime | None = None) -> int:
        now_text = iso_utc(now or utc_now())
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE slips SET status='IN_PLAY', updated_at=?
                WHERE status='OPEN' AND event_start_at IS NOT NULL AND event_start_at<=?
                """,
                (now_text, now_text),
            )
            return cur.rowcount

    def flag_stale_in_play(
        self, now: datetime | None = None, grace_hours: int = 24
    ) -> int:
        cutoff = iso_utc((now or utc_now()) - timedelta(hours=grace_hours))
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE slips
                SET status='NEEDS_SETTLEMENT',
                    review_note=COALESCE(review_note, 'Still unresolved after the settlement grace period.'),
                    updated_at=?
                WHERE status='IN_PLAY' AND event_start_at IS NOT NULL AND event_start_at<=?
                """,
                (iso_utc(utc_now()), cutoff),
            )
            return cur.rowcount

    def expire_review_slips(
        self, *, max_age_days: int, now: datetime | None = None
    ) -> int:
        now = now or utc_now()
        age_cutoff = iso_utc(now - timedelta(days=max_age_days))
        now_text = iso_utc(now)
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE slips
                SET status='REJECTED',
                    review_note='Expired before a valid pre-event placement could be made.',
                    updated_at=?
                WHERE status='REVIEW' AND (
                    (event_start_at IS NOT NULL AND event_start_at<=?)
                    OR source_id IN (
                        SELECT id FROM source_items WHERE collected_at<=?
                    )
                )
                """,
                (now_text, now_text, age_cutoff),
            )
            if cur.rowcount:
                self._audit_conn(
                    conn,
                    "review_candidates_expired",
                    "slip_batch",
                    None,
                    {"count": cur.rowcount, "cutoff": age_cutoff},
                )
            return cur.rowcount

    def open_legs(self) -> list[dict[str, Any]]:
        return self.query(
            """
            SELECT l.*, s.status AS slip_status, s.bet_type, s.quoted_odds_decimal,
                   s.stake_units, s.placed_at
            FROM legs l JOIN slips s ON s.id=l.slip_id
            WHERE s.status IN ('OPEN', 'IN_PLAY', 'NEEDS_SETTLEMENT')
              AND l.status='PENDING' AND l.provider_event_id IS NOT NULL
            ORDER BY l.event_start_at
            """
        )

    def settle_leg(
        self,
        leg_id: int,
        status: str,
        *,
        home_score: float | None,
        away_score: float | None,
        detail: str,
        settled_at: datetime | None = None,
    ) -> None:
        if status not in FINAL_LEG_STATUSES:
            raise ValueError(f"Invalid final leg status: {status}")
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE legs SET status=?, home_score=?, away_score=?,
                    settlement_detail=?, settled_at=? WHERE id=?
                """,
                (
                    status,
                    home_score,
                    away_score,
                    detail,
                    iso_utc(settled_at or utc_now()),
                    leg_id,
                ),
            )
            self._audit_conn(
                conn,
                "leg_settled",
                "leg",
                leg_id,
                {"status": status, "detail": detail},
            )

    def recompute_slip_settlement(self, slip_id: int) -> str:
        with self.connect() as conn:
            slip = conn.execute("SELECT * FROM slips WHERE id=?", (slip_id,)).fetchone()
            legs = conn.execute(
                "SELECT status, quoted_odds_decimal FROM legs WHERE slip_id=? ORDER BY leg_no",
                (slip_id,),
            ).fetchall()
            if not slip or not legs:
                return "REVIEW"
            statuses = [row["status"] for row in legs]
            adjusted_odds: float | None = None
            if "LOST" in statuses:
                final_status = "LOST"
            elif any(value == "PENDING" for value in statuses):
                return str(slip["status"])
            elif all(value == "VOID" for value in statuses):
                final_status = "VOID"
            elif all(value in {"PUSH", "VOID"} for value in statuses):
                final_status = "PUSH"
            elif slip["bet_type"] == "PARLAY" and any(
                value in {"PUSH", "VOID"} for value in statuses
            ):
                surviving_prices = [
                    row["quoted_odds_decimal"] for row in legs if row["status"] == "WON"
                ]
                if not surviving_prices or any(
                    value is None or float(value) <= 1.0 for value in surviving_prices
                ):
                    final_status = "NEEDS_SETTLEMENT"
                else:
                    adjusted_odds = 1.0
                    for value in surviving_prices:
                        adjusted_odds *= float(value)
                    final_status = "WON"
            elif any(value == "WON" for value in statuses):
                final_status = "WON"
            else:
                final_status = "VOID"

            if final_status == "WON":
                settlement_odds = adjusted_odds or float(slip["quoted_odds_decimal"])
                profit = round(
                    float(slip["stake_units"])
                    * (settlement_odds - 1.0),
                    6,
                )
            elif final_status == "LOST":
                profit = -float(slip["stake_units"])
            elif final_status in {"PUSH", "VOID"}:
                profit = 0.0
            else:
                profit = None

            settlement_source = None
            if profit is not None:
                settlement_source = (
                    "automatic-score-push-adjusted"
                    if adjusted_odds is not None
                    else "automatic-score"
                )
            review_note = None
            if final_status == "NEEDS_SETTLEMENT":
                review_note = (
                    "A parlay leg pushed or was voided, but at least one surviving leg "
                    "has no individual quoted price. Confirm the adjusted payout manually."
                )

            conn.execute(
                """
                UPDATE slips SET status=?, profit_units=?, settled_at=?,
                    settlement_source=?, review_note=COALESCE(review_note, ?),
                    updated_at=? WHERE id=?
                """,
                (
                    final_status,
                    profit,
                    iso_utc(utc_now()) if profit is not None else None,
                    settlement_source,
                    review_note,
                    iso_utc(utc_now()),
                    slip_id,
                ),
            )
            if profit is not None:
                self._audit_conn(
                    conn,
                    "slip_settled",
                    "slip",
                    slip_id,
                    {
                        "status": final_status,
                        "profit_units": profit,
                        "settlement_odds_decimal": adjusted_odds
                        or float(slip["quoted_odds_decimal"]),
                    },
                )
            return final_status

    def manual_settle_slip(
        self, slip_id: int, status: str, note: str, settled_at: datetime | None = None
    ) -> None:
        if status not in {"WON", "LOST", "PUSH", "VOID"}:
            raise ValueError("Manual result must be WON, LOST, PUSH, or VOID")
        with self.connect() as conn:
            slip = conn.execute("SELECT * FROM slips WHERE id=?", (slip_id,)).fetchone()
            if not slip:
                raise ValueError("Slip does not exist")
            if status == "WON":
                profit = round(
                    float(slip["stake_units"])
                    * (float(slip["quoted_odds_decimal"]) - 1.0),
                    6,
                )
            elif status == "LOST":
                profit = -float(slip["stake_units"])
            else:
                profit = 0.0
            conn.execute(
                """
                UPDATE slips SET status=?, profit_units=?, settled_at=?,
                    settlement_source='manual', review_note=?, updated_at=? WHERE id=?
                """,
                (
                    status,
                    profit,
                    iso_utc(settled_at or utc_now()),
                    note,
                    iso_utc(utc_now()),
                    slip_id,
                ),
            )
            self._audit_conn(
                conn,
                "manual_settlement",
                "slip",
                slip_id,
                {"status": status, "profit_units": profit, "note": note},
            )

    def mark_last_prestart_snapshot(self, leg_id: int) -> None:
        with self.connect() as conn:
            leg = conn.execute("SELECT event_start_at FROM legs WHERE id=?", (leg_id,)).fetchone()
            if not leg or not leg["event_start_at"]:
                return
            conn.execute("UPDATE odds_snapshots SET is_last_prestart=0 WHERE leg_id=?", (leg_id,))
            row = conn.execute(
                """
                SELECT id FROM odds_snapshots
                WHERE leg_id=? AND captured_at < ?
                ORDER BY captured_at DESC LIMIT 1
                """,
                (leg_id, leg["event_start_at"]),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE odds_snapshots SET is_last_prestart=1 WHERE id=?", (row["id"],)
                )

    def export_rows(self) -> list[dict[str, Any]]:
        return self.query(
            """
            SELECT s.id AS slip_id, src.reddit_id, src.subreddit, src.author,
                   src.permalink, src.collected_at, s.bet_type, s.sport, s.league,
                   s.description, s.quoted_odds_decimal, s.verified_odds_decimal,
                   s.stake_units, s.status, s.verification_status, s.placed_at,
                   s.event_start_at, s.settled_at, s.profit_units,
                   s.settlement_source
            FROM slips s JOIN source_items src ON src.id=s.source_id
            ORDER BY COALESCE(s.placed_at, src.collected_at), s.id
            """
        )

    def audit(
        self, action: str, entity_type: str, entity_id: int | None, details: dict[str, Any]
    ) -> None:
        with self.connect() as conn:
            self._audit_conn(conn, action, entity_type, entity_id, details)

    @staticmethod
    def _audit_conn(
        conn: sqlite3.Connection,
        action: str,
        entity_type: str,
        entity_id: int | None,
        details: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO audit_log(occurred_at, action, entity_type, entity_id, details_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (iso_utc(utc_now()), action, entity_type, entity_id, json.dumps(details, default=str)),
        )
