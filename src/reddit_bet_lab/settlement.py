from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any


@dataclass(slots=True)
class LegResult:
    status: str | None
    detail: str
    home_score: float | None = None
    away_score: float | None = None


def score_pair(event: dict[str, Any]) -> tuple[float, float] | None:
    scores = event.get("scores")
    if not scores:
        return None
    by_name: dict[str, float] = {}
    for item in scores:
        try:
            by_name[str(item["name"])] = float(item["score"])
        except (KeyError, TypeError, ValueError):
            continue
    home = _lookup_score(str(event.get("home_team") or ""), by_name)
    away = _lookup_score(str(event.get("away_team") or ""), by_name)
    if home is None or away is None:
        return None
    return home, away


def settle_leg_from_score(leg: dict[str, Any], event: dict[str, Any]) -> LegResult:
    scores = score_pair(event)
    if scores is None:
        return LegResult(None, "Completed event did not include a usable final score.")
    home_score, away_score = scores
    home_team = str(leg.get("home_team") or event.get("home_team") or "")
    away_team = str(leg.get("away_team") or event.get("away_team") or "")
    market = str(leg.get("market_type") or "")
    side = str(leg.get("side") or leg.get("selection") or "")
    line = _float_or_none(leg.get("line_value"))

    if market == "h2h":
        if home_score == away_score:
            actual = "Draw"
        else:
            actual = home_team if home_score > away_score else away_team
        won = _selection_matches(side, actual)
        return _binary(won, home_score, away_score, f"Final result: {actual}.")

    if market == "total":
        if str(leg.get("sport") or "").lower() == "tennis":
            return LegResult(None, "Tennis totals need market-specific scoring and manual settlement.")
        if line is None:
            return LegResult(None, "Total line is missing.")
        total = home_score + away_score
        if abs(total - line) < 1e-9:
            return LegResult("PUSH", f"Final total {total:g} equals line {line:g}.", home_score, away_score)
        wants_over = side.lower().startswith("over")
        won = total > line if wants_over else total < line
        return _binary(
            won,
            home_score,
            away_score,
            f"Final total {total:g}; selection {side} {line:g}.",
        )

    if market == "spread":
        if line is None:
            return LegResult(None, "Spread line is missing.")
        selected_home = _selection_matches(side, home_team) or _selection_matches(
            str(leg.get("selection") or ""), home_team
        )
        selected_away = _selection_matches(side, away_team) or _selection_matches(
            str(leg.get("selection") or ""), away_team
        )
        if selected_home == selected_away:
            return LegResult(None, "Could not identify the spread team.")
        margin = (home_score - away_score if selected_home else away_score - home_score) + line
        if abs(margin) < 1e-9:
            return LegResult("PUSH", "Adjusted spread score tied.", home_score, away_score)
        return _binary(
            margin > 0,
            home_score,
            away_score,
            f"Adjusted selected-team margin: {margin:g}.",
        )

    if market == "btts":
        actual_yes = home_score > 0 and away_score > 0
        wants_yes = side.lower() not in {"no", "false"}
        return _binary(
            actual_yes == wants_yes,
            home_score,
            away_score,
            f"Both teams scored: {'yes' if actual_yes else 'no'}.",
        )

    if market == "double_chance":
        lower = side.lower().replace(" ", "")
        draw = home_score == away_score
        home_win = home_score > away_score
        away_win = away_score > home_score
        if "1x" in lower:
            won = home_win or draw
        elif "x2" in lower:
            won = away_win or draw
        elif re.search(r"(?:^|\D)12(?:$|\D)", lower):
            won = not draw
        elif "draw" in side.lower() and _selection_matches(side, home_team):
            won = home_win or draw
        elif "draw" in side.lower() and _selection_matches(side, away_team):
            won = away_win or draw
        else:
            return LegResult(None, "Could not interpret the double-chance selection.")
        return _binary(won, home_score, away_score, "Double-chance result evaluated from final score.")

    if market == "draw_no_bet":
        if home_score == away_score:
            return LegResult("PUSH", "Draw-no-bet pushed on a draw.", home_score, away_score)
        actual = home_team if home_score > away_score else away_team
        return _binary(
            _selection_matches(side, actual),
            home_score,
            away_score,
            f"Winner after draw protection: {actual}.",
        )

    return LegResult(None, f"Market '{market}' requires manual settlement.")


def _binary(
    won: bool, home_score: float, away_score: float, detail: str
) -> LegResult:
    return LegResult("WON" if won else "LOST", detail, home_score, away_score)


def _lookup_score(team: str, values: dict[str, float]) -> float | None:
    if team in values:
        return values[team]
    ranked = sorted(
        ((_similarity(team, candidate), score) for candidate, score in values.items()),
        reverse=True,
    )
    return ranked[0][1] if ranked and ranked[0][0] >= 80 else None


def _selection_matches(selection: str, outcome: str) -> bool:
    normalized_selection = _normalize(selection)
    normalized_outcome = _normalize(outcome)
    if not normalized_selection or not normalized_outcome:
        return False
    return (
        normalized_outcome in normalized_selection
        or normalized_selection in normalized_outcome
        or _similarity(normalized_selection, normalized_outcome) >= 76
    )


def _normalize(value: str) -> str:
    value = value.lower()
    value = re.sub(r"\b(?:ml|moneyline|to win|win|draw no bet|dnb|spread|handicap)\b", " ", value)
    value = re.sub(r"[+-]\d+(?:\.\d+)?", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _similarity(left: str, right: str) -> float:
    a, b = _normalize(left), _normalize(right)
    if not a or not b:
        return 0.0
    if a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a)):
        return 100.0
    return SequenceMatcher(None, a, b).ratio() * 100


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None

