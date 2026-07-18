from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path
    db_path: Path
    reddit_client_id: str
    reddit_client_secret: str
    reddit_user_agent: str
    reddit_api_approved: bool
    reddit_raw_retention_hours: int
    odds_api_key: str
    odds_region: str
    subreddits: tuple[str, ...]
    starting_bank_units: float
    stake_units: float
    auto_approve_verified: bool
    collect_lookback_hours: int
    max_event_horizon_days: int
    minimum_lead_minutes: int
    collect_interval_minutes: int
    result_lookback_days: int
    event_match_threshold: float
    odds_relative_tolerance: float
    reddit_post_limit: int
    max_threads_per_subreddit: int

    @property
    def reddit_ready(self) -> bool:
        return bool(
            self.reddit_api_approved
            and self.reddit_client_id
            and self.reddit_client_secret
            and self.reddit_user_agent
            and "your_username" not in self.reddit_user_agent
        )

    @property
    def odds_ready(self) -> bool:
        return bool(self.odds_api_key)


def load_settings(env_path: Path | None = None) -> Settings:
    _load_dotenv(env_path or PROJECT_ROOT / ".env")
    db_value = Path(os.getenv("DB_PATH", "data/reddit_bets.sqlite3"))
    if not db_value.is_absolute():
        db_value = PROJECT_ROOT / db_value
    subreddits = tuple(
        part.strip()
        for part in os.getenv(
            "SUBREDDITS", "SoccerBetting,sportsbook,SportsBetting"
        ).split(",")
        if part.strip()
    )
    return Settings(
        project_root=PROJECT_ROOT,
        db_path=db_value,
        reddit_client_id=os.getenv("REDDIT_CLIENT_ID", "").strip(),
        reddit_client_secret=os.getenv("REDDIT_CLIENT_SECRET", "").strip(),
        reddit_user_agent=os.getenv(
            "REDDIT_USER_AGENT",
            "windows:public-tip-lab:v0.1.0 (by u/your_username)",
        ).strip(),
        reddit_api_approved=_bool("REDDIT_API_APPROVED", False),
        reddit_raw_retention_hours=max(1, _int("REDDIT_RAW_RETENTION_HOURS", 48)),
        odds_api_key=os.getenv("ODDS_API_KEY", "").strip(),
        odds_region=os.getenv("ODDS_REGION", "uk").strip(),
        subreddits=subreddits,
        starting_bank_units=_float("STARTING_BANK_UNITS", 100.0),
        stake_units=_float("STAKE_UNITS", 1.0),
        auto_approve_verified=_bool("AUTO_APPROVE_VERIFIED", True),
        collect_lookback_hours=_int("COLLECT_LOOKBACK_HOURS", 36),
        max_event_horizon_days=_int("MAX_EVENT_HORIZON_DAYS", 7),
        minimum_lead_minutes=_int("MINIMUM_LEAD_MINUTES", 2),
        collect_interval_minutes=_int("COLLECT_INTERVAL_MINUTES", 15),
        result_lookback_days=max(1, min(3, _int("RESULT_LOOKBACK_DAYS", 3))),
        event_match_threshold=_float("EVENT_MATCH_THRESHOLD", 78.0),
        odds_relative_tolerance=_float("ODDS_RELATIVE_TOLERANCE", 0.35),
        reddit_post_limit=max(10, min(500, _int("REDDIT_POST_LIMIT", 200))),
        max_threads_per_subreddit=max(
            1, _int("MAX_THREADS_PER_SUBREDDIT", 24)
        ),
    )
