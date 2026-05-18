"""Parked-failures storage.

When a ticker's consecutive_failures reaches the threshold (default 3),
the runner marks it `parked` in etl_ticker_status AND appends a row
to var/etl/parked/<YYYY-MM-DD>.csv. The CSV is the audit trail; the
DB column is the queryable state.

The CLI's `unpark` subcommand reads the latest parked CSV plus the
DB column, attempts to refetch each parked ticker, and clears the
parked state on success.

Why both file AND DB:
  - File: human-readable, survives DB wipes, easy to grep, easy to
    hand-edit for "just unpark these three" workflows.
  - DB: queryable from the GUI, gauges the parked count in Prometheus.

The two should not drift in practice, but on drift the DB wins for
"is this ticker parked right now"; the file is for audit.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True, slots=True)
class ParkedEntry:
    ticker_symbol: str
    ticker_id: int
    parked_at: datetime
    reason: str
    last_error: str


def parked_dir(base_dir: Path) -> Path:
    """`base_dir` is typically Config.etl.parked_dir (e.g. var/etl/parked).
    Creates the dir if it doesn't exist."""
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


def parked_file_for(today: date, base_dir: Path) -> Path:
    """One CSV per day. Multiple unpark attempts in the same day
    append to the same file."""
    return parked_dir(base_dir) / f"{today.isoformat()}.csv"


def append_parked(
    entry: ParkedEntry,
    today: date,
    base_dir: Path,
) -> Path:
    """Append a parked-ticker row to today's CSV. Creates the file
    with a header row if it doesn't exist."""
    path = parked_file_for(today, base_dir)
    write_header = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow([
                "ticker_symbol", "ticker_id", "parked_at",
                "reason", "last_error",
            ])
        w.writerow([
            entry.ticker_symbol, entry.ticker_id,
            entry.parked_at.isoformat(),
            entry.reason, entry.last_error.replace("\n", " "),
        ])
    return path


def read_parked(path: Path) -> list[ParkedEntry]:
    """Read a parked CSV produced by `append_parked`. Returns [] if
    the file doesn't exist."""
    if not path.exists():
        return []
    out: list[ParkedEntry] = []
    with path.open("r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                out.append(ParkedEntry(
                    ticker_symbol=row["ticker_symbol"],
                    ticker_id=int(row["ticker_id"]),
                    parked_at=datetime.fromisoformat(row["parked_at"]),
                    reason=row.get("reason", ""),
                    last_error=row.get("last_error", ""),
                ))
            except (KeyError, ValueError):
                # Skip malformed rows; don't fail the whole read
                continue
    return out


def all_parked_files(base_dir: Path) -> list[Path]:
    """Every parked CSV in the directory, sorted by name (= date desc
    is just reverse=True)."""
    if not base_dir.exists():
        return []
    return sorted(base_dir.glob("*.csv"))
