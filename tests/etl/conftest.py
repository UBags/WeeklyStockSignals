"""Reuse the persistence DB fixture for ETL tests.

The signal_generator runner (Phase 15a) writes to ohlcv_bars,
tickers, etl_runs, and signal_backtest_runs — exercising it
end-to-end requires a real, migrated Postgres. Borrow the
fixture infrastructure from tests/persistence/.

The existing tests in this directory (test_runner_*, test_reconciler,
etc.) don't pull a DB fixture today and remain unaffected — this
conftest only ADDS the fixture, doesn't constrain other tests.
"""

from tests.persistence.conftest import db_engine, postgresql, postgresql_proc

__all__ = ["db_engine", "postgresql", "postgresql_proc"]
