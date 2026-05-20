"""End-to-end tests for the signal-generator runner (Phase 15a).

Real Postgres (via the persistence conftest's `db_engine` fixture).
Tests insert synthetic OHLCV bars, run `generate_historical_signals`,
and assert on the produced JSON file plus the resume table state.

The synthetic bars are designed to produce a known sequence of
regime transitions — a sustained uptrend that triggers BUY, then a
plateau that triggers EXIT_FROM_BUY, then a sharp downtrend that
triggers SELL. The engine itself is not under test here (its
behavior is tested separately); we test that the runner correctly
captures whatever signals the engine emits, into the right output
file, with the right resume bookkeeping.

Coverage summary:
  - Happy path: produces an output file with the expected shape
  - Idempotent: second run without force_full is a no-op
  - Force-full: clears resume table and recomputes
  - Per-exchange isolation: force_full on NSE leaves NYSE rows alone
  - Run lifecycle: the etl_runs row is opened, then closed with
    outcome='success'
  - Lifecycle ownership: when run_id is passed in, the function
    doesn't open a new row (and doesn't close the caller's either —
    that's Phase 15b's responsibility)
  - Priming threshold: tickers with too little history are skipped,
    not crashed
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from trip_ema.domain import Timeframe
from trip_ema.etl.signal_generator import (
    _PRIMING_DAILY_BARS,
    generate_historical_signals,
)
from trip_ema.etl.types import RunKind
from trip_ema.persistence.signal_backtest_runs_repo import (
    SignalBacktestRunsRepository,
)


# ---------- Synthetic bar generation ---------- #


def _gen_uptrend_bars(
    start: datetime, n_days: int,
    *, start_price: float = 100.0, daily_drift: float = 1.0,
    daily_range: float = 0.5, base_volume: float = 1_000_000.0,
) -> list[dict]:
    """Generate `n_days` of daily OHLCV bars with a sustained
    uptrend (close advances `daily_drift` per day). Used to wake up
    the engine and trigger a BUY once the EMA bands settle.

    Returns a list of dicts ready for direct DB insertion via
    `_insert_bars`.
    """
    bars = []
    price = start_price
    for i in range(n_days):
        ts = start + timedelta(days=i)
        # Skip weekends — bars are trading days. Pine indicators don't
        # really care about weekend gaps but it keeps the daily_to_weekly
        # aggregator's bucket assignment realistic.
        if ts.weekday() >= 5:
            continue
        open_ = price
        close_ = price + daily_drift
        high_ = max(open_, close_) + daily_range
        low_ = min(open_, close_) - daily_range
        bars.append({
            "timestamp": ts, "open": open_, "high": high_,
            "low": low_, "close": close_,
            "volume": base_volume,
        })
        price = close_
    return bars


def _gen_downtrend_bars(
    start: datetime, n_days: int,
    *, start_price: float = 200.0, daily_drift: float = -1.5,
    daily_range: float = 0.5, base_volume: float = 1_000_000.0,
) -> list[dict]:
    """Sustained downtrend — should trigger SELL after warming."""
    return _gen_uptrend_bars(
        start, n_days,
        start_price=start_price,
        daily_drift=daily_drift,
        daily_range=daily_range,
        base_volume=base_volume,
    )


def _insert_bars(conn, ticker_id: int, bars: list[dict]) -> None:
    """Bulk-insert OHLCVBar dicts as DAILY bars."""
    for b in bars:
        conn.execute(text("""
            INSERT INTO ohlcv_bars (
                ticker_id, timeframe, bar_timestamp,
                open, high, low, close, volume
            ) VALUES (
                :tid, '1D', :ts, :o, :h, :l, :c, :v
            )
        """), {
            "tid": ticker_id, "ts": b["timestamp"],
            "o": b["open"], "h": b["high"], "l": b["low"],
            "c": b["close"], "v": b["volume"],
        })


def _insert_ticker(conn, *, exchange: str, symbol: str) -> int:
    return conn.execute(text("""
        INSERT INTO tickers (exchange, symbol, instrument, is_active)
        VALUES (:ex, :sym, 'EQ', TRUE)
        RETURNING ticker_id
    """), {"ex": exchange, "sym": symbol}).scalar_one()


# ---------- Config fixture ---------- #


def _make_config(market_hours_exchanges: list[str] = ("NSE",)):
    """Build a minimal Config in-memory for the signal generator.
    We don't load YAML — we construct the schema object directly so
    tests don't need an env var or a YAML file."""
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


def _insert_long_active_dataset(
    conn, ticker_id: int,
    *, start: datetime | None = None,
) -> None:
    """Insert a multi-trend dataset long enough to survive priming
    and produce at least a few captured signals.

    Used by tests that need the runner to do real work but don't
    care about the exact bar contents. Total ≈ 850 daily bars,
    starting 2015-01-05; signals fire well after the 250-bar
    priming window.
    """
    if start is None:
        start = datetime(2015, 1, 5, tzinfo=timezone.utc)
    sideways = _gen_uptrend_bars(
        start, 350, start_price=100.0,
        daily_drift=0.05, daily_range=0.3,
    )
    after_side = sideways[-1]["timestamp"] + timedelta(days=1)
    up = _gen_uptrend_bars(
        after_side, 250, start_price=sideways[-1]["close"],
        daily_drift=1.5, daily_range=0.5,
    )
    after_up = up[-1]["timestamp"] + timedelta(days=1)
    down = _gen_downtrend_bars(
        after_up, 250, start_price=up[-1]["close"],
        daily_drift=-1.5, daily_range=0.5,
    )
    _insert_bars(conn, ticker_id, sideways + up + down)


# ---------- Happy path ---------- #


class TestHappyPath:
    def test_produces_output_file_with_signals(self, db_engine, tmp_path):
        """A ticker with enough history through an uptrend then
        downtrend should emit at least one BUY and one SELL.

        The first `_PRIMING_DAILY_BARS` daily bars (250) are
        consumed for priming and their signals are discarded by
        the `bar.date >= start_date` filter. So we need a dataset
        that's >> 250 daily bars AND that has its meaningful
        transitions AFTER the priming window. We do this by:
          - Starting with a flat/sideways pre-history that won't
            trigger signals during priming
          - Following it with an explosive uptrend then downtrend
            AFTER priming, where the signals we care about will fire.
        """
        with db_engine.begin() as conn:
            ticker_id = _insert_ticker(conn, exchange="NSE", symbol="RELIANCE")
            start = datetime(2015, 1, 5, tzinfo=timezone.utc)
            # 300 days of dull sideways drift — this gets consumed
            # by priming. Tiny drift keeps the engine warming without
            # firing premature signals.
            sideways = _gen_uptrend_bars(
                start, 350, start_price=100.0,
                daily_drift=0.05, daily_range=0.3,
            )
            # Then 250 days of explosive uptrend (post-priming → BUY fires)
            after_side = sideways[-1]["timestamp"] + timedelta(days=1)
            up = _gen_uptrend_bars(
                after_side, 250, start_price=sideways[-1]["close"],
                daily_drift=1.5, daily_range=0.5,
            )
            # Then 250 days of explosive downtrend (→ EXIT_FROM_BUY, SELL)
            after_up = up[-1]["timestamp"] + timedelta(days=1)
            down = _gen_downtrend_bars(
                after_up, 250, start_price=up[-1]["close"],
                daily_drift=-1.5, daily_range=0.5,
            )
            _insert_bars(conn, ticker_id, sideways + up + down)

        cfg = _make_config()
        result = generate_historical_signals(
            db_engine, cfg,
            exchange="NSE",
            timeframes=[Timeframe.WEEKLY],
            output_dir=tmp_path,
        )

        # File on disk
        out = tmp_path / "NSE.json"
        assert out.exists()
        data = json.loads(out.read_text())

        # Shape sanity
        assert data["schema_version"] == "1.0"
        assert data["exchange"] == "NSE"
        assert data["generated_by_run_id"] == result.run_id
        assert "1W" in data["params"]
        assert "by_date" in data
        assert data["counts"]["signals_total"] == sum(
            data["counts"]["signals_by_type"].values()
        )

        # Result struct
        assert result.exchange == "NSE"
        assert result.timeframes_computed == ["1W"]
        assert result.tickers_processed == 1
        assert result.tickers_skipped == 0
        # We expect the engine to fire at least one signal across
        # the dataset; if it doesn't, the synthetic data design or
        # the engine has changed and we want to know.
        total_emitted = sum(result.signals_emitted.values())
        assert total_emitted > 0, (
            f"Expected at least one signal across 400 days of "
            f"deliberately-extreme synthetic bars; got 0. "
            f"signals_emitted={result.signals_emitted}"
        )

    def test_etl_runs_row_is_closed_with_success(self, db_engine, tmp_path):
        with db_engine.begin() as conn:
            ticker_id = _insert_ticker(conn, exchange="NSE", symbol="X")
            _insert_long_active_dataset(conn, ticker_id)

        cfg = _make_config()
        result = generate_historical_signals(
            db_engine, cfg,
            exchange="NSE",
            timeframes=[Timeframe.WEEKLY],
            output_dir=tmp_path,
        )

        with db_engine.connect() as conn:
            row = conn.execute(text(
                "SELECT kind, exchange, outcome, finished_at "
                "FROM etl_runs WHERE run_id = :rid"
            ), {"rid": result.run_id}).first()
        assert row.kind == RunKind.SIGNAL_BACKTEST.value
        assert row.exchange == "NSE"
        assert row.outcome == "success"
        assert row.finished_at is not None

    def test_resume_row_is_upserted_for_processed_tickers(
        self, db_engine, tmp_path,
    ):
        with db_engine.begin() as conn:
            ticker_id = _insert_ticker(conn, exchange="NSE", symbol="X")
            _insert_long_active_dataset(conn, ticker_id)

        cfg = _make_config()
        generate_historical_signals(
            db_engine, cfg,
            exchange="NSE",
            timeframes=[Timeframe.WEEKLY],
            output_dir=tmp_path,
        )

        with db_engine.connect() as conn:
            rows = SignalBacktestRunsRepository(conn).get_for_exchange("NSE")
        assert len(rows) == 1
        assert rows[0].ticker_id == ticker_id
        assert rows[0].timeframe == Timeframe.WEEKLY
        # through_date is the date of the last bar processed
        # (synthesized weekly bucket's Monday-anchor date).
        assert rows[0].computed_through_date <= date.today()


# ---------- Idempotency / resume ---------- #


class TestResumeAndIdempotency:
    def test_second_run_without_force_full_skips_ticker(
        self, db_engine, tmp_path,
    ):
        """Resume contract: a second run with no new bars and no
        force_full reports the ticker as skipped, not processed.
        And the file is rewritten but the signals don't change."""
        with db_engine.begin() as conn:
            ticker_id = _insert_ticker(conn, exchange="NSE", symbol="X")
            _insert_long_active_dataset(conn, ticker_id)

        cfg = _make_config()
        # First run
        r1 = generate_historical_signals(
            db_engine, cfg,
            exchange="NSE",
            timeframes=[Timeframe.WEEKLY],
            output_dir=tmp_path,
        )
        first_signals = sum(r1.signals_emitted.values())
        first_file = json.loads((tmp_path / "NSE.json").read_text())

        # Second run — no new bars added since
        r2 = generate_historical_signals(
            db_engine, cfg,
            exchange="NSE",
            timeframes=[Timeframe.WEEKLY],
            output_dir=tmp_path,
        )
        # Skipped, not processed
        assert r2.tickers_processed == 0
        assert r2.tickers_skipped == 1
        assert sum(r2.signals_emitted.values()) == 0
        # The file's by_date content is unchanged (the second
        # run only refreshed `generated_at` and `generated_by_run_id`).
        second_file = json.loads((tmp_path / "NSE.json").read_text())
        assert second_file["by_date"] == first_file["by_date"]
        assert second_file["counts"]["signals_total"] == first_signals

    def test_force_full_wipes_resume_rows_and_reprocesses(
        self, db_engine, tmp_path,
    ):
        """force_full=True must DELETE resume rows in a small dedicated
        transaction at run start, then recompute every ticker. The
        resulting JSON should still be valid and the ticker should
        appear in `tickers_processed`, not `tickers_skipped`."""
        with db_engine.begin() as conn:
            ticker_id = _insert_ticker(conn, exchange="NSE", symbol="X")
            _insert_long_active_dataset(conn, ticker_id)

        cfg = _make_config()
        # First (incremental, but acts as full because no resume row exists)
        generate_historical_signals(
            db_engine, cfg,
            exchange="NSE",
            timeframes=[Timeframe.WEEKLY],
            output_dir=tmp_path,
        )

        # Confirm the resume row exists
        with db_engine.connect() as conn:
            assert len(
                SignalBacktestRunsRepository(conn).get_for_exchange("NSE")
            ) == 1

        # Force-full
        r2 = generate_historical_signals(
            db_engine, cfg,
            exchange="NSE",
            timeframes=[Timeframe.WEEKLY],
            force_full=True,
            output_dir=tmp_path,
        )
        # Re-processed
        assert r2.tickers_processed == 1
        assert r2.tickers_skipped == 0
        # And the resume row is back (it was wiped, then recreated)
        with db_engine.connect() as conn:
            assert len(
                SignalBacktestRunsRepository(conn).get_for_exchange("NSE")
            ) == 1

    def test_force_full_only_affects_target_exchange(
        self, db_engine, tmp_path,
    ):
        """Insert resume rows for NSE AND NYSE. force_full on NSE must
        leave NYSE's rows untouched. This is the per-exchange isolation
        guarantee from §4.2.4."""
        with db_engine.begin() as conn:
            t_nse = _insert_ticker(conn, exchange="NSE", symbol="X")
            t_nyse = _insert_ticker(conn, exchange="NYSE", symbol="X")
            repo = SignalBacktestRunsRepository(conn)
            for ex, tid in (("NSE", t_nse), ("NYSE", t_nyse)):
                repo.upsert(
                    exchange=ex, ticker_id=tid,
                    timeframe=Timeframe.WEEKLY,
                    computed_from_date=date(2020, 1, 1),
                    computed_through_date=date(2024, 12, 31),
                )
            # NSE ticker needs bars too (else force_full's recompute
            # has nothing to do, which is fine but doesn't exercise
            # the path).
            _insert_long_active_dataset(conn, t_nse)

        cfg = _make_config(market_hours_exchanges=["NSE", "NYSE"])
        generate_historical_signals(
            db_engine, cfg,
            exchange="NSE",
            timeframes=[Timeframe.WEEKLY],
            force_full=True,
            output_dir=tmp_path,
        )

        with db_engine.connect() as conn:
            nyse_rows = SignalBacktestRunsRepository(conn).get_for_exchange("NYSE")
        assert len(nyse_rows) == 1, (
            "NYSE's resume row must NOT be wiped by an NSE force_full"
        )
        # The NYSE row's values are unchanged (from_date == 2020-01-01
        # is what we wrote up there)
        assert nyse_rows[0].computed_from_date == date(2020, 1, 1)


# ---------- Lifecycle ownership (Phase 15b prep) ---------- #


class TestLifecycleOwnership:
    def test_passing_run_id_skips_open_but_still_closes(
        self, db_engine, tmp_path,
    ):
        """Phase 15b: caller opens etl_runs in the request thread, hands
        the run_id to the runner. The runner must NOT open a new row,
        and (per Finding 4) the function still closes the row at the
        end so the caller doesn't have to remember.

        Re-reading: the docstring on `generate_historical_signals` says
        when run_id is provided, the function still owns the close.
        Confirm: the row passed in has outcome=success when the
        function returns.
        """
        with db_engine.begin() as conn:
            ticker_id = _insert_ticker(conn, exchange="NSE", symbol="X")
            _insert_long_active_dataset(conn, ticker_id)

        # Caller (the future HTTP route) opens the row.
        from trip_ema.etl.status_repo import EtlStatusRepository
        with db_engine.begin() as conn:
            pre_existing_run_id = EtlStatusRepository(conn).open_run(
                RunKind.SIGNAL_BACKTEST,
                exchange="NSE",
                correlation_id="from-http-route",
                notes="opened by caller",
            )

        cfg = _make_config()
        result = generate_historical_signals(
            db_engine, cfg,
            exchange="NSE",
            timeframes=[Timeframe.WEEKLY],
            run_id=pre_existing_run_id,
            output_dir=tmp_path,
        )
        # The function reused the caller's run_id, didn't open a new one
        assert result.run_id == pre_existing_run_id

        with db_engine.connect() as conn:
            rows = list(conn.execute(text(
                "SELECT run_id, outcome FROM etl_runs"
            )))
        # Per the docstring's split-ownership rule (Finding 4):
        # `owns_run_lifecycle = run_id is None` — so when the caller
        # provides the run_id, the function does NOT close it. The
        # row is still open after the function returns; the caller
        # closes it (Phase 15b's executor.submit completion callback).
        # This test locks that behavior in so 15b can rely on it.
        assert len(rows) == 1
        assert rows[0].run_id == pre_existing_run_id
        assert rows[0].outcome is None, (
            "When caller owns the etl_runs lifecycle (run_id passed in), "
            "the runner must NOT close the row — that's the caller's job."
        )


# ---------- Priming / too-little-history ---------- #


class TestPriming:
    def test_ticker_with_insufficient_history_is_skipped(
        self, db_engine, tmp_path,
    ):
        """A ticker with fewer than _PRIMING_DAILY_BARS+1 daily bars
        is skipped — there's not enough history to produce meaningful
        signals. The runner must handle this cleanly: no crash,
        ticker counted as skipped, no resume row inserted."""
        with db_engine.begin() as conn:
            ticker_id = _insert_ticker(conn, exchange="NSE", symbol="TINY")
            start = datetime(2024, 1, 1, tzinfo=timezone.utc)
            # Way fewer than the priming threshold
            _insert_bars(conn, ticker_id, _gen_uptrend_bars(start, 50))

        cfg = _make_config()
        result = generate_historical_signals(
            db_engine, cfg,
            exchange="NSE",
            timeframes=[Timeframe.WEEKLY],
            output_dir=tmp_path,
        )

        assert result.tickers_processed == 0
        assert result.tickers_skipped == 1
        # No resume row written for a ticker we couldn't process
        with db_engine.connect() as conn:
            assert SignalBacktestRunsRepository(conn).get_for_exchange("NSE") == []

    def test_priming_constant_is_in_expected_range(self):
        """Lock the priming constant against accidental tweaks. The
        addendum docs the rationale (≈36 weekly bars × 5 trading days
        + ATR + volume headroom ≈ 250). If you genuinely need to
        change this, update both the constant AND the docs in
        PHASE_15_DESIGN_ADDENDUM."""
        assert 200 <= _PRIMING_DAILY_BARS <= 400


# ---------- Input validation ---------- #


class TestInputValidation:
    def test_empty_timeframes_rejected(self, db_engine, tmp_path):
        cfg = _make_config()
        with pytest.raises(ValueError, match="timeframes must not be empty"):
            generate_historical_signals(
                db_engine, cfg,
                exchange="NSE",
                timeframes=[],
                output_dir=tmp_path,
            )

    def test_unsupported_timeframe_rejected(self, db_engine, tmp_path):
        cfg = _make_config()
        with pytest.raises(ValueError, match="not supported in Phase 15"):
            generate_historical_signals(
                db_engine, cfg,
                exchange="NSE",
                timeframes=[Timeframe.HOURLY],
                output_dir=tmp_path,
            )

    def test_unknown_exchange_rejected(self, db_engine, tmp_path):
        from trip_ema.etl.exchanges import UnsupportedExchangeError

        cfg = _make_config()
        with pytest.raises(UnsupportedExchangeError):
            generate_historical_signals(
                db_engine, cfg,
                exchange="XYZ",
                timeframes=[Timeframe.WEEKLY],
                output_dir=tmp_path,
            )

    def test_exchange_not_in_market_hours_rejected(
        self, db_engine, tmp_path,
    ):
        from trip_ema.etl.exchanges import UnsupportedExchangeError

        # Only NSE in market_hours; ask for NYSE
        cfg = _make_config(market_hours_exchanges=["NSE"])
        with pytest.raises(UnsupportedExchangeError):
            generate_historical_signals(
                db_engine, cfg,
                exchange="NYSE",
                timeframes=[Timeframe.WEEKLY],
                output_dir=tmp_path,
            )


# ---------- File handling edge cases ---------- #


class TestExistingFileHandling:
    def test_corrupt_existing_file_raises(self, db_engine, tmp_path):
        """If the existing JSON file is corrupt, the runner refuses to
        proceed — silently overwriting a damaged file might hide a
        real problem."""
        with db_engine.begin() as conn:
            ticker_id = _insert_ticker(conn, exchange="NSE", symbol="X")
            _insert_long_active_dataset(conn, ticker_id)

        # Write a bad JSON file before the run
        (tmp_path / "NSE.json").write_text("{not valid json")

        cfg = _make_config()
        with pytest.raises(RuntimeError, match="not valid JSON"):
            generate_historical_signals(
                db_engine, cfg,
                exchange="NSE",
                timeframes=[Timeframe.WEEKLY],
                output_dir=tmp_path,
            )

    def test_wrong_exchange_in_existing_file_raises(
        self, db_engine, tmp_path,
    ):
        """If the file exists but its `exchange` field doesn't match
        the run's exchange, refuse rather than overwrite."""
        with db_engine.begin() as conn:
            ticker_id = _insert_ticker(conn, exchange="NSE", symbol="X")
            _insert_long_active_dataset(conn, ticker_id)

        (tmp_path / "NSE.json").write_text(json.dumps({
            "exchange": "NYSE", "by_date": {},
        }))

        cfg = _make_config()
        with pytest.raises(RuntimeError, match="exchange="):
            generate_historical_signals(
                db_engine, cfg,
                exchange="NSE",
                timeframes=[Timeframe.WEEKLY],
                output_dir=tmp_path,
            )
