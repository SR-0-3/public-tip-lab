from __future__ import annotations

import math
from typing import Any

from .config import Settings
from .db import Database
from .http_client import ProviderError
from .models import RunSummary, iso_utc, parse_utc, utc_now
from .odds_client import OddsClient
from .parser import parse_source
from .reddit_client import RedditClient
from .settlement import settle_leg_from_score


class Experiment:
    def __init__(self, settings: Settings, db: Database | None = None):
        self.settings = settings
        self.db = db or Database(settings.db_path)
        self.db.initialize(settings.starting_bank_units)

    def collect(self) -> RunSummary:
        summary = RunSummary("collect")
        run_id = self.db.start_run(summary)
        try:
            summary.counters["sources_minimized_for_retention"] = (
                self.db.minimize_reddit_sources(self.settings.reddit_raw_retention_hours)
            )
            client = RedditClient(self.settings)
            collected_at = utc_now()
            sources = client.collect_sources(collected_at)
            summary.errors.extend(client.errors)
            summary.counters["sources_seen"] = len(sources)
            new_slips: list[int] = []
            for source in sources:
                source_id, is_new, revised = self.db.upsert_source(source)
                if revised:
                    summary.bump("source_edits_observed")
                    self.db.audit(
                        "source_edit_observed",
                        "source",
                        source_id,
                        {"reddit_id": source.reddit_id},
                    )
                if not is_new:
                    summary.bump("sources_already_seen")
                    continue
                summary.bump("sources_new")
                outcome = parse_source(source)
                if not outcome.candidates:
                    self.db.set_source_processing(
                        source_id, "REJECTED", outcome.rejection_reason
                    )
                    summary.bump("sources_rejected")
                    continue
                self.db.set_source_processing(source_id, "PARSED")
                for index, candidate in enumerate(outcome.candidates):
                    slip_id, inserted = self.db.insert_candidate(
                        source_id, index, candidate, self.settings.stake_units
                    )
                    if inserted:
                        summary.bump("candidates_created")
                        new_slips.append(slip_id)

            if self.settings.odds_ready:
                odds = OddsClient(self.settings)
                for slip_id in new_slips:
                    status = self.validate_slip(slip_id, odds)
                    summary.bump(f"validation_{status.lower()}")
                    slip = self.db.get_slip(slip_id)
                    if (
                        self.settings.auto_approve_verified
                        and slip
                        and slip["status"] == "REVIEW"
                        and status == "ODDS_MATCH"
                        and float(slip["parser_confidence"]) >= 0.75
                    ):
                        ok, _ = self.db.approve_slip(
                            slip_id,
                            verification_status="ODDS_MATCH",
                            review_note="Automatically placed after strict event, market, and price validation.",
                            minimum_lead_minutes=self.settings.minimum_lead_minutes,
                        )
                        summary.bump("paper_bets_placed" if ok else "auto_approval_rejected")
                self._save_quota(odds)
            else:
                summary.counters["validation_skipped_no_key"] = len(new_slips)
            summary.counters["review_candidates_expired"] = self.db.expire_review_slips(
                max_age_days=self.settings.max_event_horizon_days
            )
            if client.errors and not sources:
                summary.status = "failed"
            elif summary.errors:
                summary.status = "completed_with_warnings"
            else:
                summary.status = "completed"
        except Exception as exc:
            summary.status = "failed"
            summary.errors.append(str(exc))
        finally:
            summary.ended_at = utc_now()
            self.db.finish_run(run_id, summary)
        return summary

    def validate_pending(self) -> RunSummary:
        summary = RunSummary("validate")
        run_id = self.db.start_run(summary)
        try:
            odds = OddsClient(self.settings)
            rows = self.db.query("SELECT id FROM slips WHERE status='REVIEW' ORDER BY id")
            for row in rows:
                slip_id = int(row["id"])
                status = self.validate_slip(slip_id, odds)
                summary.bump(f"validation_{status.lower()}")
                slip = self.db.get_slip(slip_id)
                if (
                    self.settings.auto_approve_verified
                    and slip
                    and status == "ODDS_MATCH"
                    and float(slip["parser_confidence"]) >= 0.75
                ):
                    ok, _ = self.db.approve_slip(
                        slip_id,
                        verification_status="ODDS_MATCH",
                        review_note="Automatically placed after strict retry validation.",
                        minimum_lead_minutes=self.settings.minimum_lead_minutes,
                    )
                    summary.bump("paper_bets_placed" if ok else "auto_approval_rejected")
            self._save_quota(odds)
            summary.counters["review_candidates_expired"] = self.db.expire_review_slips(
                max_age_days=self.settings.max_event_horizon_days
            )
            summary.status = "completed"
        except Exception as exc:
            summary.status = "failed"
            summary.errors.append(str(exc))
        finally:
            summary.ended_at = utc_now()
            self.db.finish_run(run_id, summary)
        return summary

    def validate_slip(self, slip_id: int, odds: OddsClient | None = None) -> str:
        slip = self.db.get_slip(slip_id)
        if not slip:
            raise ValueError(f"Slip {slip_id} does not exist")
        odds = odds or OddsClient(self.settings)
        collected_at = parse_utc(slip["collected_at"])
        if collected_at is None:
            raise ValueError("Source has no collection time")
        legs = self.db.get_legs(slip_id)
        results: list[tuple[dict[str, Any], Any]] = []
        for leg in legs:
            leg_quote = (
                slip["quoted_odds_decimal"]
                if slip["bet_type"] == "SINGLE"
                else leg["quoted_odds_decimal"]
            )
            result = odds.validate_leg(
                leg,
                collected_at=collected_at,
                quoted_odds_decimal=float(leg_quote) if leg_quote is not None else None,
                context=f"{slip['parent_title']} {slip.get('league') or ''}",
            )
            event = result.matched_event
            self.db.update_leg_validation(
                int(leg["id"]),
                verification_status=result.status,
                confidence=result.confidence,
                notes=result.notes,
                market_key=result.market_key,
                verified_odds_decimal=result.verified_price,
                provider_event_id=event.event_id if event else None,
                sport_key=event.sport_key if event else None,
                sport=slip.get("sport"),
                league=event.sport_title if event else slip.get("league"),
                home_team=event.home_team if event else None,
                away_team=event.away_team if event else None,
                event_start_at=iso_utc(event.commence_time) if event else None,
            )
            if result.prices:
                self.db.add_odds_snapshots(int(leg["id"]), result.prices)
            results.append((leg, result))

        overall_status, verified_price, notes = self._aggregate_validation(slip, results)
        matched_events = [result.matched_event for _, result in results if result.matched_event]
        event_start = min(
            (event.commence_time for event in matched_events), default=None
        )
        confidence = min(
            [result.confidence for _, result in results], default=0.0
        )
        confidence = min(confidence, float(slip["parser_confidence"]))
        league = (
            matched_events[0].sport_title
            if matched_events and all(
                event.sport_title == matched_events[0].sport_title for event in matched_events
            )
            else slip.get("league")
        )
        self.db.update_slip_validation(
            slip_id,
            verification_status=overall_status,
            confidence=confidence,
            notes=notes,
            verified_odds_decimal=verified_price,
            sport=slip.get("sport"),
            league=league,
            event_start_at=iso_utc(event_start),
        )
        return overall_status

    def _aggregate_validation(
        self, slip: dict[str, Any], results: list[tuple[dict[str, Any], Any]]
    ) -> tuple[str, float | None, list[str]]:
        if not results:
            return "UNVERIFIED", None, ["No legs were extracted."]
        statuses = [result.status for _, result in results]
        notes = [note for _, result in results for note in result.notes]
        if slip["bet_type"] == "SINGLE":
            result = results[0][1]
            return result.status, result.verified_price, notes
        if any(status == "UNVERIFIED" for status in statuses):
            return "UNVERIFIED", None, notes
        if any(status == "EVENT_MATCH" for status in statuses):
            return "EVENT_MATCH", None, notes
        prices = [result.verified_price for _, result in results]
        if any(price is None for price in prices):
            return "MARKET_MATCH", None, notes
        product_price = math.prod(float(price) for price in prices if price is not None)
        event_ids = [result.matched_event.event_id for _, result in results if result.matched_event]
        if len(set(event_ids)) != len(event_ids):
            notes.append(
                "Same-event parlay pricing is correlated, so multiplying leg prices is not a valid odds check."
            )
            return "MARKET_MATCH", product_price, notes
        quoted = slip.get("quoted_odds_decimal")
        if quoted is None:
            return "MARKET_MATCH", product_price, notes
        gap = abs(float(quoted) / product_price - 1.0)
        if gap <= self.settings.odds_relative_tolerance:
            notes.append(f"Parlay quote is within {gap:.1%} of the product of independent leg medians.")
            return "ODDS_MATCH", product_price, notes
        notes.append(f"Parlay quote differs from independent leg medians by {gap:.1%}.")
        return "MARKET_MATCH", product_price, notes

    def settle(self) -> RunSummary:
        summary = RunSummary("settle")
        run_id = self.db.start_run(summary)
        try:
            summary.counters["moved_to_in_play"] = self.db.refresh_time_statuses()
            summary.counters["flagged_for_manual_settlement"] = self.db.flag_stale_in_play()
            open_legs = self.db.open_legs()
            if not open_legs:
                summary.status = "completed"
                return self._finish_early(run_id, summary)
            odds = OddsClient(self.settings)
            by_sport: dict[str, list[dict[str, Any]]] = {}
            for leg in open_legs:
                by_sport.setdefault(str(leg["sport_key"]), []).append(leg)
            for sport_key, legs in by_sport.items():
                try:
                    events = {str(item["id"]): item for item in odds.scores(sport_key)}
                except ProviderError as exc:
                    summary.errors.append(f"{sport_key}: {exc}")
                    continue
                for leg in legs:
                    event = events.get(str(leg["provider_event_id"]))
                    if not event or not event.get("completed"):
                        continue
                    result = settle_leg_from_score(leg, event)
                    if result.status is None:
                        self.db.execute(
                            "UPDATE slips SET status='NEEDS_SETTLEMENT', review_note=?, updated_at=? WHERE id=?",
                            (result.detail, iso_utc(utc_now()), leg["slip_id"]),
                        )
                        summary.bump("manual_settlement_required")
                        continue
                    self.db.settle_leg(
                        int(leg["id"]),
                        result.status,
                        home_score=result.home_score,
                        away_score=result.away_score,
                        detail=result.detail,
                    )
                    self.db.mark_last_prestart_snapshot(int(leg["id"]))
                    final = self.db.recompute_slip_settlement(int(leg["slip_id"]))
                    summary.bump("legs_settled")
                    if final in {"WON", "LOST", "PUSH", "VOID"}:
                        summary.bump(f"slips_{final.lower()}")
            self._save_quota(odds)
            summary.status = "completed" if not summary.errors else "completed_with_warnings"
        except Exception as exc:
            summary.status = "failed"
            summary.errors.append(str(exc))
        finally:
            if summary.ended_at is None:
                summary.ended_at = utc_now()
                self.db.finish_run(run_id, summary)
        return summary

    def daily(self) -> tuple[RunSummary, RunSummary]:
        return self.collect(), self.settle()

    def _save_quota(self, odds: OddsClient) -> None:
        for key, value in odds.last_quota.items():
            self.db.set_metadata(f"odds_api_{key}", str(value))

    def _finish_early(self, run_id: int, summary: RunSummary) -> RunSummary:
        summary.ended_at = utc_now()
        self.db.finish_run(run_id, summary)
        return summary
