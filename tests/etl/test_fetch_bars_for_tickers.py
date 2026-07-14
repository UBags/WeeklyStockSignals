"""Tests for trip_ema.etl.runner.fetch_bars_for_tickers.

Step 11b1 added this as a partial-ticker counterpart to run_backfill.
Tests validate the input validation + DB resolution paths; the
actual yfinance fetch is mocked via YFinanceSource so the test
suite doesn't depend on network.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from trip_ema.config.schema import EtlConfig
from trip_ema.etl.runner import fetch_bars_for_tickers
from trip_ema.persistence import transaction


def _seed_ticker(
    db_engine, *, ticker_id: int, symbol: str,
    exchange: str = "NSE", tier: int = 2,
    is_active: bool = True,
) -> None:
    with transaction(db_engine) as conn:
        conn.execute(text("""
            INSERT INTO tickers (
                ticker_id, exchange, symbol, tier,
                is_active, delisted_at
            ) VALUES (:tid, :ex, :sym, :tier, :ia, NULL)
            ON CONFLICT (ticker_id) DO NOTHING
        """), {
            "tid": ticker_id, "ex": exchange, "sym": symbol,
            "tier": tier, "ia": is_active,
        })


class _MockConfig:
    """Config stand-in whose `etl` section is the REAL, validated EtlConfig.

    It used to hand-roll one:

        class etl:
            yfinance_batch_size = 50
            yfinance_concurrency = 4

    — two of EtlConfig's fifteen fields. The moment fetch_bars_for_tickers started
    reading yfinance_request_delay_seconds (which it had been ignoring, silently
    running at 0.0 against a configured 30.0), these tests died with
    "type object 'etl' has no attribute ...". Not bad luck: a hand-maintained copy
    of a schema is a drift generator, and it had thirteen more fields to trip over.

    EtlConfig's fields are all defaulted, so EtlConfig() constructs bare. Every
    field is now present, pydantic-validated, carrying the production default —
    and a field added by a future commit is simply there. This cannot drift again.

    The outer shell stays: the full Config requires database/ema/atr/market_hours,
    which these tests genuinely do not need. The shell was never the problem; the
    hand-rolled schema was.
    """
    etl = EtlConfig()


class TestFetchBarsForTickers:

    def test_empty_ticker_ids_raises(self, db_engine):
        with pytest.raises(ValueError, match="non-empty"):
            fetch_bars_for_tickers(
                db_engine, _MockConfig(),
                ticker_ids=[],
                start_date=date(2024, 6, 1),
            )

    def test_no_active_tickers_resolved_raises(self, db_engine):
        """ticker_ids that don't match any active rows raise."""
        with pytest.raises(ValueError, match="no active tickers"):
            fetch_bars_for_tickers(
                db_engine, _MockConfig(),
                ticker_ids=[999, 1000],
                start_date=date(2024, 6, 1),
            )

    def test_mixed_exchange_raises(self, db_engine):
        _seed_ticker(
            db_engine, ticker_id=1, symbol="NSE_T", exchange="NSE",
        )
        _seed_ticker(
            db_engine, ticker_id=2, symbol="BSE_T", exchange="BSE",
        )
        with pytest.raises(ValueError, match="multiple exchanges"):
            fetch_bars_for_tickers(
                db_engine, _MockConfig(),
                ticker_ids=[1, 2],
                start_date=date(2024, 6, 1),
            )

    def test_inactive_tickers_silently_dropped(self, db_engine):
        """Inactive tickers in the input list are silently filtered
        out — the fetch proceeds for the active subset."""
        _seed_ticker(
            db_engine, ticker_id=1, symbol="ACT", is_active=True,
        )
        _seed_ticker(
            db_engine, ticker_id=2, symbol="INA", is_active=False,
        )
        # Mock the executor so we observe what tickers it sees
        captured_tickers = []

        def _spy_execute_batches(*, tickers, **kwargs):
            captured_tickers.extend(tickers)
            from trip_ema.etl.types import RunOutcome
            # _execute_batches now returns (outcome, failed_tickers).
            return RunOutcome.SUCCESS, []

        run_id = fetch_bars_for_tickers(
            db_engine, _MockConfig(),
            ticker_ids=[1, 2],
            start_date=date(2024, 6, 1),
            end_date=date(2024, 6, 5),
            source=MagicMock(),
            execute_batches_fn=_spy_execute_batches,
            validate_exchange_fn=lambda *a, **k: None,
        )
        assert run_id > 0
        # Only the active ticker was passed through
        assert len(captured_tickers) == 1
        assert captured_tickers[0].ticker_id == 1

    def test_returns_etl_run_id(self, db_engine):
        _seed_ticker(db_engine, ticker_id=1, symbol="T1")

        def _stub_execute_batches(**kwargs):
            from trip_ema.etl.types import RunOutcome
            # _execute_batches now returns (outcome, failed_tickers).
            return RunOutcome.SUCCESS, []

        run_id = fetch_bars_for_tickers(
            db_engine, _MockConfig(),
            ticker_ids=[1],
            start_date=date(2024, 6, 1),
            source=MagicMock(),
            execute_batches_fn=_stub_execute_batches,
            validate_exchange_fn=lambda *a, **k: None,
        )
        # Verify the etl_runs row exists
        with db_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT kind, exchange FROM etl_runs
                 WHERE run_id = :rid
            """), {"rid": run_id}).first()
        assert row is not None
        assert row.exchange == "NSE"
