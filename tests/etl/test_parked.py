"""Tests for the parked-failures file management. Uses tmp_path for
isolated filesystem state."""

from __future__ import annotations

from datetime import date, datetime, timezone, UTC
from pathlib import Path

import pytest

from trip_ema.etl.parked import (
    ParkedEntry,
    all_parked_files,
    append_parked,
    parked_dir,
    parked_file_for,
    read_parked,
)


def _entry(symbol="RELIANCE", tid=1, reason="3 consecutive failures"):
    return ParkedEntry(
        ticker_symbol=symbol,
        ticker_id=tid,
        parked_at=datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC),
        reason=reason,
        last_error="yfinance returned empty DataFrame",
    )


class TestParkedFileManagement:

    def test_append_creates_file_with_header(self, tmp_path):
        path = append_parked(_entry(), date(2026, 5, 17), tmp_path)
        assert path.exists()
        text = path.read_text()
        # Header is the first line
        assert text.startswith("ticker_symbol,ticker_id,")
        # Then one data row
        assert "RELIANCE" in text

    def test_append_to_existing_file_no_duplicate_header(self, tmp_path):
        append_parked(_entry(symbol="A", tid=1), date(2026, 5, 17), tmp_path)
        append_parked(_entry(symbol="B", tid=2), date(2026, 5, 17), tmp_path)
        path = parked_file_for(date(2026, 5, 17), tmp_path)
        lines = path.read_text().strip().split("\n")
        # 1 header + 2 data rows
        assert len(lines) == 3
        # Only one header
        assert lines[0].startswith("ticker_symbol")
        assert "ticker_symbol" not in lines[1]
        assert "ticker_symbol" not in lines[2]

    def test_read_parked_round_trip(self, tmp_path):
        append_parked(_entry(symbol="X", tid=42), date(2026, 5, 17), tmp_path)
        entries = read_parked(parked_file_for(date(2026, 5, 17), tmp_path))
        assert len(entries) == 1
        assert entries[0].ticker_symbol == "X"
        assert entries[0].ticker_id == 42

    def test_read_nonexistent_file_returns_empty(self, tmp_path):
        path = tmp_path / "doesnt_exist.csv"
        assert read_parked(path) == []

    def test_separate_dates_separate_files(self, tmp_path):
        """Multiple days' parked entries get separate files."""
        append_parked(_entry(symbol="A"), date(2026, 5, 17), tmp_path)
        append_parked(_entry(symbol="B"), date(2026, 5, 18), tmp_path)
        files = all_parked_files(tmp_path)
        assert len(files) == 2
        # Sorted ascending by name (date order)
        assert files[0].name == "2026-05-17.csv"
        assert files[1].name == "2026-05-18.csv"

    def test_all_parked_files_when_dir_doesnt_exist(self, tmp_path):
        missing = tmp_path / "nope"
        assert all_parked_files(missing) == []

    def test_error_message_with_newlines_is_sanitized(self, tmp_path):
        """A last_error with embedded newlines would break CSV parsing.
        We replace them with spaces on write."""
        entry = ParkedEntry(
            ticker_symbol="MULTI", ticker_id=1,
            parked_at=datetime(2026, 5, 17, tzinfo=UTC),
            reason="test",
            last_error="line1\nline2\nline3",
        )
        path = append_parked(entry, date(2026, 5, 17), tmp_path)
        entries = read_parked(path)
        assert "\n" not in entries[0].last_error
        assert entries[0].last_error == "line1 line2 line3"
