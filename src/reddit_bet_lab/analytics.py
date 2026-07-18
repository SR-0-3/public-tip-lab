from __future__ import annotations

import math
import random
from typing import Any, Iterable


SETTLED = {"WON", "LOST", "PUSH", "VOID"}


def summary_metrics(rows: Iterable[dict[str, Any]], starting_bank: float) -> dict[str, Any]:
    values = list(rows)
    placed = [row for row in values if row.get("placed_at")]
    settled = [row for row in placed if row.get("status") in SETTLED]
    decisive = [row for row in settled if row.get("status") in {"WON", "LOST"}]
    open_rows = [
        row for row in placed
        if row.get("status") in {"OPEN", "IN_PLAY", "NEEDS_SETTLEMENT"}
    ]
    profit = sum(float(row.get("profit_units") or 0) for row in settled)
    settled_stake = sum(float(row.get("stake_units") or 0) for row in settled)
    open_liability = sum(float(row.get("stake_units") or 0) for row in open_rows)
    wins = sum(row.get("status") == "WON" for row in decisive)
    return {
        "placed": len(placed),
        "settled": len(settled),
        "open": len(open_rows),
        "wins": wins,
        "losses": len(decisive) - wins,
        "profit_units": profit,
        "settled_stake_units": settled_stake,
        "roi": profit / settled_stake if settled_stake else None,
        "hit_rate": wins / len(decisive) if decisive else None,
        "paper_balance": starting_bank + profit,
        "open_liability": open_liability,
        "available_bank": starting_bank + profit - open_liability,
        "max_drawdown": max_drawdown(settled),
        "roi_ci": bootstrap_roi_ci(settled),
        "calibration": calibration_test(decisive),
    }


def bankroll_curve(
    rows: Iterable[dict[str, Any]], starting_bank: float
) -> list[dict[str, Any]]:
    settled = sorted(
        [row for row in rows if row.get("profit_units") is not None],
        key=lambda row: (str(row.get("settled_at") or ""), int(row.get("slip_id") or row.get("id") or 0)),
    )
    balance = starting_bank
    result = [{"order": 0, "settled_at": None, "balance": balance, "profit": 0.0}]
    for index, row in enumerate(settled, start=1):
        profit = float(row.get("profit_units") or 0)
        balance += profit
        result.append(
            {
                "order": index,
                "settled_at": row.get("settled_at"),
                "balance": balance,
                "profit": profit,
                "slip_id": row.get("slip_id") or row.get("id"),
            }
        )
    return result


def max_drawdown(rows: Iterable[dict[str, Any]]) -> float:
    ordered = sorted(
        [row for row in rows if row.get("profit_units") is not None],
        key=lambda row: str(row.get("settled_at") or ""),
    )
    cumulative = 0.0
    peak = 0.0
    worst = 0.0
    for row in ordered:
        cumulative += float(row.get("profit_units") or 0)
        peak = max(peak, cumulative)
        worst = min(worst, cumulative - peak)
    return abs(worst)


def bootstrap_roi_ci(
    rows: Iterable[dict[str, Any]], iterations: int = 2000, seed: int = 7301
) -> tuple[float, float] | None:
    values = [
        (float(row.get("profit_units") or 0), float(row.get("stake_units") or 0))
        for row in rows
        if float(row.get("stake_units") or 0) > 0
    ]
    if len(values) < 5:
        return None
    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(iterations):
        draw = [values[rng.randrange(len(values))] for _ in values]
        stake = sum(item[1] for item in draw)
        if stake:
            samples.append(sum(item[0] for item in draw) / stake)
    if not samples:
        return None
    samples.sort()
    low = samples[int(0.025 * (len(samples) - 1))]
    high = samples[int(0.975 * (len(samples) - 1))]
    return low, high


def calibration_test(rows: Iterable[dict[str, Any]]) -> dict[str, float] | None:
    values = list(rows)
    if len(values) < 3:
        return None
    expected = 0.0
    variance = 0.0
    observed = 0.0
    count = 0
    for row in values:
        try:
            odds = float(row["quoted_odds_decimal"])
        except (KeyError, TypeError, ValueError):
            continue
        if odds <= 1:
            continue
        probability = min(0.999, max(0.001, 1.0 / odds))
        expected += probability
        variance += probability * (1.0 - probability)
        observed += 1.0 if row.get("status") == "WON" else 0.0
        count += 1
    if count < 3 or variance <= 0:
        return None
    z_score = (observed - expected) / math.sqrt(variance)
    return {
        "count": float(count),
        "observed_wins": observed,
        "expected_wins_from_raw_implied_odds": expected,
        "z_score": z_score,
    }
