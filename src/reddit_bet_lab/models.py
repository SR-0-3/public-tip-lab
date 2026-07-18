from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(slots=True)
class RawSource:
    reddit_id: str
    subreddit: str
    submission_id: str
    source_type: str
    parent_title: str
    author: str
    permalink: str
    body: str
    created_at: datetime
    collected_at: datetime
    edited_at: datetime | None = None
    score: int | None = None
    flair: str | None = None


@dataclass(slots=True)
class LegCandidate:
    raw_text: str
    selection: str
    market_type: str = "custom"
    side: str | None = None
    line_value: float | None = None
    event_text: str | None = None
    home_team_hint: str | None = None
    away_team_hint: str | None = None
    sport: str | None = None
    league: str | None = None
    quoted_odds_decimal: float | None = None


@dataclass(slots=True)
class BetCandidate:
    bet_type: str
    description: str
    quoted_odds_decimal: float | None
    original_odds_text: str | None
    sport: str | None
    league: str | None
    legs: list[LegCandidate] = field(default_factory=list)
    parser_confidence: float = 0.0
    parser_notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ParseOutcome:
    candidates: list[BetCandidate] = field(default_factory=list)
    rejection_reason: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProviderEvent:
    event_id: str
    sport_key: str
    sport_title: str
    commence_time: datetime
    home_team: str
    away_team: str


@dataclass(slots=True)
class MarketPrice:
    bookmaker: str
    market_key: str
    outcome_name: str
    price_decimal: float
    point: float | None = None
    description: str | None = None
    captured_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class ValidationResult:
    status: str
    confidence: float
    notes: list[str] = field(default_factory=list)
    matched_event: ProviderEvent | None = None
    verified_price: float | None = None
    market_key: str | None = None
    prices: list[MarketPrice] = field(default_factory=list)


@dataclass(slots=True)
class RunSummary:
    run_type: str
    started_at: datetime = field(default_factory=utc_now)
    ended_at: datetime | None = None
    status: str = "running"
    counters: dict[str, int | float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def bump(self, key: str, amount: int = 1) -> None:
        self.counters[key] = int(self.counters.get(key, 0)) + amount

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_type": self.run_type,
            "started_at": iso_utc(self.started_at),
            "ended_at": iso_utc(self.ended_at),
            "status": self.status,
            "counters": self.counters,
            "errors": self.errors,
        }

