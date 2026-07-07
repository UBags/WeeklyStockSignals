"""Tests for the V1 `backfill` CLI subcommand (Step 11b2).

Covers:
  - argparse: --date_from required (unless --release-lock alone);
    --date_to defaults to today; --stocks accepts both comma-separated
    and space-separated; --no_fetch_bars / --release-lock flags
  - Date parsing errors → exit 2 with clear stderr
  - Symbol resolution: missing / inactive / wrong-exchange symbols
    → exit 2 with which symbols failed
  - --release-lock with no args → cleanup mode, exits 0
  - --release-lock with --date_from → clears orphans then runs
  - IntegrityError on concurrent start → exit 1 with friendly message
  - Successful pipeline → exit 0 with summary

The pipeline itself is mocked (it has its own test suite); these
tests focus on the CLI wrapper's argparse + symbol resolution +
error handling.
"""
from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from trip_ema.backfill import BackfillResult
from trip_ema.etl.cli import cmd_backfill
from trip_ema.persistence import transaction
from trip_ema.persistence.backfill_runs_repo import (
    BackfillRunsRepository,
)


# ============================================================
# Helpers
# ============================================================

def _seed_ticker(
    db_engine, *, ticker_id: int, symbol: str,
    exchange: str = "NSE", is_active: bool = True,
) -> None:
    with transaction(db_engine) as conn:
        conn.execute(text("""
            INSERT INTO tickers (
                ticker_id, exchange, symbol, tier,
                is_active, delisted_at
            ) VALUES (:tid, :ex, :sym, 2, :ia, NULL)
            ON CONFLICT (ticker_id) DO NOTHING
        """), {
            "tid": ticker_id, "ex": exchange,
            "sym": symbol, "ia": is_active,
        })


def _make_args(**overrides):
    """Build an argparse.Namespace-like object with V1 backfill
    defaults filled in."""
    from types import SimpleNamespace
    defaults = {
        "date_from": None,
        "date_to": None,
        "stocks": None,
        "exchange": "NSE",
        "no_fetch_bars": False,
        "release_lock": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_result(**overrides) -> BackfillResult:
    """Build a successful BackfillResult for mock returns."""
    defaults = dict(
        run_id=1,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        date_from=date(2024, 6, 3),
        date_to=date(2024, 6, 3),
        from_monday=date(2024, 6, 3),
        to_monday=date(2024, 6, 3),
        exchange="NSE",
        ticker_scope_size=1,
        ticker_scope_label="ALL",
        refetch_bars=False,
        bars_fetch_run_id=None,
        stock_state_tickers_done=1,
        stock_state_failed=[],
        stock_enrichment_tickers_done=1,
        stock_enrichment_failed=[],
        iiv_industries_done=0,
        industry_state_done=0,
        industry_enrichment_done=0,
        historical_signals_deleted=0,
        historical_signals_inserted=0,
        current_signals_upserted=0,
        current_signals_deleted=0,
        days_walked=1,
        failed_stage=None,
        error_message=None,
    )
    defaults.update(overrides)
    return BackfillResult(**defaults)


class _MockConfig:
    """Minimal Config stand-in. The CLI passes it to create_engine_from_config
    (we monkeypatch that) and to the pipeline (we monkeypatch backfill)."""
    pass


# ============================================================
# --release-lock cleanup-only mode
# ============================================================

class TestReleaseLockCleanup:

    def test_release_lock_alone_no_active_rows(
        self, db_engine, capsys, monkeypatch,
    ):
        """--release-lock with no other args + no orphan rows →
        exit 0 with informative output."""
        monkeypatch.setattr(
            "trip_ema.etl.cli.create_engine_from_config",
            lambda cfg: db_engine,
        )
        rc = cmd_backfill(
            _make_args(release_lock=True), _MockConfig(),
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "no active marker rows" in out
        # Guidance about how to run a proper backfill should be present
        assert "--date_from" in out

    def test_release_lock_alone_clears_orphan(
        self, db_engine, capsys, monkeypatch,
    ):
        """--release-lock with no other args + 1 orphan row →
        clears it, exits 0."""
        with transaction(db_engine) as conn:
            BackfillRunsRepository(conn).start(
                pid=12345, exchange="NSE",
                date_from=date(2024, 1, 1),
                date_to=date(2024, 6, 30),
                refetch_bars=True,
                ticker_scope="ALL",
            )

        monkeypatch.setattr(
            "trip_ema.etl.cli.create_engine_from_config",
            lambda cfg: db_engine,
        )
        rc = cmd_backfill(
            _make_args(release_lock=True), _MockConfig(),
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "cleared 1 orphan" in out

        # Marker is now finished
        with db_engine.connect() as conn:
            assert not BackfillRunsRepository(conn).is_running()


# ============================================================
# date_from required
# ============================================================

class TestDateFromRequired:

    def test_no_date_from_no_release_lock_errors(
        self, db_engine, capsys, monkeypatch,
    ):
        monkeypatch.setattr(
            "trip_ema.etl.cli.create_engine_from_config",
            lambda cfg: db_engine,
        )
        rc = cmd_backfill(_make_args(), _MockConfig())
        assert rc == 2
        err = capsys.readouterr().err
        assert "--date_from is required" in err

    def test_invalid_date_from_errors(
        self, db_engine, capsys, monkeypatch,
    ):
        monkeypatch.setattr(
            "trip_ema.etl.cli.create_engine_from_config",
            lambda cfg: db_engine,
        )
        rc = cmd_backfill(
            _make_args(date_from="not-a-date"), _MockConfig(),
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "YYYY-MM-DD" in err

    def test_invalid_date_to_errors(
        self, db_engine, capsys, monkeypatch,
    ):
        monkeypatch.setattr(
            "trip_ema.etl.cli.create_engine_from_config",
            lambda cfg: db_engine,
        )
        rc = cmd_backfill(
            _make_args(
                date_from="2024-06-03", date_to="bogus",
            ),
            _MockConfig(),
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "--date_to" in err and "YYYY-MM-DD" in err


# ============================================================
# Symbol resolution
# ============================================================

class TestSymbolResolution:

    def test_comma_separated_stocks_resolved(
        self, db_engine, capsys, monkeypatch,
    ):
        _seed_ticker(db_engine, ticker_id=10, symbol="ABC")
        _seed_ticker(db_engine, ticker_id=20, symbol="XYZ")

        captured_stocks = []

        def _spy_backfill(*args, **kwargs):
            captured_stocks.append(kwargs.get("stocks"))
            return _make_result(
                ticker_scope_size=2, ticker_scope_label="10,20",
            )

        monkeypatch.setattr(
            "trip_ema.etl.cli.create_engine_from_config",
            lambda cfg: db_engine,
        )
        monkeypatch.setattr("trip_ema.backfill.backfill", _spy_backfill)
        monkeypatch.setattr(
            "trip_ema.backfill.backfill", _spy_backfill,
            raising=False,
        )

        rc = cmd_backfill(
            _make_args(
                date_from="2024-06-03",
                stocks=["ABC,XYZ"],
            ),
            _MockConfig(),
        )
        assert rc == 0
        assert captured_stocks == [[10, 20]]

    def test_space_separated_stocks_resolved(
        self, db_engine, capsys, monkeypatch,
    ):
        _seed_ticker(db_engine, ticker_id=10, symbol="ABC")
        _seed_ticker(db_engine, ticker_id=20, symbol="XYZ")

        captured_stocks = []

        def _spy_backfill(*args, **kwargs):
            captured_stocks.append(kwargs.get("stocks"))
            return _make_result(
                ticker_scope_size=2, ticker_scope_label="10,20",
            )

        monkeypatch.setattr(
            "trip_ema.etl.cli.create_engine_from_config",
            lambda cfg: db_engine,
        )
        monkeypatch.setattr(
            "trip_ema.backfill.backfill", _spy_backfill,
            raising=False,
        )

        rc = cmd_backfill(
            _make_args(
                date_from="2024-06-03",
                # nargs='+' would produce a list of separate args
                stocks=["ABC", "XYZ"],
            ),
            _MockConfig(),
        )
        assert rc == 0
        assert captured_stocks == [[10, 20]]

    def test_mixed_comma_and_space(
        self, db_engine, monkeypatch,
    ):
        """'--stocks A,B C' → 3 symbols A, B, C."""
        _seed_ticker(db_engine, ticker_id=10, symbol="A")
        _seed_ticker(db_engine, ticker_id=20, symbol="B")
        _seed_ticker(db_engine, ticker_id=30, symbol="C")

        captured = []

        def _spy(*args, **kwargs):
            captured.append(kwargs.get("stocks"))
            return _make_result(ticker_scope_size=3)

        monkeypatch.setattr(
            "trip_ema.etl.cli.create_engine_from_config",
            lambda cfg: db_engine,
        )
        monkeypatch.setattr(
            "trip_ema.backfill.backfill", _spy, raising=False,
        )

        rc = cmd_backfill(
            _make_args(
                date_from="2024-06-03",
                stocks=["A,B", "C"],
            ),
            _MockConfig(),
        )
        assert rc == 0
        assert captured == [[10, 20, 30]]

    def test_missing_symbol_errors(
        self, db_engine, capsys, monkeypatch,
    ):
        _seed_ticker(db_engine, ticker_id=10, symbol="EXISTS")
        monkeypatch.setattr(
            "trip_ema.etl.cli.create_engine_from_config",
            lambda cfg: db_engine,
        )
        rc = cmd_backfill(
            _make_args(
                date_from="2024-06-03",
                stocks=["EXISTS,NOPE,ALSO_NOPE"],
            ),
            _MockConfig(),
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "not found" in err
        assert "NOPE" in err
        assert "ALSO_NOPE" in err

    def test_inactive_symbol_errors(
        self, db_engine, capsys, monkeypatch,
    ):
        _seed_ticker(
            db_engine, ticker_id=10, symbol="INA", is_active=False,
        )
        monkeypatch.setattr(
            "trip_ema.etl.cli.create_engine_from_config",
            lambda cfg: db_engine,
        )
        rc = cmd_backfill(
            _make_args(date_from="2024-06-03", stocks=["INA"]),
            _MockConfig(),
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "INA" in err

    def test_symbol_uppercased(
        self, db_engine, monkeypatch,
    ):
        """Lowercase input gets upper-cased before lookup."""
        _seed_ticker(db_engine, ticker_id=10, symbol="ABC")
        captured = []

        def _spy(*args, **kwargs):
            captured.append(kwargs.get("stocks"))
            return _make_result()

        monkeypatch.setattr(
            "trip_ema.etl.cli.create_engine_from_config",
            lambda cfg: db_engine,
        )
        monkeypatch.setattr(
            "trip_ema.backfill.backfill", _spy, raising=False,
        )
        rc = cmd_backfill(
            _make_args(
                date_from="2024-06-03",
                stocks=["abc"],
            ),
            _MockConfig(),
        )
        assert rc == 0
        assert captured == [[10]]


# ============================================================
# IntegrityError handling
# ============================================================

class TestIntegrityErrorHandling:

    def test_concurrent_start_friendly_message(
        self, db_engine, capsys, monkeypatch,
    ):
        _seed_ticker(db_engine, ticker_id=1, symbol="T1")

        def _raises(*args, **kwargs):
            # IntegrityError needs statement + params + orig
            raise IntegrityError("sim", {}, Exception("unique"))

        monkeypatch.setattr(
            "trip_ema.etl.cli.create_engine_from_config",
            lambda cfg: db_engine,
        )
        monkeypatch.setattr(
            "trip_ema.backfill.backfill", _raises, raising=False,
        )

        rc = cmd_backfill(
            _make_args(date_from="2024-06-03"),
            _MockConfig(),
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "another backfill" in err.lower()
        assert "--release-lock" in err


# ============================================================
# Successful run
# ============================================================

class TestSuccessfulRun:

    def test_returns_zero_on_success(
        self, db_engine, capsys, monkeypatch,
    ):
        _seed_ticker(db_engine, ticker_id=1, symbol="T1")
        monkeypatch.setattr(
            "trip_ema.etl.cli.create_engine_from_config",
            lambda cfg: db_engine,
        )
        monkeypatch.setattr(
            "trip_ema.backfill.backfill",
            lambda *a, **k: _make_result(),
            raising=False,
        )
        rc = cmd_backfill(
            _make_args(date_from="2024-06-03"),
            _MockConfig(),
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "backfill complete" in out

    def test_returns_one_on_failed_stage(
        self, db_engine, capsys, monkeypatch,
    ):
        _seed_ticker(db_engine, ticker_id=1, symbol="T1")
        monkeypatch.setattr(
            "trip_ema.etl.cli.create_engine_from_config",
            lambda cfg: db_engine,
        )
        monkeypatch.setattr(
            "trip_ema.backfill.backfill",
            lambda *a, **k: _make_result(
                failed_stage="stage_iiv",
                error_message="RuntimeError: sim",
            ),
            raising=False,
        )
        rc = cmd_backfill(
            _make_args(date_from="2024-06-03"),
            _MockConfig(),
        )
        assert rc == 1
        out = capsys.readouterr().out
        assert "backfill FAILED" in out
        assert "stage_iiv" in out


# ============================================================
# refetch_bars + release_lock flag propagation
# ============================================================

class TestFlagPropagation:

    def test_no_fetch_bars_passed_through(
        self, db_engine, monkeypatch,
    ):
        _seed_ticker(db_engine, ticker_id=1, symbol="T1")
        captured = []

        def _spy(*args, **kwargs):
            captured.append(kwargs.get("refetch_bars"))
            return _make_result(refetch_bars=False)

        monkeypatch.setattr(
            "trip_ema.etl.cli.create_engine_from_config",
            lambda cfg: db_engine,
        )
        monkeypatch.setattr(
            "trip_ema.backfill.backfill", _spy, raising=False,
        )
        cmd_backfill(
            _make_args(
                date_from="2024-06-03", no_fetch_bars=True,
            ),
            _MockConfig(),
        )
        assert captured == [False]

    def test_release_lock_with_date_from_continues(
        self, db_engine, capsys, monkeypatch,
    ):
        """--release-lock + --date_from: clears orphan THEN runs
        the backfill."""
        _seed_ticker(db_engine, ticker_id=1, symbol="T1")
        # Pre-seed an orphan marker
        with transaction(db_engine) as conn:
            BackfillRunsRepository(conn).start(
                pid=12345, exchange="NSE",
                date_from=date(2024, 1, 1),
                date_to=date(2024, 6, 30),
                refetch_bars=True,
                ticker_scope="ALL",
            )

        backfill_called = []

        def _spy(*args, **kwargs):
            backfill_called.append(True)
            return _make_result()

        monkeypatch.setattr(
            "trip_ema.etl.cli.create_engine_from_config",
            lambda cfg: db_engine,
        )
        monkeypatch.setattr(
            "trip_ema.backfill.backfill", _spy, raising=False,
        )

        rc = cmd_backfill(
            _make_args(
                date_from="2024-06-03", release_lock=True,
            ),
            _MockConfig(),
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "cleared 1 orphan" in out
        assert backfill_called == [True]
