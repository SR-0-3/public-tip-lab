from __future__ import annotations

import re
import statistics
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable

from .config import Settings
from .http_client import ProviderError, json_request
from .models import MarketPrice, ProviderEvent, ValidationResult, parse_utc, utc_now


UTC = timezone.utc
BASE_URL = "https://api.the-odds-api.com/v4"

# Common Reddit shorthand. Aliases are only a recall aid: automatic placement still
# requires a unique future-event match, the correct market, and a plausible price.
TEAM_ALIASES = {
    "man utd": "manchester united",
    "man united": "manchester united",
    "manchester utd": "manchester united",
    "man city": "manchester city",
    "spurs": "tottenham hotspur",
    "wolves": "wolverhampton wanderers",
    "psg": "paris saint germain",
    "barca": "barcelona",
    "atleti": "atletico madrid",
    "juve": "juventus",
    "leverkusen": "bayer leverkusen",
    "dortmund": "borussia dortmund",
    "gladbach": "borussia monchengladbach",
    "sociedad": "real sociedad",
    "brighton": "brighton and hove albion",
    "west ham": "west ham united",
    "newcastle": "newcastle united",
    "leeds": "leeds united",
    "lakers": "los angeles lakers",
    "warriors": "golden state warriors",
    "sixers": "philadelphia 76ers",
    "cavs": "cleveland cavaliers",
    "mavs": "dallas mavericks",
    "knicks": "new york knicks",
    "celtics": "boston celtics",
    "nuggets": "denver nuggets",
    "bucks": "milwaukee bucks",
    "niners": "san francisco 49ers",
    "bucs": "tampa bay buccaneers",
    "pats": "new england patriots",
    "yanks": "new york yankees",
}


class OddsClient:
    def __init__(self, settings: Settings):
        if not settings.odds_ready:
            raise ProviderError(
                "The Odds API key is missing. Copy .env.example to .env and add ODDS_API_KEY."
            )
        self.settings = settings
        self._sports: list[dict[str, Any]] | None = None
        self._events_cache: dict[str, tuple[float, list[ProviderEvent]]] = {}
        self._price_cache: dict[tuple[str, str], list[MarketPrice]] = {}
        self.last_quota: dict[str, str] = {}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        payload = dict(params or {})
        payload["apiKey"] = self.settings.odds_api_key
        response = json_request(f"{BASE_URL}{path}", params=payload)
        self.last_quota = {
            key.lower(): value
            for key, value in response.headers.items()
            if key.lower() in {"x-requests-remaining", "x-requests-used", "x-requests-last"}
        }
        return response.data

    def sports(self) -> list[dict[str, Any]]:
        if self._sports is None:
            payload = self._get("/sports/")
            self._sports = [item for item in payload if item.get("active")]
        return self._sports

    def sport_keys(
        self, sport: str | None, league: str | None = None, context: str = ""
    ) -> list[str]:
        if not sport:
            return []
        target = _normalize(sport)
        candidates = []
        for item in self.sports():
            group = _normalize(str(item.get("group") or ""))
            title = str(item.get("title") or "")
            description = str(item.get("description") or "")
            if not _group_matches(target, group):
                continue
            league_score = 0.0
            if league:
                league_score = max(
                    _similarity(league, title), _similarity(league, description)
                )
            context_score = max(
                _similarity(context, title) if context else 0,
                _similarity(context, description) if context else 0,
            )
            candidates.append((max(league_score, context_score), str(item["key"])))
        candidates.sort(key=lambda value: (-value[0], value[1]))
        return [key for _, key in candidates]

    def events(self, sport_key: str) -> list[ProviderEvent]:
        cached = self._events_cache.get(sport_key)
        if cached and time.monotonic() - cached[0] < 1800:
            return cached[1]
        now = utc_now()
        horizon = now + timedelta(days=self.settings.max_event_horizon_days)
        payload = self._get(
            f"/sports/{sport_key}/events",
            {
                "dateFormat": "iso",
                "commenceTimeFrom": now.isoformat().replace("+00:00", "Z"),
                "commenceTimeTo": horizon.isoformat().replace("+00:00", "Z"),
            },
        )
        events = [_provider_event(item) for item in payload]
        self._events_cache[sport_key] = (time.monotonic(), events)
        return events

    def find_event(
        self,
        leg: dict[str, Any],
        *,
        collected_at: datetime,
        context: str = "",
    ) -> tuple[ProviderEvent | None, float, list[str]]:
        notes: list[str] = []
        keys = self.sport_keys(leg.get("sport"), leg.get("league"), context)
        if not keys:
            return None, 0.0, ["Sport or league could not be mapped to a provider key."]
        home_hint = leg.get("home_team_hint")
        away_hint = leg.get("away_team_hint")
        if not home_hint and not away_hint:
            side = leg.get("side")
            if leg.get("market_type") in {"h2h", "draw_no_bet", "spread"} and side:
                home_hint = str(side)
        if not home_hint and not away_hint:
            return None, 0.0, ["No team names were available for strict event matching."]

        cutoff = max(collected_at, utc_now()) + timedelta(
            minutes=self.settings.minimum_lead_minutes
        )
        horizon = collected_at + timedelta(days=self.settings.max_event_horizon_days)
        scored: list[tuple[float, ProviderEvent]] = []
        for key in keys:
            try:
                events = self.events(key)
            except ProviderError as exc:
                notes.append(f"Event lookup failed for {key}: {exc}")
                continue
            for event in events:
                if not (cutoff < event.commence_time <= horizon):
                    continue
                score = _event_score(home_hint, away_hint, event)
                if score > 0:
                    scored.append((score, event))
            if home_hint and away_hint and scored and max(item[0] for item in scored) >= 98:
                # An exact two-team match is decisive and avoids querying every league.
                break
        if not scored:
            notes.append("No future provider event matched inside the configured horizon.")
            return None, 0.0, notes
        scored.sort(key=lambda item: (-item[0], item[1].commence_time))
        best_score, best = scored[0]
        if best_score < self.settings.event_match_threshold:
            notes.append(
                f"Best event match scored {best_score:.1f}, below the {self.settings.event_match_threshold:.1f} threshold."
            )
            return None, best_score / 100.0, notes
        if len(scored) > 1 and scored[1][0] >= best_score - 3:
            notes.append("Two events matched almost equally; manual confirmation is required.")
            return None, best_score / 100.0, notes
        notes.append(
            f"Matched {best.away_team} at {best.home_team} ({best_score:.1f}/100)."
        )
        return best, best_score / 100.0, notes

    def validate_leg(
        self,
        leg: dict[str, Any],
        *,
        collected_at: datetime,
        quoted_odds_decimal: float | None,
        context: str = "",
    ) -> ValidationResult:
        event, event_confidence, notes = self.find_event(
            leg, collected_at=collected_at, context=context
        )
        if event is None:
            return ValidationResult("UNVERIFIED", event_confidence, notes=notes)

        market_key = _provider_market_key(str(leg.get("market_type") or ""))
        if market_key is None:
            notes.append("The event is real and future, but this market needs manual verification.")
            return ValidationResult(
                "EVENT_MATCH",
                event_confidence,
                notes=notes,
                matched_event=event,
            )

        try:
            prices = self.event_prices(event, market_key)
        except ProviderError as exc:
            notes.append(f"Odds lookup failed: {exc}")
            return ValidationResult(
                "EVENT_MATCH",
                event_confidence,
                notes=notes,
                matched_event=event,
                market_key=market_key,
            )
        matching = _matching_prices(leg, event, prices)
        if not matching:
            notes.append("The event matched, but no equivalent outcome was found in current bookmaker markets.")
            return ValidationResult(
                "EVENT_MATCH",
                event_confidence,
                notes=notes,
                matched_event=event,
                market_key=market_key,
                prices=prices,
            )
        median_price = float(statistics.median(item.price_decimal for item in matching))
        notes.append(
            f"Equivalent market found at a median current price of {median_price:.3f}."
        )
        if quoted_odds_decimal is None:
            status = "MARKET_MATCH"
            confidence = min(0.93, event_confidence + 0.08)
            notes.append("No leg-level quoted price was available for a price check.")
        else:
            relative_gap = abs(quoted_odds_decimal / median_price - 1.0)
            if relative_gap <= self.settings.odds_relative_tolerance:
                status = "ODDS_MATCH"
                confidence = min(0.98, event_confidence + 0.14)
                notes.append(
                    f"Quoted price is within {relative_gap:.1%} of the current market median."
                )
            else:
                status = "MARKET_MATCH"
                confidence = min(0.93, event_confidence + 0.08)
                notes.append(
                    f"Quoted price differs from the current market median by {relative_gap:.1%}."
                )
        return ValidationResult(
            status,
            confidence,
            notes=notes,
            matched_event=event,
            verified_price=median_price,
            market_key=market_key,
            prices=matching,
        )

    def event_prices(self, event: ProviderEvent, market_key: str) -> list[MarketPrice]:
        cache_key = (event.event_id, market_key)
        if cache_key in self._price_cache:
            return self._price_cache[cache_key]
        captured_at = utc_now()
        payload = self._get(
            f"/sports/{event.sport_key}/events/{event.event_id}/odds",
            {
                "regions": self.settings.odds_region,
                "markets": market_key,
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
        )
        result: list[MarketPrice] = []
        for bookmaker in payload.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                for outcome in market.get("outcomes", []):
                    try:
                        price = float(outcome["price"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    result.append(
                        MarketPrice(
                            bookmaker=str(bookmaker.get("title") or bookmaker.get("key") or "unknown"),
                            market_key=str(market.get("key") or market_key),
                            outcome_name=str(outcome.get("name") or ""),
                            price_decimal=price,
                            point=_float_or_none(outcome.get("point")),
                            description=str(outcome.get("description")) if outcome.get("description") else None,
                            captured_at=captured_at,
                        )
                    )
        self._price_cache[cache_key] = result
        return result

    def scores(self, sport_key: str) -> list[dict[str, Any]]:
        payload = self._get(
            f"/sports/{sport_key}/scores/",
            {"daysFrom": self.settings.result_lookback_days, "dateFormat": "iso"},
        )
        return list(payload)


def _provider_event(item: dict[str, Any]) -> ProviderEvent:
    commence = parse_utc(str(item["commence_time"]))
    if commence is None:
        raise ProviderError("Provider event is missing commence_time")
    return ProviderEvent(
        event_id=str(item["id"]),
        sport_key=str(item["sport_key"]),
        sport_title=str(item.get("sport_title") or item.get("sport_key") or ""),
        commence_time=commence,
        home_team=str(item["home_team"]),
        away_team=str(item["away_team"]),
    )


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    result = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    result = result.lower().replace("&", " and ")
    result = re.sub(r"\b(?:fc|cf|afc|sc|cd|ac|club|the)\b", " ", result)
    result = re.sub(r"[^a-z0-9]+", " ", result)
    result = " ".join(result.split())
    return TEAM_ALIASES.get(result, result)


def _group_matches(target: str, group: str) -> bool:
    aliases = {
        "soccer": {"soccer"},
        "american football": {"american football"},
        "ice hockey": {"ice hockey"},
        "mixed martial arts": {"mixed martial arts"},
    }
    if target in aliases:
        return group in aliases[target]
    return target == group or target in group or group in target


def _similarity(left: str | None, right: str | None) -> float:
    a, b = _normalize(left), _normalize(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    if min(len(a), len(b)) >= 4 and (a in b or b in a):
        return 94.0
    seq = SequenceMatcher(None, a, b).ratio() * 100
    a_tokens, b_tokens = set(a.split()), set(b.split())
    overlap = 100 * len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))
    return max(seq, overlap)


def _event_score(
    home_hint: str | None, away_hint: str | None, event: ProviderEvent
) -> float:
    if home_hint and away_hint:
        direct = (
            _similarity(home_hint, event.home_team)
            + _similarity(away_hint, event.away_team)
        ) / 2
        reverse = (
            _similarity(home_hint, event.away_team)
            + _similarity(away_hint, event.home_team)
        ) / 2
        return max(direct, reverse)
    hint = home_hint or away_hint
    return max(_similarity(hint, event.home_team), _similarity(hint, event.away_team)) * 0.94


def _provider_market_key(market_type: str) -> str | None:
    return {"h2h": "h2h", "total": "totals", "spread": "spreads"}.get(market_type)


def _matching_prices(
    leg: dict[str, Any], event: ProviderEvent, prices: Iterable[MarketPrice]
) -> list[MarketPrice]:
    market_type = str(leg.get("market_type") or "")
    side = str(leg.get("side") or leg.get("selection") or "")
    line = _float_or_none(leg.get("line_value"))
    result: list[MarketPrice] = []
    for price in prices:
        if market_type == "h2h":
            if _similarity(side, price.outcome_name) >= 78:
                result.append(price)
            elif "draw" in side.lower() and price.outcome_name.lower() == "draw":
                result.append(price)
        elif market_type == "total":
            if price.outcome_name.lower() != side.lower():
                continue
            if line is None or price.point is None or abs(price.point - line) < 1e-9:
                result.append(price)
        elif market_type == "spread":
            if max(
                _similarity(side, price.outcome_name),
                _similarity(str(leg.get("selection") or ""), price.outcome_name),
            ) < 70:
                continue
            if line is None or price.point is None or abs(price.point - line) < 1e-9:
                result.append(price)
    return result


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
