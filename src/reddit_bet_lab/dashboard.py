from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from .analytics import bankroll_curve, summary_metrics
from .config import load_settings
from .db import Database
from .models import parse_utc
from .pipeline import Experiment


def main() -> None:
    st.set_page_config(page_title="Public Tip Lab", page_icon="📊", layout="wide")
    settings = load_settings()
    db = Database(settings.db_path)
    db.initialize(settings.starting_bank_units)
    db.refresh_time_statuses()

    st.title("Public Tip Lab")
    st.caption(
        "Forward-only paper betting. One fixed unit per Reddit slip; parlays are one slip, not one unit per leg."
    )

    ledger = _load_ledger(db)
    metadata = db.metadata()
    starting_bank = float(metadata.get("starting_bank_units", settings.starting_bank_units))
    filtered = _sidebar_filters(ledger)

    tabs = st.tabs(
        [
            "Overview", "Bet ledger", "Breakdowns", "Tipsters", "Review",
            "Rejected feed", "Operations",
        ]
    )
    with tabs[0]:
        _overview(filtered, starting_bank)
    with tabs[1]:
        _ledger_tab(filtered, db)
    with tabs[2]:
        _breakdowns(filtered)
    with tabs[3]:
        _tipsters(filtered)
    with tabs[4]:
        _review(db, settings)
    with tabs[5]:
        _rejected_feed(db)
    with tabs[6]:
        _operations(db, settings, metadata)


def _load_ledger(db: Database) -> pd.DataFrame:
    rows = db.query(
        """
        SELECT s.id AS slip_id, s.bet_type, s.sport, s.league, s.description,
               s.quoted_odds_decimal, s.verified_odds_decimal, s.stake_units,
               s.status, s.verification_status, s.parser_confidence,
               s.validation_confidence, s.placed_at, s.event_start_at,
               s.settled_at, s.profit_units, s.settlement_source,
               src.subreddit, src.author, src.permalink, src.parent_title,
               src.collected_at, src.source_type,
               GROUP_CONCAT(DISTINCT l.market_type) AS markets,
               COUNT(l.id) AS leg_count
        FROM slips s
        JOIN source_items src ON src.id=s.source_id
        LEFT JOIN legs l ON l.slip_id=s.id
        GROUP BY s.id
        ORDER BY COALESCE(s.placed_at, src.collected_at) DESC, s.id DESC
        """
    )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    for column in ("placed_at", "event_start_at", "settled_at", "collected_at"):
        frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
    for column in (
        "quoted_odds_decimal",
        "verified_odds_decimal",
        "stake_units",
        "profit_units",
        "parser_confidence",
        "validation_confidence",
    ):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def _sidebar_filters(frame: pd.DataFrame) -> pd.DataFrame:
    st.sidebar.header("Experiment filters")
    if frame.empty:
        st.sidebar.info("No candidate bets have been collected yet.")
        return frame
    filtered = frame.copy()
    for column, label in (
        ("subreddit", "Subreddit"),
        ("sport", "Sport"),
        ("bet_type", "Slip type"),
        ("status", "Status"),
    ):
        options = sorted(str(value) for value in filtered[column].dropna().unique())
        selected = st.sidebar.multiselect(label, options, default=options)
        filtered = filtered[filtered[column].astype(str).isin(selected)] if selected else filtered.iloc[0:0]

    dates = frame["collected_at"].dropna()
    if not dates.empty:
        minimum, maximum = dates.dt.date.min(), dates.dt.date.max()
        chosen = st.sidebar.date_input("Collected date", value=(minimum, maximum), min_value=minimum, max_value=maximum)
        if isinstance(chosen, tuple) and len(chosen) == 2:
            filtered = filtered[
                filtered["collected_at"].dt.date.between(chosen[0], chosen[1])
            ]
    st.sidebar.caption(f"{len(filtered):,} candidate slips in this view")
    return filtered


def _overview(frame: pd.DataFrame, starting_bank: float) -> None:
    if frame.empty:
        st.info("Run the collector or load the demo database to populate the experiment.")
        return
    records = frame.to_dict("records")
    metrics = summary_metrics(records, starting_bank)
    columns = st.columns(4)
    columns[0].metric("Realised P/L", _units(metrics["profit_units"], signed=True))
    columns[1].metric("ROI on settled stake", _percent(metrics["roi"]))
    columns[2].metric("Paper balance", _units(metrics["paper_balance"]))
    columns[3].metric("Open liability", _units(metrics["open_liability"]))
    st.caption(
        f"{metrics['settled']} settled · {metrics['open']} unresolved · "
        f"hit rate {_percent(metrics['hit_rate'])} · max drawdown {_units(metrics['max_drawdown'])}"
    )

    settled = frame[frame["profit_units"].notna()].copy()
    left, right = st.columns((2, 1))
    with left:
        curve = pd.DataFrame(bankroll_curve(records, starting_bank))
        figure = px.line(curve, x="order", y="balance", markers=True)
        figure.update_layout(
            title="Paper bankroll after each settled slip",
            xaxis_title="Settled slip number",
            yaxis_title="Units",
            margin=dict(l=20, r=20, t=55, b=20),
        )
        st.plotly_chart(figure, width="stretch")
    with right:
        if settled.empty:
            st.info("No settled bets yet.")
        else:
            grouped = (
                settled.groupby("subreddit", dropna=False)["profit_units"]
                .sum()
                .sort_values()
                .reset_index()
            )
            colors = ["loss" if value < 0 else "profit" for value in grouped["profit_units"]]
            figure = px.bar(
                grouped,
                x="profit_units",
                y="subreddit",
                orientation="h",
                color=colors,
                color_discrete_map={"profit": "#2a9d8f", "loss": "#e76f51"},
            )
            figure.update_layout(
                title="Realised P/L by subreddit",
                xaxis_title="Units",
                yaxis_title=None,
                showlegend=False,
                margin=dict(l=20, r=20, t=55, b=20),
            )
            st.plotly_chart(figure, width="stretch")

    st.subheader("Uncertainty check")
    ci = metrics["roi_ci"]
    calibration = metrics["calibration"]
    if ci:
        st.write(
            f"Bootstrap 95% interval for ROI: **{_percent(ci[0])} to {_percent(ci[1])}**. "
            "If this still spans zero, the experiment has not shown a stable edge."
        )
    else:
        st.write("At least five settled slips are needed for the first bootstrap ROI interval.")
    if calibration:
        st.write(
            f"Observed wins: **{calibration['observed_wins']:.0f}**; raw implied-odds expectation: "
            f"**{calibration['expected_wins_from_raw_implied_odds']:.1f}**; z-score: "
            f"**{calibration['z_score']:.2f}**. Raw implied odds include bookmaker margin, so this is a diagnostic, not a proof of skill."
        )


def _ledger_tab(frame: pd.DataFrame, db: Database) -> None:
    if frame.empty:
        st.info("No slips in the current filter.")
        return
    display = frame[
        [
            "slip_id", "placed_at", "event_start_at", "subreddit", "author",
            "sport", "bet_type", "description", "quoted_odds_decimal", "stake_units",
            "status", "profit_units", "verification_status", "permalink",
        ]
    ].copy()
    display = display.rename(
        columns={
            "slip_id": "ID", "placed_at": "Placed (UTC)", "event_start_at": "Starts (UTC)",
            "subreddit": "Subreddit", "author": "Tipster", "sport": "Sport",
            "bet_type": "Type", "description": "Bet", "quoted_odds_decimal": "Odds",
            "stake_units": "Stake", "status": "Status", "profit_units": "P/L",
            "verification_status": "Verification", "permalink": "Source",
        }
    )
    st.dataframe(
        display,
        hide_index=True,
        width="stretch",
        column_config={
            "Source": st.column_config.LinkColumn("Source", display_text="Open Reddit"),
            "Odds": st.column_config.NumberColumn(format="%.3f"),
            "Stake": st.column_config.NumberColumn(format="%.2f u"),
            "P/L": st.column_config.NumberColumn(format="%+.2f u"),
        },
    )
    slip_id = st.selectbox("Inspect slip", display["ID"].tolist(), format_func=lambda value: f"#{value}")
    slip = db.get_slip(int(slip_id))
    if not slip:
        return
    st.markdown(f"**{slip['description']}** · {slip['status']} · @{slip['quoted_odds_decimal']}")
    st.text_area("Immutable first-seen Reddit snapshot", slip["body_original"], height=170, disabled=True)
    legs = pd.DataFrame(db.get_legs(int(slip_id)))
    if not legs.empty:
        st.dataframe(
            legs[
                ["leg_no", "event_start_at", "home_team", "away_team", "selection", "market_type", "verification_status", "status"]
            ],
            hide_index=True,
            width="stretch",
        )


def _breakdowns(frame: pd.DataFrame) -> None:
    if frame.empty:
        st.info("Breakdowns appear after bets settle.")
        return
    settled = frame[frame["status"].isin(["WON", "LOST", "PUSH", "VOID"])].copy()
    if settled.empty:
        st.info("Breakdowns appear after bets settle.")
        return
    group_label = st.selectbox(
        "Group performance by",
        ["Sport", "Market", "Subreddit", "Slip type", "Odds band"],
    )
    settled["odds_band"] = pd.cut(
        settled["quoted_odds_decimal"],
        bins=[0, 1.5, 2.0, 3.0, 5.0, float("inf")],
        labels=["1.01-1.49", "1.50-1.99", "2.00-2.99", "3.00-4.99", "5.00+"],
        right=False,
    ).astype("string").fillna("Unknown")
    group_column = {
        "Sport": "sport", "Market": "markets", "Subreddit": "subreddit",
        "Slip type": "bet_type", "Odds band": "odds_band",
    }[group_label]
    settled[group_column] = settled[group_column].fillna("Unknown")
    grouped = settled.groupby(group_column, dropna=False).agg(
        bets=("slip_id", "count"),
        stake=("stake_units", "sum"),
        profit=("profit_units", "sum"),
        wins=("status", lambda values: int((values == "WON").sum())),
        decisive=("status", lambda values: int(values.isin(["WON", "LOST"]).sum())),
        average_odds=("quoted_odds_decimal", "mean"),
    ).reset_index()
    grouped["roi"] = grouped["profit"] / grouped["stake"]
    grouped["hit_rate"] = grouped["wins"] / grouped["decisive"].replace(0, pd.NA)
    grouped = grouped.sort_values("profit", ascending=False)
    st.dataframe(
        grouped,
        hide_index=True,
        width="stretch",
        column_config={
            "stake": st.column_config.NumberColumn(format="%.1f u"),
            "profit": st.column_config.NumberColumn(format="%+.2f u"),
            "roi": st.column_config.NumberColumn(format="%.1%%"),
            "hit_rate": st.column_config.NumberColumn(format="%.1%%"),
            "average_odds": st.column_config.NumberColumn(format="%.3f"),
        },
    )
    figure = px.bar(
        grouped,
        x=group_column,
        y="profit",
        color=group_column,
        text="bets",
    )
    figure.update_layout(
        title=f"Realised P/L by {group_label.lower()}",
        xaxis_title=None,
        yaxis_title="Units",
        showlegend=False,
        margin=dict(l=20, r=20, t=55, b=20),
    )
    st.plotly_chart(figure, width="stretch")


def _tipsters(frame: pd.DataFrame) -> None:
    if frame.empty:
        st.info("Tipster records appear after bets settle.")
        return
    settled = frame[frame["status"].isin(["WON", "LOST", "PUSH", "VOID"])].copy()
    if settled.empty:
        st.info("Tipster records appear after bets settle.")
        return
    minimum = st.slider("Minimum settled slips", min_value=1, max_value=max(1, min(50, len(settled))), value=min(5, len(settled)))
    grouped = settled.groupby(["subreddit", "author"], dropna=False).agg(
        bets=("slip_id", "count"),
        stake=("stake_units", "sum"),
        profit=("profit_units", "sum"),
        wins=("status", lambda values: int((values == "WON").sum())),
        losses=("status", lambda values: int((values == "LOST").sum())),
        average_odds=("quoted_odds_decimal", "mean"),
    ).reset_index()
    grouped = grouped[grouped["bets"] >= minimum]
    grouped["roi"] = grouped["profit"] / grouped["stake"]
    grouped["hit_rate"] = grouped["wins"] / (grouped["wins"] + grouped["losses"]).replace(0, pd.NA)
    grouped = grouped.sort_values(["roi", "bets"], ascending=[False, False])
    st.dataframe(
        grouped,
        hide_index=True,
        width="stretch",
        column_config={
            "profit": st.column_config.NumberColumn(format="%+.2f u"),
            "roi": st.column_config.NumberColumn(format="%.1%%"),
            "hit_rate": st.column_config.NumberColumn(format="%.1%%"),
            "average_odds": st.column_config.NumberColumn(format="%.3f"),
        },
    )
    st.caption(
        "Leaderboards create selection bias: with many tipsters, someone can look exceptional by chance. Treat small records as leads for investigation, not evidence of an edge."
    )


def _review(db: Database, settings) -> None:
    pending = db.review_slips()
    if not pending:
        st.success("Review queue is empty.")
        return
    options = {int(row["id"]): row for row in pending}
    slip_id = st.selectbox(
        "Pending item",
        list(options),
        format_func=lambda value: f"#{value} · {options[value]['status']} · {options[value]['description'][:80]}",
    )
    row = options[int(slip_id)]
    st.markdown(
        f"**r/{row['subreddit']} · u/{row['author']}**  |  [open source]({row['permalink']})"
    )
    st.text_area("Immutable first-seen snapshot", row["body_original"], height=200, disabled=True)
    legs = db.get_legs(int(slip_id))
    if legs:
        st.dataframe(
            pd.DataFrame(legs)[
                ["leg_no", "event_text", "selection", "market_type", "event_start_at", "verification_status", "validation_notes_json"]
            ],
            hide_index=True,
            width="stretch",
        )

    if row["status"] == "NEEDS_SETTLEMENT":
        with st.form(f"settle_{slip_id}"):
            result = st.selectbox("Final result", ["WON", "LOST", "PUSH", "VOID"])
            note = st.text_input("Settlement evidence/note")
            settle = st.form_submit_button("Save manual settlement", type="primary")
        if settle:
            if not note.strip():
                st.error("Add a short evidence note before manually settling.")
            else:
                db.manual_settle_slip(int(slip_id), result, note.strip())
                st.rerun()
        return

    with st.form(f"review_{slip_id}"):
        description = st.text_input("Bet description", value=row["description"])
        odds = st.number_input(
            "Quoted decimal odds", min_value=1.01, max_value=1001.0,
            value=float(row["quoted_odds_decimal"] or 2.0), step=0.01,
        )
        bet_type = st.selectbox("Slip type", ["SINGLE", "PARLAY"], index=0 if row["bet_type"] == "SINGLE" else 1)
        sport = st.text_input("Sport", value=row["sport"] or "")
        league = st.text_input("League", value=row["league"] or "")
        start_text = st.text_input(
            "Earliest event start (UTC ISO 8601)", value=row["event_start_at"] or ""
        )
        note = st.text_input("Review note", value=row["review_note"] or "")
        save = st.form_submit_button("Save review edits")
        place = st.form_submit_button("Place fixed-unit paper bet", type="primary")
        reject = st.form_submit_button("Reject candidate")
    if save or place:
        try:
            start = parse_utc(start_text)
            if start is None:
                raise ValueError("Start time is required")
            db.edit_review_slip(
                int(slip_id), description=description.strip(), quoted_odds_decimal=float(odds),
                bet_type=bet_type, sport=sport.strip() or None, league=league.strip() or None,
                event_start_at=start.isoformat().replace("+00:00", "Z"), review_note=note.strip() or None,
            )
            if place:
                ok, message = db.approve_slip(
                    int(slip_id), verification_status="MANUAL_VERIFIED",
                    review_note=note.strip() or "Manually reviewed against a future event.",
                    minimum_lead_minutes=settings.minimum_lead_minutes,
                )
                (st.success if ok else st.error)(message)
            if save or place:
                st.rerun()
        except ValueError as exc:
            st.error(str(exc))
    if reject:
        db.reject_slip(int(slip_id), note.strip() or "Rejected during manual review.")
        st.rerun()


def _rejected_feed(db: Database) -> None:
    source_rows = db.query(
        """
        SELECT 'Source' AS record_type, 'source-' || src.id AS record_id,
               src.collected_at, src.subreddit, src.author, src.parent_title,
               src.rejection_reason AS reason, src.permalink, src.body_original
        FROM source_items src
        WHERE src.processing_state='REJECTED'
        """
    )
    slip_rows = db.query(
        """
        SELECT 'Slip' AS record_type, 'slip-' || s.id AS record_id,
               src.collected_at, src.subreddit, src.author, src.parent_title,
               COALESCE(s.review_note, 'Candidate rejected during review or after its event started') AS reason,
               src.permalink, src.body_original
        FROM slips s JOIN source_items src ON src.id=s.source_id
        WHERE s.status='REJECTED'
        """
    )
    frame = pd.DataFrame(source_rows + slip_rows)
    if frame.empty:
        st.success("Nothing has been rejected yet.")
        return
    frame["collected_at"] = pd.to_datetime(frame["collected_at"], utc=True, errors="coerce")
    frame = frame.sort_values("collected_at", ascending=False)

    st.caption(
        "Audit trail for posts that were not parseable and candidates rejected before paper placement. "
        "These records never contribute to P/L."
    )
    reason_counts = (
        frame.assign(reason=frame["reason"].fillna("Unspecified"))
        .groupby("reason", dropna=False)
        .size()
        .sort_values(ascending=True)
        .rename("count")
        .reset_index()
    )
    figure = px.bar(reason_counts, x="count", y="reason", orientation="h")
    figure.update_layout(
        title="Why records were rejected", xaxis_title="Records", yaxis_title=None,
        margin=dict(l=20, r=20, t=55, b=20),
    )
    st.plotly_chart(figure, width="stretch")

    display = frame[
        ["record_id", "record_type", "collected_at", "subreddit", "author", "reason", "permalink"]
    ].rename(
        columns={
            "record_id": "ID", "record_type": "Type", "collected_at": "Collected (UTC)",
            "subreddit": "Subreddit", "author": "Author", "reason": "Reason",
            "permalink": "Source",
        }
    )
    st.dataframe(
        display,
        hide_index=True,
        width="stretch",
        column_config={"Source": st.column_config.LinkColumn("Source", display_text="Open Reddit")},
    )
    choices = frame["record_id"].tolist()
    selected = st.selectbox("Inspect rejected snapshot", choices, key="rejected_snapshot")
    row = frame.loc[frame["record_id"] == selected].iloc[0]
    st.markdown(f"**{row['record_type']} {row['record_id']}** - {row['reason'] or 'Unspecified'}")
    st.text_area(
        "Immutable first-seen Reddit snapshot",
        str(row["body_original"]),
        height=200,
        disabled=True,
        key="rejected_snapshot_text",
    )


def _operations(db: Database, settings, metadata: dict[str, str]) -> None:
    left, right = st.columns(2)
    with left:
        st.subheader("Connections")
        st.write(f"Reddit API approval: **{'confirmed' if settings.reddit_api_approved else 'not confirmed'}**")
        st.write(f"Reddit OAuth: **{'ready' if settings.reddit_ready else 'locked or not configured'}**")
        st.write(f"Raw Reddit retention: **{settings.reddit_raw_retention_hours} hours**")
        st.write(f"Event/odds feed: **{'ready' if settings.odds_ready else 'not configured'}**")
        remaining = metadata.get("odds_api_x-requests-remaining")
        if remaining is not None:
            st.write(f"Odds API credits remaining: **{remaining}**")
    with right:
        st.subheader("Run now")
        experiment = Experiment(settings, db)
        collect = st.button("Collect new Reddit picks", disabled=not settings.reddit_ready)
        validate = st.button("Retry review validation", disabled=not settings.odds_ready)
        settle = st.button("Settle completed events", disabled=not settings.odds_ready)
        if collect:
            with st.spinner("Collecting and validating..."):
                st.json(experiment.collect().as_dict())
        if validate:
            with st.spinner("Validating pending slips..."):
                st.json(experiment.validate_pending().as_dict())
        if settle:
            with st.spinner("Checking results..."):
                st.json(experiment.settle().as_dict())

    st.subheader("Data-quality audit")
    counts = db.query(
        """
        SELECT
          (SELECT COUNT(*) FROM source_items) AS source_snapshots,
          (SELECT COUNT(*) FROM source_revisions) AS later_edits_observed,
          (SELECT COUNT(*) FROM source_items WHERE processing_state='REJECTED') AS source_rejections,
          (SELECT COUNT(*) FROM slips) AS parsed_slips,
          (SELECT COUNT(*) FROM slips WHERE placed_at IS NOT NULL) AS paper_bets_placed,
          (SELECT COUNT(*) FROM slips WHERE status='REVIEW') AS awaiting_review
        """
    )[0]
    st.dataframe(pd.DataFrame([counts]), hide_index=True, width="stretch")

    st.subheader("Recent runs")
    runs = pd.DataFrame(
        db.query(
            "SELECT id, run_type, started_at, ended_at, status, counters_json, errors_json FROM runs ORDER BY id DESC LIMIT 25"
        )
    )
    if runs.empty:
        st.info("No collector or settlement runs recorded yet.")
    else:
        st.dataframe(runs, hide_index=True, width="stretch")


def _units(value: float | int | None, signed: bool = False) -> str:
    if value is None:
        return "—"
    return f"{float(value):+.2f} u" if signed else f"{float(value):.2f} u"


def _percent(value: float | None) -> str:
    return "—" if value is None or pd.isna(value) else f"{value:.1%}"
