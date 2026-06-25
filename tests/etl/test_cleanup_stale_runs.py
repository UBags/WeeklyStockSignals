"""Tests for EtlStatusRepository.cleanup_stale_runs.

Real Postgres (via the persistence conftest's `db_engine` fixture).
Tests insert etl_runs rows with controlled `started_at` offsets, run
the cleanup, and assert on what got closed vs left alone.

What we're nailing down:

  1. **Threshold honored.** Rows started inside `stale_after_hours`
     are never touched, even if open. Rows started outside it are
     closed if (and only if) they're open.
  2. **Outcome filter honored.** A row that's already closed
     (outcome IS NOT NULL) is left alone regardless of age — we
     only target rows that look like orphans.
  3. **Exchange scoping.** Cleaning NSE never touches BSE's rows
     and vice versa. This is what lets us call cleanup at the start
     of every fetch pass without worrying about cross-exchange
     interference.
  4. **Threshold is parametric.** Callers can override the 6-hour
     default for situations where a known-long-running backfill is
     active.
  5. **Idempotency.** A second call right after the first finds
     nothing to do.
  6. **Notes preserved + appended.** Existing notes survive; the
     cleanup marker is appended on a new line so operators can
     reconstruct what happened.

We deliberately do NOT test the runner-side integration (the call
from `_run_fetch_pass`) here — that's an integration concern, and
the contract that cleanup runs at the start of every fetch pass is
better verified by the runner's own tests as those expand.
"""

from __future__ import annotations

from typing import Optional

import pytest
from sqlalchemy import text

from trip_ema.etl.status_repo import EtlStatusRepository
from trip_ema.etl.types import RunKind


# ---------- Helpers ---------- #


def _insert_open_run(
    conn,
    *,
    kind: RunKind,
    exchange: str | None,
    started_at_offset_hours: float,
    notes: str | None = None,
) -> int:
    """Insert an open (outcome=NULL, finished_at=NULL) etl_runs row
    with `started_at` placed at NOW() - offset_hours. Returns
    run_id.

    Float-valued offset lets us pin tests near the threshold
    (e.g. 5.9h vs 6.1h) without timing flakiness — we use a 0.5h
    buffer in both directions in the tests below.
    """
    return conn.execute(
        text("""
            INSERT INTO etl_runs (kind, exchange, started_at, notes)
            VALUES (
                :kind, :ex,
                NOW() - make_interval(secs => :offset_secs),
                :notes
            )
            RETURNING run_id
        """),
        {
            "kind": kind.value,
            "ex": exchange,
            "offset_secs": started_at_offset_hours * 3600,
            "notes": notes,
        },
    ).scalar_one()


def _insert_closed_run(
    conn,
    *,
    kind: RunKind,
    exchange: str | None,
    started_at_offset_hours: float,
    outcome: str,
) -> int:
    """Insert a closed (outcome and finished_at both populated)
    etl_runs row. Used to verify cleanup ignores already-closed
    rows."""
    return conn.execute(
        text("""
            INSERT INTO etl_runs (
                kind, exchange, started_at, finished_at, outcome
            )
            VALUES (
                :kind, :ex,
                NOW() - make_interval(secs => :offset_secs),
                NOW() - make_interval(secs => :offset_secs)
                      + make_interval(mins => 10),
                :outcome
            )
            RETURNING run_id
        """),
        {
            "kind": kind.value,
            "ex": exchange,
            "offset_secs": started_at_offset_hours * 3600,
            "outcome": outcome,
        },
    ).scalar_one()


def _row_state(conn, run_id: int) -> dict:
    """Fetch the columns we care about for assertions."""
    row = conn.execute(
        text("""
            SELECT outcome, finished_at, notes
            FROM etl_runs
            WHERE run_id = :rid
        """),
        {"rid": run_id},
    ).first()
    assert row is not None, f"row {run_id} missing"
    return {
        "outcome": row.outcome,
        "finished_at": row.finished_at,
        "notes": row.notes,
    }


# ---------- Threshold honored ---------- #


class TestThreshold:
    def test_does_not_touch_fresh_open_run(self, db_engine):
        """An open run started 1h ago must not be touched by the
        default 6h threshold — that's a likely-still-running ETL."""
        with db_engine.begin() as conn:
            run_id = _insert_open_run(
                conn,
                kind=RunKind.EOD,
                exchange="NSE",
                started_at_offset_hours=1.0,
            )

        with db_engine.begin() as conn:
            n = EtlStatusRepository(conn).cleanup_stale_runs(exchange="NSE")

        assert n == 0
        with db_engine.connect() as conn:
            state = _row_state(conn, run_id)
        assert state["outcome"] is None
        assert state["finished_at"] is None

    def test_closes_stale_open_run(self, db_engine):
        """An open run started 7h ago is past the 6h threshold and
        must be closed."""
        with db_engine.begin() as conn:
            run_id = _insert_open_run(
                conn,
                kind=RunKind.EOD,
                exchange="NSE",
                started_at_offset_hours=7.0,
            )

        with db_engine.begin() as conn:
            n = EtlStatusRepository(conn).cleanup_stale_runs(exchange="NSE")

        assert n == 1
        with db_engine.connect() as conn:
            state = _row_state(conn, run_id)
        assert state["outcome"] == "failed"
        assert state["finished_at"] is not None
        assert "cleanup: orphaned by mid-run process death" in state["notes"]
        assert "auto-closed after 6h" in state["notes"]

    def test_threshold_at_default_is_six_hours(self, db_engine):
        """Pin the documented default. If someone changes the default
        in the method signature, this catches it — the public
        contract calls for 6 hours."""
        # 5.5h: just inside; 6.5h: just outside
        with db_engine.begin() as conn:
            fresh = _insert_open_run(
                conn, kind=RunKind.EOD, exchange="NSE",
                started_at_offset_hours=5.5,
            )
            stale = _insert_open_run(
                conn, kind=RunKind.EOD, exchange="NSE",
                started_at_offset_hours=6.5,
            )

        with db_engine.begin() as conn:
            n = EtlStatusRepository(conn).cleanup_stale_runs(exchange="NSE")

        assert n == 1
        with db_engine.connect() as conn:
            assert _row_state(conn, fresh)["outcome"] is None
            assert _row_state(conn, stale)["outcome"] == "failed"


# ---------- Outcome filter honored ---------- #


class TestOutcomeFilter:
    def test_does_not_touch_already_closed_run(self, db_engine):
        """A closed run (outcome='success', finished_at populated)
        must never be touched, even at extreme age. Closed rows are
        history; rewriting them would lose the original outcome."""
        with db_engine.begin() as conn:
            run_id = _insert_closed_run(
                conn,
                kind=RunKind.EOD,
                exchange="NSE",
                started_at_offset_hours=24 * 30,   # 30 days ago
                outcome="success",
            )

        with db_engine.begin() as conn:
            n = EtlStatusRepository(conn).cleanup_stale_runs(exchange="NSE")

        assert n == 0
        with db_engine.connect() as conn:
            state = _row_state(conn, run_id)
        assert state["outcome"] == "success"   # unchanged
        assert state["finished_at"] is not None

    def test_does_not_touch_partial_outcome_run(self, db_engine):
        """A row with outcome='partial' is still 'closed' from the
        cleanup's perspective — we only touch outcome IS NULL."""
        with db_engine.begin() as conn:
            run_id = _insert_closed_run(
                conn,
                kind=RunKind.EOD,
                exchange="NSE",
                started_at_offset_hours=24,
                outcome="partial",
            )

        with db_engine.begin() as conn:
            n = EtlStatusRepository(conn).cleanup_stale_runs(exchange="NSE")

        assert n == 0
        with db_engine.connect() as conn:
            assert _row_state(conn, run_id)["outcome"] == "partial"


# ---------- Exchange scoping ---------- #


class TestExchangeScoping:
    def test_scopes_by_exchange(self, db_engine):
        """Stale orphans for NSE and BSE both exist. cleanup(NSE) must
        close only the NSE one, not the BSE one. This is what lets us
        call cleanup at the start of every fetch pass — NSE's EoD
        won't disturb a BSE backfill running in parallel."""
        with db_engine.begin() as conn:
            nse_run = _insert_open_run(
                conn, kind=RunKind.EOD, exchange="NSE",
                started_at_offset_hours=8.0,
            )
            bse_run = _insert_open_run(
                conn, kind=RunKind.EOD, exchange="BSE",
                started_at_offset_hours=8.0,
            )

        with db_engine.begin() as conn:
            n = EtlStatusRepository(conn).cleanup_stale_runs(exchange="NSE")

        assert n == 1
        with db_engine.connect() as conn:
            assert _row_state(conn, nse_run)["outcome"] == "failed"
            assert _row_state(conn, bse_run)["outcome"] is None
            assert _row_state(conn, bse_run)["finished_at"] is None

    def test_does_not_touch_null_exchange_rows(self, db_engine):
        """Pre-Phase-15a etl_runs rows have exchange IS NULL. The
        cleanup's WHERE clause uses `exchange = :ex` which doesn't
        match NULL, so legacy rows are deliberately left alone.
        Documented in cleanup_stale_runs' docstring as intentional."""
        with db_engine.begin() as conn:
            legacy_run = _insert_open_run(
                conn, kind=RunKind.EOD, exchange=None,
                started_at_offset_hours=24 * 60,   # 60 days ago
            )

        with db_engine.begin() as conn:
            n = EtlStatusRepository(conn).cleanup_stale_runs(exchange="NSE")

        assert n == 0
        with db_engine.connect() as conn:
            state = _row_state(conn, legacy_run)
        assert state["outcome"] is None         # unchanged
        assert state["finished_at"] is None     # unchanged


# ---------- Parametric threshold ---------- #


class TestParametricThreshold:
    def test_longer_threshold_keeps_more_open(self, db_engine):
        """Operator running a long backfill can raise the threshold
        to protect their in-flight job. A 12h threshold leaves a
        10h-old open run alone; the default 6h would have closed it."""
        with db_engine.begin() as conn:
            run_id = _insert_open_run(
                conn, kind=RunKind.BACKFILL, exchange="NSE",
                started_at_offset_hours=10.0,
            )

        with db_engine.begin() as conn:
            n = EtlStatusRepository(conn).cleanup_stale_runs(
                exchange="NSE",
                stale_after_hours=12,
            )

        assert n == 0
        with db_engine.connect() as conn:
            assert _row_state(conn, run_id)["outcome"] is None

    def test_shorter_threshold_cleans_more_aggressively(self, db_engine):
        """The same row that's protected by the 6h default gets closed
        with a 2h threshold. Symmetric to the longer-threshold test."""
        with db_engine.begin() as conn:
            run_id = _insert_open_run(
                conn, kind=RunKind.EOD, exchange="NSE",
                started_at_offset_hours=3.0,
            )

        with db_engine.begin() as conn:
            n = EtlStatusRepository(conn).cleanup_stale_runs(
                exchange="NSE",
                stale_after_hours=2,
            )

        assert n == 1
        with db_engine.connect() as conn:
            state = _row_state(conn, run_id)
        assert state["outcome"] == "failed"
        assert "auto-closed after 2h" in state["notes"]


# ---------- Idempotency ---------- #


class TestIdempotency:
    def test_second_call_is_a_no_op(self, db_engine):
        """First call closes the orphan; second call has nothing to
        do because the row is no longer open. Important: the runner
        calls cleanup at the start of every fetch pass — back-to-back
        EoD + intraday must not double-touch the same row."""
        with db_engine.begin() as conn:
            run_id = _insert_open_run(
                conn, kind=RunKind.EOD, exchange="NSE",
                started_at_offset_hours=7.0,
            )

        with db_engine.begin() as conn:
            first = EtlStatusRepository(conn).cleanup_stale_runs(exchange="NSE")
        with db_engine.begin() as conn:
            second = EtlStatusRepository(conn).cleanup_stale_runs(exchange="NSE")

        assert first == 1
        assert second == 0
        # The row's finished_at was set by the first call; the second
        # call must NOT have updated it again (would be a write
        # amplification + audit confusion).
        with db_engine.connect() as conn:
            state_after_first_logically = _row_state(conn, run_id)
        # The notes should mention cleanup exactly once — a second
        # write would have appended it again.
        assert state_after_first_logically["notes"].count(
            "cleanup: orphaned by mid-run process death"
        ) == 1


# ---------- Notes preserved + appended ---------- #


class TestNotesHandling:
    def test_existing_notes_preserved_and_marker_appended(self, db_engine):
        """If the orphan row had notes (e.g. correlation_id or
        operator annotation) before it was abandoned, those notes
        must survive cleanup. The marker is appended on a new line."""
        with db_engine.begin() as conn:
            run_id = _insert_open_run(
                conn, kind=RunKind.EOD, exchange="NSE",
                started_at_offset_hours=8.0,
                notes="started by operator alice; cron-skipped today",
            )

        with db_engine.begin() as conn:
            EtlStatusRepository(conn).cleanup_stale_runs(exchange="NSE")

        with db_engine.connect() as conn:
            notes = _row_state(conn, run_id)["notes"]
        # Original content survives
        assert "started by operator alice" in notes
        assert "cron-skipped today" in notes
        # Cleanup marker appended
        assert "cleanup: orphaned by mid-run process death" in notes
        # On a new line — the original `notes` field doesn't end with
        # a newline, so cleanup adds one before its marker
        assert "\ncleanup:" in notes

    def test_marker_appended_to_null_notes(self, db_engine):
        """If notes was NULL, the marker becomes the entire notes
        content (no leading newline)."""
        with db_engine.begin() as conn:
            run_id = _insert_open_run(
                conn, kind=RunKind.EOD, exchange="NSE",
                started_at_offset_hours=8.0,
                notes=None,
            )

        with db_engine.begin() as conn:
            EtlStatusRepository(conn).cleanup_stale_runs(exchange="NSE")

        with db_engine.connect() as conn:
            notes = _row_state(conn, run_id)["notes"]
        assert notes is not None
        assert notes.startswith("cleanup: orphaned by mid-run process death")


# ---------- Runner integration (belt-and-braces) ---------- #


def _make_runner_config(market_hours_exchanges=("NSE",)):
    """Minimal Config sufficient for `run_eod` to reach the
    no-tickers branch. We bypass the YFinanceSource construction
    (which would consume `config.etl.*`) by passing an explicit
    `source=` to run_eod, so EtlConfig is not required here.

    EMA + ATR are populated because the schema demands them, even
    though the engine never runs in the empty-tickers path.
    """
    from trip_ema.config.schema import (
        ATRConfig, Config, DatabaseConfig, EMAConfig, EMAStageConfig,
        MarketHoursConfig,
    )

    return Config(
        database=DatabaseConfig(
            host="localhost", database="x", user="x",
            password_env="TRIP_EMA_DB_PASSWORD",
        ),
        ema=EMAConfig(
            med=EMAStageConfig(base=8, s1=4, s2=4, final=None),
            long=EMAStageConfig(base=15, s1=4, s2=4, final=3),
            longest=EMAStageConfig(base=28, s1=7, s2=5, final=3),
        ),
        atr=ATRConfig(),
        market_hours=[
            MarketHoursConfig(
                exchange=ex, open="09:15", close="15:30",
                tz="Asia/Kolkata", weekdays=[1, 2, 3, 4, 5],
            )
            for ex in market_hours_exchanges
        ],
    )


class TestRunnerIntegration:
    """Belt-and-braces: the unit tests above verify that
    cleanup_stale_runs does the right UPDATE when called. These
    tests verify that the runner ACTUALLY CALLS it at the start of
    every fetch pass. The two layers protect different failure
    modes:

      - Someone refactors the SQL inside cleanup_stale_runs and
        breaks it → the unit tests catch it.
      - Someone removes the call from _run_fetch_pass (or moves it
        somewhere it doesn't run) → the unit tests still pass, but
        these integration tests catch it.

    We test against `run_eod` because all three bar-fetch entry
    points (run_eod / run_intraday / run_backfill) share the same
    `_run_fetch_pass` spine. If cleanup runs for one of them, it
    runs for all three.

    To keep the test fast and offline, we exercise the no-tickers
    branch — the runner does cleanup → open new run → select
    tickers → empty → close new run → return. Yfinance is never
    touched because there are no tickers to fetch.
    """

    def test_run_eod_auto_closes_orphan_before_opening_new_run(
        self, db_engine,
    ):
        """The contract: an orphaned NSE EoD row from days ago must
        be auto-closed by today's NSE EoD. Pins the cleanup-at-step-0
        invariant inside _run_fetch_pass."""
        from trip_ema.etl.runner import run_eod
        from trip_ema.etl.yfinance_source import YFinanceSource

        # Arrange: an orphaned NSE EoD row (mimics your June-1
        # run_id=239 situation — process died mid-run, row left
        # dangling with outcome=NULL).
        with db_engine.begin() as conn:
            orphan_id = _insert_open_run(
                conn,
                kind=RunKind.EOD,
                exchange="NSE",
                started_at_offset_hours=8.0,
                notes="started=2026-06-01 15:15 IST",
            )
            # Ensure there are NO active NSE tickers so the runner
            # takes the no-tickers branch instead of trying to fetch.
            conn.execute(text(
                "DELETE FROM ohlcv_bars WHERE ticker_id IN "
                "(SELECT ticker_id FROM tickers WHERE exchange = 'NSE')"
            ))
            conn.execute(text(
                "DELETE FROM tickers WHERE exchange = 'NSE'"
            ))

        cfg = _make_runner_config(market_hours_exchanges=["NSE"])
        # Passing source= bypasses YFinanceSource(...) construction
        # which would consume config.etl.* fields we deliberately
        # don't populate. The source is never actually called
        # because the tickers list is empty.
        stub_source = YFinanceSource(
            exchange="NSE", batch_size=50, request_delay_seconds=0.0,
        )

        # Act
        new_run_id = run_eod(
            db_engine, cfg,
            exchange="NSE",
            source=stub_source,
        )

        # Assert (a): the orphan was auto-closed by step-0 cleanup.
        with db_engine.connect() as conn:
            orphan_state = _row_state(conn, orphan_id)
        assert orphan_state["outcome"] == "failed", (
            "run_eod must call cleanup_stale_runs at step 0; "
            "the 8h-old orphan should have been auto-closed before "
            "the new run was opened."
        )
        assert orphan_state["finished_at"] is not None
        assert "cleanup: orphaned by mid-run process death" in (
            orphan_state["notes"]
        )
        # The original notes survived (notes-preservation contract)
        assert "started=2026-06-01 15:15 IST" in orphan_state["notes"]

        # Assert (b): a NEW run row was opened by run_eod, distinct
        # from the orphan, and closed cleanly (no-tickers branch).
        assert new_run_id != orphan_id
        with db_engine.connect() as conn:
            new_state = _row_state(conn, new_run_id)
        assert new_state["outcome"] == "success"
        assert new_state["finished_at"] is not None
        assert "no tickers" in (new_state["notes"] or "")

    def test_run_eod_for_one_exchange_does_not_touch_other_exchange_orphans(
        self, db_engine,
    ):
        """Per-exchange isolation, tested end-to-end through the
        runner. An NSE EoD run must not auto-close BSE orphans.
        This belt-and-braces the unit-level scoping test
        (TestExchangeScoping.test_scopes_by_exchange) against the
        possibility that someone changes how the runner passes
        exchange to cleanup_stale_runs."""
        from trip_ema.etl.runner import run_eod
        from trip_ema.etl.yfinance_source import YFinanceSource

        with db_engine.begin() as conn:
            bse_orphan_id = _insert_open_run(
                conn,
                kind=RunKind.EOD,
                exchange="BSE",
                started_at_offset_hours=8.0,
            )
            # Clear NSE tickers so run_eod takes the no-tickers branch
            conn.execute(text(
                "DELETE FROM ohlcv_bars WHERE ticker_id IN "
                "(SELECT ticker_id FROM tickers WHERE exchange = 'NSE')"
            ))
            conn.execute(text(
                "DELETE FROM tickers WHERE exchange = 'NSE'"
            ))

        cfg = _make_runner_config(market_hours_exchanges=["NSE", "BSE"])
        stub_source = YFinanceSource(
            exchange="NSE", batch_size=50, request_delay_seconds=0.0,
        )

        # Run NSE EoD — should NOT touch the BSE orphan
        run_eod(db_engine, cfg, exchange="NSE", source=stub_source)

        with db_engine.connect() as conn:
            bse_state = _row_state(conn, bse_orphan_id)
        assert bse_state["outcome"] is None, (
            "NSE EoD must not auto-close BSE orphans; cleanup_stale_runs "
            "is scoped by exchange and the runner must pass its own "
            "exchange, not a wildcard."
        )
        assert bse_state["finished_at"] is None
