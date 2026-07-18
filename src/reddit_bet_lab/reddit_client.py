from __future__ import annotations

import base64
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .config import Settings
from .http_client import ProviderError, json_request
from .models import RawSource, utc_now


UTC = timezone.utc
THREAD_RE = re.compile(
    r"\b(?:daily picks?|reddit daily picks?|pick of the day|potd|betting and picks?|"
    r"daily discussion|soccer betting|world cup|parlay|acca|bet builder|best bets?|props?)\b",
    re.I,
)
PICK_BODY_RE = re.compile(
    r"(?:\b(?:pick|bet|play|prediction|selection|odds?|price)\s*[:=@\-]|"
    r"@\s*(?:\d+\.\d+|[+-]\d{3,4})|\b(?:over|under)\s*\d|"
    r"\b(?:parlay|acca|bet builder|btts|moneyline|to win|draw no bet|double chance)\b)",
    re.I,
)
CONTAINER_AUTHOR_RE = re.compile(r"^(?:sbpotdbot|pikerekt|automoderator)$", re.I)


class RedditClient:
    """Small read-only OAuth client for Reddit's approved Data API."""

    def __init__(self, settings: Settings):
        if not settings.reddit_ready:
            raise ProviderError(
                "Reddit collection is locked. Obtain explicit Data API approval, add the "
                "approved credentials to .env, and only then set REDDIT_API_APPROVED=true."
            )
        self.settings = settings
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._last_request_at = 0.0
        self.errors: list[str] = []

    def _token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token
        basic = base64.b64encode(
            f"{self.settings.reddit_client_id}:{self.settings.reddit_client_secret}".encode(
                "utf-8"
            )
        ).decode("ascii")
        response = json_request(
            "https://www.reddit.com/api/v1/access_token",
            method="POST",
            headers={
                "Authorization": f"Basic {basic}",
                "User-Agent": self.settings.reddit_user_agent,
            },
            form={"grant_type": "client_credentials"},
        )
        token = response.data.get("access_token")
        if not token:
            raise ProviderError("Reddit OAuth did not return an access token.")
        self._access_token = str(token)
        self._token_expires_at = time.time() + int(response.data.get("expires_in", 3600))
        return self._access_token

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        # A conservative client-side delay complements Reddit's returned rate-limit headers.
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < 0.65:
            time.sleep(0.65 - elapsed)
        response = json_request(
            f"https://oauth.reddit.com{path}",
            params=params,
            headers={
                "Authorization": f"bearer {self._token()}",
                "User-Agent": self.settings.reddit_user_agent,
            },
        )
        self._last_request_at = time.monotonic()
        remaining = _header_float(response.headers, "x-ratelimit-remaining")
        reset = _header_float(response.headers, "x-ratelimit-reset")
        if remaining is not None and remaining < 2 and reset:
            time.sleep(min(5.0, max(0.0, reset)))
        return response.data

    def collect_sources(self, collected_at: datetime | None = None) -> list[RawSource]:
        collected_at = collected_at or utc_now()
        cutoff = collected_at - timedelta(hours=self.settings.collect_lookback_hours)
        sources: list[RawSource] = []
        seen_ids: set[str] = set()
        for subreddit in self.settings.subreddits:
            try:
                submissions = self._new_submissions(subreddit)
            except ProviderError as exc:
                self.errors.append(f"r/{subreddit} listing failed: {exc}")
                continue
            recent = [item for item in submissions if _created(item) >= cutoff]
            thread_count = 0
            for submission in recent:
                data = submission.get("data", {})
                submission_id = str(data.get("id", ""))
                if not submission_id:
                    continue
                title = str(data.get("title") or "")
                body = str(data.get("selftext") or "")
                author = str(data.get("author") or "[deleted]")
                is_container = bool(THREAD_RE.search(title))

                if body and PICK_BODY_RE.search(body) and not (
                    is_container and CONTAINER_AUTHOR_RE.match(author)
                ):
                    source = self._submission_source(
                        subreddit, data, collected_at
                    )
                    if source.reddit_id not in seen_ids:
                        sources.append(source)
                        seen_ids.add(source.reddit_id)

                should_fetch_comments = is_container or (
                    subreddit.lower() == "sportsbetting" and PICK_BODY_RE.search(title + " " + body)
                )
                if not should_fetch_comments:
                    continue
                if thread_count >= self.settings.max_threads_per_subreddit:
                    continue
                thread_count += 1
                try:
                    comments = self._top_level_comments(subreddit, submission_id)
                except ProviderError as exc:
                    self.errors.append(
                        f"r/{subreddit} comments for {submission_id} failed: {exc}"
                    )
                    continue
                for comment in comments:
                    comment_data = comment.get("data", {})
                    if _created(comment_data) < cutoff:
                        continue
                    comment_body = str(comment_data.get("body") or "")
                    if not PICK_BODY_RE.search(comment_body):
                        continue
                    source = self._comment_source(
                        subreddit, submission_id, title, comment_data, collected_at
                    )
                    if source.reddit_id not in seen_ids:
                        sources.append(source)
                        seen_ids.add(source.reddit_id)
        return sources

    def _new_submissions(self, subreddit: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        after: str | None = None
        while len(result) < self.settings.reddit_post_limit:
            page_size = min(100, self.settings.reddit_post_limit - len(result))
            payload = self._get(
                f"/r/{subreddit}/new",
                {"limit": page_size, "after": after, "raw_json": 1},
            )
            listing = payload.get("data", {})
            children = list(listing.get("children", []))
            result.extend(children)
            after = listing.get("after")
            if not after or not children:
                break
        return result

    def _top_level_comments(
        self, subreddit: str, submission_id: str
    ) -> Iterable[dict[str, Any]]:
        payload = self._get(
            f"/r/{subreddit}/comments/{submission_id}",
            {"sort": "new", "limit": 500, "depth": 1, "raw_json": 1},
        )
        if not isinstance(payload, list) or len(payload) < 2:
            return []
        return [
            child
            for child in payload[1].get("data", {}).get("children", [])
            if child.get("kind") == "t1"
        ]

    @staticmethod
    def _submission_source(
        subreddit: str, data: dict[str, Any], collected_at: datetime
    ) -> RawSource:
        return RawSource(
            reddit_id=f"t3_{data['id']}",
            subreddit=subreddit,
            submission_id=str(data["id"]),
            source_type="submission",
            parent_title=str(data.get("title") or ""),
            author=str(data.get("author") or "[deleted]"),
            permalink=f"https://www.reddit.com{data.get('permalink', '')}",
            body=str(data.get("selftext") or ""),
            created_at=_created(data),
            collected_at=collected_at,
            edited_at=_edited(data),
            score=_optional_int(data.get("score")),
            flair=str(data.get("link_flair_text")) if data.get("link_flair_text") else None,
        )

    @staticmethod
    def _comment_source(
        subreddit: str,
        submission_id: str,
        title: str,
        data: dict[str, Any],
        collected_at: datetime,
    ) -> RawSource:
        permalink = data.get("permalink") or f"/r/{subreddit}/comments/{submission_id}/_/{data['id']}"
        return RawSource(
            reddit_id=f"t1_{data['id']}",
            subreddit=subreddit,
            submission_id=submission_id,
            source_type="comment",
            parent_title=title,
            author=str(data.get("author") or "[deleted]"),
            permalink=f"https://www.reddit.com{permalink}",
            body=str(data.get("body") or ""),
            created_at=_created(data),
            collected_at=collected_at,
            edited_at=_edited(data),
            score=_optional_int(data.get("score")),
        )


def _created(data: dict[str, Any]) -> datetime:
    return datetime.fromtimestamp(float(data.get("created_utc", 0)), tz=UTC)


def _edited(data: dict[str, Any]) -> datetime | None:
    value = data.get("edited")
    if not value or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(float(value), tz=UTC)


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _header_float(headers: Any, key: str) -> float | None:
    for candidate, value in headers.items():
        if candidate.lower() == key.lower():
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None
