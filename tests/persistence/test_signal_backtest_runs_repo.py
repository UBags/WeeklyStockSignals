"""Tests for SignalBacktestRunsRepository (Phase 15a).

Uses the shared `db_engine` fixture in conftest.py — fresh, migrated
Postgres database per test.

The 0005 migration adds the table but doesn't seed any rows; the
tickers table is also empty, so each test sets up its own tickers
via direct INSERTs.

Coverage:
  - upsert insert path (new row created)
  - upsert update path (existing row's computed_through_date advances,
    computed_from_date preserved)
  - get for present and absent rows
  - get_for_exchange ordering and scoping
  - delete_by_exchange for force_full
  - CHECK constraint enforcement (timeframe and date ordering)
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from trip_ema.domain import Timeframe
from trip_ema.persistence.signal_backtest_runs_repo import (
    SignalBacktestRun,
    SignalBacktestRunsRepository,
)


def _insert_ticker(
    conn, *, exchange: str = "NSE", symbol: str = "RELIANCE",
) -> int:
    """Insert a ticker row and return its ticker_id. We can't use the
    SymbolHistory or full tickers_repo path here because those carry
    extra responsibilities; for repo tests we want a minimal row."""
    return conn.execute(
        text("""
            INSERT INTO tickers (exchange, symbol, instrument)
            VALUES (:ex, :sym, 'EQ')
            RETURNING ticker_id
        """),
        {"ex": exchange, "sym": symbol},
    ).scalar_one()


# ---------- get on empty / present ---------- #


class TestGet:
    def test_get_returns_none_for_missing_row(self, db_engine):
        with db_engine.begin() as conn:
            ticker_id = _insert_ticker(conn)
        with db_engine.connect() as conn:
            row = SignalBacktestRunsRepository(conn).get(
                exchange="NSE",
                ticker_id=ticker_id,
                timeframe=Timeframe.WEEKLY,
            )
        assert row is None

    def test_get_returns_inserted_row(self, db_engine):
        with db_engine.begin() as conn:
            ticker_id = _insert_ticker(conn)
            SignalBacktestRunsRepository(conn).upsert(
                exchange="NSE",
                ticker_id=ticker_id,
                timeframe=Timeframe.WEEKLY,
                computed_from_date=date(2020, 1, 1),
                computed_through_date=date(2024, 12, 31),
                last_run_id=42,
            )
        with db_engine.connect() as conn:
            row = SignalBacktestRunsRepository(conn).get(
                exchange="NSE",
                ticker_id=ticker_id,
                timeframe=Timeframe.WEEKLY,
            )
        assert isinstance(row, SignalBacktestRun)
        assert row.exchange == "NSE"
        assert row.ticker_id == ticker_id
        assert row.timeframe == Timeframe.WEEKLY
        assert row.computed_from_date == date(2020, 1, 1)
        assert row.computed_through_date == date(2024, 12, 31)
        assert row.last_run_id == 42

    def test_get_does_not_cross_timeframe(self, db_engine):
        """A (ticker, '1W') row must not satisfy a get for
        (ticker, '1D'). The primary key includes timeframe so the
        repo's get must respect that."""
        with db_engine.begin() as conn:
            ticker_id = _insert_ticker(conn)
            SignalBacktestRunsRepository(conn).upsert(
                exchange="NSE",
                ticker_id=ticker_id,
                timeframe=Timeframe.WEEKLY,
                computed_from_date=date(2020, 1, 1),
                computed_through_date=date(2024, 12, 31),
            )
        with db_engine.connect() as conn:
            daily = SignalBacktestRunsRepository(conn).get(
                exchange="NSE",
                ticker_id=ticker_id,
                timeframe=Timeframe.DAILY,
            )
        assert daily is None


# ---------- upsert ---------- #


class TestUpsert:
    def test_insert_creates_new_row(self, db_engine):
        with db_engine.begin() as conn:
            ticker_id = _insert_ticker(conn)
            SignalBacktestRunsRepository(conn).upsert(
                exchange="NSE",
                ticker_id=ticker_id,
                timeframe=Timeframe.WEEKLY,
                computed_from_date=date(2020, 1, 1),
                computed_through_date=date(2024, 12, 31),
            )
        with db_engine.connect() as conn:
            rows = list(conn.execute(text(
                "SELECT exchange, ticker_id, timeframe, computed_from_date, "
                "computed_through_date FROM signal_backtest_runs"
            )))
        assert len(rows) == 1

    def test_update_advances_through_date_and_preserves_from_date(
        self, db_engine,
    ):
        """The COALESCE semantics on computed_from_date are
        non-obvious — they're spelled out in the repo docstring as
        'preserve the original from-date' on update. This is the
        invariant test."""
        with db_engine.begin() as conn:
            ticker_id = _insert_ticker(conn)
            repo = SignalBacktestRunsRepository(conn)
            # First insert
            repo.upsert(
                exchange="NSE",
                ticker_id=ticker_id,
                timeframe=Timeframe.WEEKLY,
                computed_from_date=date(2020, 1, 1),
                computed_through_date=date(2023, 12, 31),
                last_run_id=10,
            )
            # Incremental run — extends through_date forward
            repo.upsert(
                exchange="NSE",
                ticker_id=ticker_id,
                timeframe=Timeframe.WEEKLY,
                computed_from_date=date(2024, 1, 1),  # this is IGNORED on update
                computed_through_date=date(2024, 12, 31),
                last_run_id=11,
            )
        with db_engine.connect() as conn:
            row = SignalBacktestRunsRepository(conn).get(
                exchange="NSE",
                ticker_id=ticker_id,
                timeframe=Timeframe.WEEKLY,
            )
        # from_date is the ORIGINAL
        assert row.computed_from_date == date(2020, 1, 1)
        # through_date is the NEW (advances forward)
        assert row.computed_through_date == date(2024, 12, 31)
        # last_run_id reflects the latest run
        assert row.last_run_id == 11

    def test_upsert_for_distinct_timeframes_keeps_both_rows(self, db_engine):
        """Same (exchange, ticker), different timeframes → two rows."""
        with db_engine.begin() as conn:
            ticker_id = _insert_ticker(conn)
            repo = SignalBacktestRunsRepository(conn)
            repo.upsert(
                exchange="NSE",
                ticker_id=ticker_id,
                timeframe=Timeframe.WEEKLY,
                computed_from_date=date(2020, 1, 1),
                computed_through_date=date(2024, 12, 31),
            )
            repo.upsert(
                exchange="NSE",
                ticker_id=ticker_id,
                timeframe=Timeframe.DAILY,
                computed_from_date=date(2020, 1, 1),
                computed_through_date=date(2024, 12, 31),
            )
        with db_engine.connect() as conn:
            rows = SignalBacktestRunsRepository(conn).get_for_exchange("NSE")
        assert len(rows) == 2
        tfs = sorted(r.timeframe.value for r in rows)
        assert tfs == ["1D", "1W"]


# ---------- get_for_exchange ---------- #


class TestGetForExchange:
    def test_returns_all_rows_for_exchange(self, db_engine):
        with db_engine.begin() as conn:
            t1 = _insert_ticker(conn, symbol="RELIANCE")
            t2 = _insert_ticker(conn, symbol="TCS")
            repo = SignalBacktestRunsRepository(conn)
            for tid in (t1, t2):
                repo.upsert(
                    exchange="NSE",
                    ticker_id=tid,
                    timeframe=Timeframe.WEEKLY,
                    computed_from_date=date(2020, 1, 1),
                    computed_through_date=date(2024, 12, 31),
                )
        with db_engine.connect() as conn:
            rows = SignalBacktestRunsRepository(conn).get_for_exchange("NSE")
        assert len(rows) == 2
        # Ordered by (ticker_id, timeframe)
        assert [r.ticker_id for r in rows] == sorted([t1, t2])

    def test_other_exchanges_not_returned(self, db_engine):
        with db_engine.begin() as conn:
            t_nse = _insert_ticker(conn, exchange="NSE", symbol="X")
            t_nyse = _insert_ticker(conn, exchange="NYSE", symbol="X")
            repo = SignalBacktestRunsRepository(conn)
            for ex, tid in (("NSE", t_nse), ("NYSE", t_nyse)):
                repo.upsert(
                    exchange=ex,
                    ticker_id=tid,
                    timeframe=Timeframe.WEEKLY,
                    computed_from_date=date(2020, 1, 1),
                    computed_through_date=date(2024, 12, 31),
                )
        with db_engine.connect() as conn:
            nse_rows = SignalBacktestRunsRepository(conn).get_for_exchange("NSE")
            nyse_rows = SignalBacktestRunsRepository(conn).get_for_exchange("NYSE")
        assert len(nse_rows) == 1 and nse_rows[0].exchange == "NSE"
        assert len(nyse_rows) == 1 and nyse_rows[0].exchange == "NYSE"

    def test_empty_when_no_rows(self, db_engine):
        with db_engine.connect() as conn:
            rows = SignalBacktestRunsRepository(conn).get_for_exchange("NSE")
        assert rows == []


# ---------- delete_by_exchange (force_full path) ---------- #


class TestDeleteByExchange:
    def test_wipes_only_target_exchange(self, db_engine):
        """force_full's contract: clear THIS exchange's resume rows,
        leave other exchanges untouched."""
        with db_engine.begin() as conn:
            t_nse = _insert_ticker(conn, exchange="NSE", symbol="X")
            t_nyse = _insert_ticker(conn, exchange="NYSE", symbol="X")
            repo = SignalBacktestRunsRepository(conn)
            for ex, tid in (("NSE", t_nse), ("NYSE", t_nyse)):
                repo.upsert(
                    exchange=ex,
                    ticker_id=tid,
                    timeframe=Timeframe.WEEKLY,
                    computed_from_date=date(2020, 1, 1),
                    computed_through_date=date(2024, 12, 31),
                )
        with db_engine.begin() as conn:
            deleted = SignalBacktestRunsRepository(conn).delete_by_exchange("NSE")
        assert deleted == 1
        with db_engine.connect() as conn:
            assert SignalBacktestRunsRepository(conn).get_for_exchange("NSE") == []
            nyse_remaining = SignalBacktestRunsRepository(conn).get_for_exchange("NYSE")
            assert len(nyse_remaining) == 1

    def test_delete_when_no_rows_is_zero(self, db_engine):
        with db_engine.begin() as conn:
            deleted = SignalBacktestRunsRepository(conn).delete_by_exchange("NSE")
        assert deleted == 0


# ---------- CHECK constraints ---------- #


class TestCheckConstraints:
    def test_through_date_must_be_ge_from_date(self, db_engine):
        """The check constraint
        `signal_backtest_runs_date_order_chk` rejects
        through < from at the DB level."""
        with db_engine.begin() as conn:
            ticker_id = _insert_ticker(conn)
        with pytest.raises(IntegrityError):
            with db_engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO signal_backtest_runs
                        (exchange, ticker_id, timeframe,
                         computed_from_date, computed_through_date)
                    VALUES (:ex, :tid, '1W', :from_, :thru)
                """), {
                    "ex": "NSE", "tid": ticker_id,
                    "from_": date(2024, 12, 31),
                    "thru": date(2020, 1, 1),
                })

    def test_timeframe_must_be_1d_or_1w(self, db_engine):
        """The check constraint `signal_backtest_runs_timeframe_chk`
        rejects timeframes other than '1D' / '1W'."""
        with db_engine.begin() as conn:
            ticker_id = _insert_ticker(conn)
        with pytest.raises(IntegrityError):
            with db_engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO signal_backtest_runs
                        (exchange, ticker_id, timeframe,
                         computed_from_date, computed_through_date)
                    VALUES (:ex, :tid, '1h', :from_, :thru)
                """), {
                    "ex": "NSE", "tid": ticker_id,
                    "from_": date(2024, 1, 1),
                    "thru": date(2024, 12, 31),
                })

    def test_ticker_id_must_reference_real_ticker(self, db_engine):
        """The FOREIGN KEY on ticker_id rejects orphan rows. ON DELETE
        CASCADE would handle cleanup if the ticker were later deleted,
        but you can't insert one that doesn't exist."""
        with pytest.raises(IntegrityError):
            with db_engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO signal_backtest_runs
                        (exchange, ticker_id, timeframe,
                         computed_from_date, computed_through_date)
                    VALUES ('NSE', 999999, '1W', :from_, :thru)
                """), {
                    "from_": date(2024, 1, 1),
                    "thru": date(2024, 12, 31),
                })


# ---------- etl_runs.exchange column smoke test ---------- #


class TestEtlRunsExchangeColumn:
    """Phase 15a added the `exchange` column on etl_runs alongside
    the new table. Smoke-test that the column exists and the existing
    EtlStatusRepository.open_run populates it."""

    def test_open_run_populates_exchange_column(self, db_engine):
        from trip_ema.etl.status_repo import EtlStatusRepository
        from trip_ema.etl.types import RunKind

        with db_engine.begin() as conn:
            run_id = EtlStatusRepository(conn).open_run(
                RunKind.SIGNAL_BACKTEST,
                exchange="NSE",
                correlation_id="cid-test",
                notes="test",
            )
        with db_engine.connect() as conn:
            row = conn.execute(text(
                "SELECT kind, exchange, correlation_id "
                "FROM etl_runs WHERE run_id = :rid"
            ), {"rid": run_id}).first()
        assert row.kind == "signal_backtest"
        assert row.exchange == "NSE"
        assert row.correlation_id == "cid-test"

    def test_open_run_without_exchange_leaves_null(self, db_engine):
        """Back-compat: a caller that omits exchange gets a NULL —
        not an error. This is the contract for pre-Phase-15 rows."""
        from trip_ema.etl.status_repo import EtlStatusRepository
        from trip_ema.etl.types import RunKind

        with db_engine.begin() as conn:
            run_id = EtlStatusRepository(conn).open_run(
                RunKind.EOD,
                correlation_id="cid-test",
            )
        with db_engine.connect() as conn:
            row = conn.execute(text(
                "SELECT exchange FROM etl_runs WHERE run_id = :rid"
            ), {"rid": run_id}).first()
        assert row.exchange is None
