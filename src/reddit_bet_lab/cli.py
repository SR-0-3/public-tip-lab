from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .config import PROJECT_ROOT, Settings, load_settings
from .db import Database
from .demo import seed_demo
from .pipeline import Experiment


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="public-tip-lab",
        description="Forward-test public Reddit betting tips with fixed paper units.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        help="Override the SQLite database path for this command.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create the database schema.")
    commands.add_parser("doctor", help="Check configuration without exposing secrets.")
    commands.add_parser("collect", help="Collect, parse, validate, and paper-place new picks.")
    commands.add_parser("validate", help="Retry validation of review-queue picks.")
    commands.add_parser("settle", help="Update in-play states and settle completed events.")
    commands.add_parser("daily", help="Run collection followed by settlement.")

    watch = commands.add_parser("watch", help="Run the forward test continuously while this PC is on.")
    watch.add_argument("--minutes", type=int, help="Collection interval; defaults to .env value.")

    export = commands.add_parser("export", help="Export the complete paper-bet ledger to CSV.")
    export.add_argument("--out", type=Path, help="Output CSV path.")

    demo = commands.add_parser("seed-demo", help="Create a separate demonstration database.")
    demo.add_argument("--force", action="store_true", help="Replace only the selected demo database if it exists.")

    commands.add_parser("dashboard", help="Launch the Streamlit dashboard.")
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    settings = load_settings()
    if args.db:
        path = args.db
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        settings = replace(settings, db_path=path.resolve())
    return settings


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = _settings(args)
    db = Database(settings.db_path)

    if args.command == "init":
        db.initialize(settings.starting_bank_units)
        print(f"Initialized {settings.db_path}")
        return 0
    if args.command == "doctor":
        return doctor(settings)
    if args.command == "seed-demo":
        demo_path = settings.db_path
        if args.db is None:
            demo_path = PROJECT_ROOT / "data" / "demo.sqlite3"
        seed_demo(demo_path, settings.starting_bank_units, replace=args.force)
        print(f"Demo database created at {demo_path}")
        return 0
    if args.command == "dashboard":
        return launch_dashboard(settings)

    experiment = Experiment(settings, db)
    if args.command == "collect":
        return _print_summary(experiment.collect())
    if args.command == "validate":
        return _print_summary(experiment.validate_pending())
    if args.command == "settle":
        return _print_summary(experiment.settle())
    if args.command == "daily":
        collect, settle = experiment.daily()
        code_a = _print_summary(collect)
        code_b = _print_summary(settle)
        return max(code_a, code_b)
    if args.command == "export":
        return export_csv(db, args.out)
    if args.command == "watch":
        return watch(experiment, args.minutes or settings.collect_interval_minutes)
    return 2


def doctor(settings: Settings) -> int:
    Database(settings.db_path).initialize(settings.starting_bank_units)
    checks = {
        "python": sys.version.split()[0],
        "database": str(settings.db_path),
        "database_writable": os.access(settings.db_path.parent, os.W_OK),
        "reddit_credentials_present": bool(
            settings.reddit_client_id and settings.reddit_client_secret
        ),
        "reddit_api_approval_confirmed": settings.reddit_api_approved,
        "reddit_collection_ready": settings.reddit_ready,
        "reddit_raw_retention_hours": settings.reddit_raw_retention_hours,
        "odds_api_key_configured": settings.odds_ready,
        "streamlit_installed": importlib.util.find_spec("streamlit") is not None,
        "pandas_installed": importlib.util.find_spec("pandas") is not None,
        "plotly_installed": importlib.util.find_spec("plotly") is not None,
        "subreddits": settings.subreddits,
        "stake_units_per_slip": settings.stake_units,
        "starting_bank_units": settings.starting_bank_units,
    }
    print(json.dumps(checks, indent=2, default=str))
    ready = all(
        checks[key]
        for key in (
            "database_writable",
            "reddit_collection_ready",
            "odds_api_key_configured",
            "streamlit_installed",
            "pandas_installed",
            "plotly_installed",
        )
    )
    return 0 if ready else 1


def export_csv(db: Database, output: Path | None) -> int:
    rows = db.export_rows()
    if output is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output = PROJECT_ROOT / "exports" / f"paper-bets-{stamp}.csv"
    elif not output.is_absolute():
        output = PROJECT_ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else ["slip_id"]
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Exported {len(rows)} rows to {output}")
    return 0


def watch(experiment: Experiment, minutes: int) -> int:
    if minutes < 5:
        print("Refusing an interval below 5 minutes; this protects API quotas.", file=sys.stderr)
        return 2
    print(f"Forward test running every {minutes} minutes. Press Ctrl+C to stop.")
    settle_every = max(1, math.ceil(240 / minutes))
    cycle = 0
    try:
        while True:
            _print_summary(experiment.collect())
            if cycle % settle_every == 0:
                _print_summary(experiment.settle())
            cycle += 1
            time.sleep(minutes * 60)
    except KeyboardInterrupt:
        print("Stopped cleanly.")
        return 0


def launch_dashboard(settings: Settings) -> int:
    if importlib.util.find_spec("streamlit") is None:
        print("Streamlit is not installed. Run scripts/setup_windows.bat first.", file=sys.stderr)
        return 1
    environment = dict(os.environ)
    environment["DB_PATH"] = str(settings.db_path)
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(PROJECT_ROOT / "app.py"),
        "--server.headless=false",
    ]
    return subprocess.run(command, cwd=PROJECT_ROOT, env=environment, check=False).returncode


def _print_summary(summary) -> int:
    print(json.dumps(summary.as_dict(), indent=2, default=str))
    return 0 if summary.status.startswith("completed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
