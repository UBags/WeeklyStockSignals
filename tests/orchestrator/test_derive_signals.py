"""Integration tests for trip_ema.orchestrator.derive_signals.

Real Postgres via the db_engine fixture; the tests seed
stock_indicator_state and ohlcv_bars directly so they exercise the
exact read paths the production cycle uses.

Pure-unit tests (no DB) for _classify_regime_change live in the
TestClassifyRegimeChange class — no fixture needed.
"""
from __future__ import annotations

from datetime import datetime, UTC
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")

import pytest

from trip_ema.domain import OHLCVBar, TickerRef, Timeframe
from trip_ema.domain.stock_indicator_state import StockIndicatorState
from trip_ema.orchestrator.derive_signals import (
    _classify_regime_change,
    derive_signal_for_ticker_this_cycle,
    derive_signals_for_all_active_stocks,
)
from trip_ema.persistence import transaction
from trip_ema.persistence.bars_repo import BarsRepository
from trip_ema.persistence.stock_indicator_state_repo import (
    StockIndicatorStateRepository,
)
from trip_ema.persistence.tickers_repo import TickersRepository


# ============================================================
# Helpers
# ============================================================

def _seed_ticker(
    conn, *, symbol: str = "RELIANCE", is_active: bool = True,
) -> int:
    ref = TickerRef("NSE", symbol, "EQ")
    return TickersRepository(conn).insert(
        ref, created_at=datetime.now(UTC), is_active=is_active,
    )


def _state(
    ticker_id: int,
    *,
    bar_timestamp: datetime,
    display_regime: int,
    timeframe: str = Timeframe.WEEKLY,
) -> StockIndicatorState:
    """Build a minimal StockIndicatorState with placeholder indicator
    values. Only `display_regime` and the keys matter for derive
    tests; EMAs/ATR/bands carry plausible numbers so app-level
    validation passes."""
    return StockIndicatorState(
        ticker_id=ticker_id,
        timeframe=timeframe,
        date=bar_timestamp.astimezone(_IST).date(),
        bar_timestamp=bar_timestamp,
        med_ema=100.0, long_ema=99.0, longest_ema=98.0,
        atr=2.0,
        upper_inner_band=104.0, lower_inner_band=96.0,
        upper_mid_band=106.0, lower_mid_band=94.0,
        upper_outer_band=108.0, lower_outer_band=92.0,
        display_regime=display_regime,
        computed_at=datetime.now(UTC),
    )


def _seed_daily_bar(
    conn, ticker_id: int, *, day: datetime, close: float = 150.0,
) -> None:
    """Seed one daily OHLCV bar via BarsRepository.upsert_bar."""
    bar = OHLCVBar(
        timestamp=day,
        open=close - 1, high=close + 1, low=close - 2,
        close=close, volume=1_000_000,
    )
    BarsRepository(conn).upsert_bar(
        ticker_id, Timeframe.DAILY, bar,
    )


# ============================================================
# Pure unit tests — no DB
# ============================================================

class TestClassifyRegimeChange:
    """The pure regime → signal_type mapping. Same precedence as
    the legacy walker."""

    @pytest.mark.parametrize("before,after,expected", [
        # entries into +1 → BUY (regardless of where from)
        (0, 1, "BUY"),
        (-1, 1, "BUY"),       # precedence over EXIT_FROM_SELL
        # entries into -1 → SELL (regardless of where from)
        (0, -1, "SELL"),
        (1, -1, "SELL"),      # precedence over EXIT_FROM_BUY
        # exits to 0 → EXIT_FROM_{BUY,SELL}
        (1, 0, "EXIT_FROM_BUY"),
        (-1, 0, "EXIT_FROM_SELL"),
    ])
    def test_classify(self, before, after, expected):
        assert _classify_regime_change(before, after) == expected

    def test_classify_equal_regimes_returns_none(self):
        """Equal regimes are not a signal. The rule is total and never
        raises (NO_CHANGE has no DB signal_type → None)."""
        assert _classify_regime_change(1, 1) is None
        assert _classify_regime_change(0, 0) is None
        assert _classify_regime_change(-1, -1) is None


# ============================================================
# Single-ticker derivation
# ============================================================

class TestSingleTicker:

    def _setup_two_bars(
        self, db_engine, *,
        prev_regime: int, curr_regime: int,
        close: float = 150.0,
    ) -> tuple[int, datetime]:
        """Seed: ticker + 2 state rows (one week apart) + daily bar.
        Returns (ticker_id, latest_bar_timestamp)."""
        prev_week = datetime(2025, 9, 29, tzinfo=UTC)
        curr_week = datetime(2025, 10, 6, tzinfo=UTC)
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            sr = StockIndicatorStateRepository(conn)
            sr.upsert_one(_state(
                tid, bar_timestamp=prev_week,
                display_regime=prev_regime,
            ))
            sr.upsert_one(_state(
                tid, bar_timestamp=curr_week,
                display_regime=curr_regime,
            ))
            _seed_daily_bar(
                conn, tid,
                day=datetime(2025, 10, 9, 9, 15, tzinfo=UTC),
                close=close,
            )
        return tid, curr_week

    def test_no_change_returns_none(self, db_engine):
        """Same regime in latest two bars → no signal."""
        tid, _ = self._setup_two_bars(
            db_engine, prev_regime=1, curr_regime=1,
        )
        with db_engine.connect() as conn:
            sig = derive_signal_for_ticker_this_cycle(
                conn, tid, now=datetime.now(UTC),
            )
        assert sig is None

    @pytest.mark.parametrize(
        "prev,curr,expected_type",
        [
            (0, 1, "BUY"),
            (-1, 1, "BUY"),                  # precedence
            (0, -1, "SELL"),
            (1, -1, "SELL"),                 # precedence
            (1, 0, "EXIT_FROM_BUY"),
            (-1, 0, "EXIT_FROM_SELL"),
        ],
    )
    def test_all_six_transitions(
        self, db_engine, prev, curr, expected_type,
    ):
        tid, curr_week = self._setup_two_bars(
            db_engine, prev_regime=prev, curr_regime=curr,
        )
        with db_engine.connect() as conn:
            sig = derive_signal_for_ticker_this_cycle(
                conn, tid, now=datetime.now(UTC),
            )
        assert sig is not None
        assert sig.signal_type == expected_type
        assert sig.regime_before == prev
        assert sig.regime_after == curr
        assert sig.ticker_id == tid
        # bar_timestamp on the signal == LATEST state row's bar
        assert sig.bar_timestamp == curr_week

    def test_zero_state_rows_returns_none(self, db_engine):
        """Warmup: no state at all → no signal."""
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
        with db_engine.connect() as conn:
            sig = derive_signal_for_ticker_this_cycle(
                conn, tid, now=datetime.now(UTC),
            )
        assert sig is None

    def test_one_state_row_returns_none(self, db_engine):
        """Warmup: only one state row → can't compare → no signal."""
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            StockIndicatorStateRepository(conn).upsert_one(_state(
                tid,
                bar_timestamp=datetime(2025, 10, 6, tzinfo=UTC),
                display_regime=1,
            ))
        with db_engine.connect() as conn:
            sig = derive_signal_for_ticker_this_cycle(
                conn, tid, now=datetime.now(UTC),
            )
        assert sig is None

    def test_no_daily_bar_returns_none(self, db_engine):
        """Regime change detected, but no daily bar to source
        price_at_signal → don't emit a broken signal."""
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            sr = StockIndicatorStateRepository(conn)
            sr.upsert_one(_state(
                tid,
                bar_timestamp=datetime(2025, 9, 29, tzinfo=UTC),
                display_regime=0,
            ))
            sr.upsert_one(_state(
                tid,
                bar_timestamp=datetime(2025, 10, 6, tzinfo=UTC),
                display_regime=1,
            ))
            # NO daily bar seeded.

        with db_engine.connect() as conn:
            sig = derive_signal_for_ticker_this_cycle(
                conn, tid, now=datetime.now(UTC),
            )
        assert sig is None

    def test_signal_uses_now_for_computed_at(self, db_engine):
        explicit_now = datetime(2026, 1, 15, 14, 30, tzinfo=UTC)
        tid, _ = self._setup_two_bars(
            db_engine, prev_regime=0, curr_regime=1,
        )
        with db_engine.connect() as conn:
            sig = derive_signal_for_ticker_this_cycle(
                conn, tid, now=explicit_now,
            )
        assert sig is not None
        assert sig.computed_at == explicit_now

    def test_signal_uses_latest_daily_close(self, db_engine):
        """price_at_signal = close of latest daily bar."""
        tid, _ = self._setup_two_bars(
            db_engine, prev_regime=0, curr_regime=1, close=257.50,
        )
        with db_engine.connect() as conn:
            sig = derive_signal_for_ticker_this_cycle(
                conn, tid, now=datetime.now(UTC),
            )
        assert sig is not None
        assert sig.price_at_signal == 257.50

    def test_signal_uses_state_timeframe_string(self, db_engine):
        """The emitted signal's timeframe = the value used to query
        state ('1W' for Timeframe.WEEKLY)."""
        tid, _ = self._setup_two_bars(
            db_engine, prev_regime=0, curr_regime=1,
        )
        with db_engine.connect() as conn:
            sig = derive_signal_for_ticker_this_cycle(
                conn, tid, now=datetime.now(UTC),
            )
        assert sig is not None
        assert sig.timeframe == "1W"


class TestOnOrBeforeBackfill:
    """The on_or_before parameter: backfill (Step 11) will use it to
    pin the daily-close lookup to a historical instant."""

    def test_on_or_before_picks_older_close(self, db_engine):
        """Two daily bars; on_or_before pins the older one's close."""
        with transaction(db_engine) as conn:
            tid = _seed_ticker(conn)
            sr = StockIndicatorStateRepository(conn)
            sr.upsert_one(_state(
                tid,
                bar_timestamp=datetime(2025, 9, 29, tzinfo=UTC),
                display_regime=0,
            ))
            sr.upsert_one(_state(
                tid,
                bar_timestamp=datetime(2025, 10, 6, tzinfo=UTC),
                display_regime=1,
            ))
            # OLD daily bar @ close=100
            _seed_daily_bar(
                conn, tid,
                day=datetime(2025, 10, 6, 9, 15, tzinfo=UTC),
                close=100.0,
            )
            # NEW daily bar @ close=200
            _seed_daily_bar(
                conn, tid,
                day=datetime(2025, 10, 8, 9, 15, tzinfo=UTC),
                close=200.0,
            )

        cutoff = datetime(2025, 10, 7, tzinfo=UTC)
        with db_engine.connect() as conn:
            sig = derive_signal_for_ticker_this_cycle(
                conn, tid, now=datetime.now(UTC),
                on_or_before=cutoff,
            )
        assert sig is not None
        # on_or_before excludes the 2025-10-08 bar, leaves the
        # 2025-10-06 one whose close is 100.0
        assert sig.price_at_signal == 100.0


# ============================================================
# Bulk derive
# ============================================================

class TestBulkDerive:

    def test_skips_inactive_tickers(self, db_engine):
        """is_active=False tickers are not iterated."""
        with transaction(db_engine) as conn:
            tid_active = _seed_ticker(
                conn, symbol="ACT", is_active=True,
            )
            tid_inactive = _seed_ticker(
                conn, symbol="INACT", is_active=False,
            )
            sr = StockIndicatorStateRepository(conn)
            for tid in (tid_active, tid_inactive):
                sr.upsert_one(_state(
                    tid,
                    bar_timestamp=datetime(2025, 9, 29, tzinfo=UTC),
                    display_regime=0,
                ))
                sr.upsert_one(_state(
                    tid,
                    bar_timestamp=datetime(2025, 10, 6, tzinfo=UTC),
                    display_regime=1,
                ))
                _seed_daily_bar(
                    conn, tid,
                    day=datetime(2025, 10, 9, 9, 15, tzinfo=UTC),
                )

        with db_engine.connect() as conn:
            signals = derive_signals_for_all_active_stocks(
                conn, now=datetime.now(UTC),
            )
        assert len(signals) == 1
        assert signals[0].ticker_id == tid_active

    def test_skips_warmup_tickers(self, db_engine):
        """Tickers with < 2 state rows produce nothing."""
        with transaction(db_engine) as conn:
            tid_warm = _seed_ticker(conn, symbol="WARM")
            tid_ready = _seed_ticker(conn, symbol="READY")
            sr = StockIndicatorStateRepository(conn)
            # Warmup ticker: only 1 state row
            sr.upsert_one(_state(
                tid_warm,
                bar_timestamp=datetime(2025, 10, 6, tzinfo=UTC),
                display_regime=1,
            ))
            # Ready ticker: 2 state rows + daily bar
            sr.upsert_one(_state(
                tid_ready,
                bar_timestamp=datetime(2025, 9, 29, tzinfo=UTC),
                display_regime=0,
            ))
            sr.upsert_one(_state(
                tid_ready,
                bar_timestamp=datetime(2025, 10, 6, tzinfo=UTC),
                display_regime=1,
            ))
            _seed_daily_bar(
                conn, tid_ready,
                day=datetime(2025, 10, 9, 9, 15, tzinfo=UTC),
            )

        with db_engine.connect() as conn:
            signals = derive_signals_for_all_active_stocks(
                conn, now=datetime.now(UTC),
            )
        assert len(signals) == 1
        assert signals[0].ticker_id == tid_ready

    def test_skips_tickers_with_no_change(self, db_engine):
        """Active, has 2 state rows, but same regime → no signal."""
        with transaction(db_engine) as conn:
            tid_flat = _seed_ticker(conn, symbol="FLAT")
            tid_flip = _seed_ticker(conn, symbol="FLIP")
            sr = StockIndicatorStateRepository(conn)
            # FLAT: regime stayed at +1
            sr.upsert_one(_state(
                tid_flat,
                bar_timestamp=datetime(2025, 9, 29, tzinfo=UTC),
                display_regime=1,
            ))
            sr.upsert_one(_state(
                tid_flat,
                bar_timestamp=datetime(2025, 10, 6, tzinfo=UTC),
                display_regime=1,
            ))
            # FLIP: 0 → +1
            sr.upsert_one(_state(
                tid_flip,
                bar_timestamp=datetime(2025, 9, 29, tzinfo=UTC),
                display_regime=0,
            ))
            sr.upsert_one(_state(
                tid_flip,
                bar_timestamp=datetime(2025, 10, 6, tzinfo=UTC),
                display_regime=1,
            ))
            # Daily bars for both
            for tid in (tid_flat, tid_flip):
                _seed_daily_bar(
                    conn, tid,
                    day=datetime(2025, 10, 9, 9, 15, tzinfo=UTC),
                )

        with db_engine.connect() as conn:
            signals = derive_signals_for_all_active_stocks(
                conn, now=datetime.now(UTC),
            )
        assert len(signals) == 1
        assert signals[0].ticker_id == tid_flip

    def test_returns_sorted_by_ticker_id(self, db_engine):
        """Stable order = ticker_id ascending."""
        with transaction(db_engine) as conn:
            # Insert in non-ticker_id order to make sure sorting is
            # actually happening
            tids = []
            for sym in ("Z_LAST", "A_FIRST", "M_MID"):
                tid = _seed_ticker(conn, symbol=sym)
                tids.append(tid)
                sr = StockIndicatorStateRepository(conn)
                sr.upsert_one(_state(
                    tid,
                    bar_timestamp=datetime(2025, 9, 29, tzinfo=UTC),
                    display_regime=0,
                ))
                sr.upsert_one(_state(
                    tid,
                    bar_timestamp=datetime(2025, 10, 6, tzinfo=UTC),
                    display_regime=1,
                ))
                _seed_daily_bar(
                    conn, tid,
                    day=datetime(2025, 10, 9, 9, 15, tzinfo=UTC),
                )

        with db_engine.connect() as conn:
            signals = derive_signals_for_all_active_stocks(
                conn, now=datetime.now(UTC),
            )
        assert len(signals) == 3
        ids = [s.ticker_id for s in signals]
        assert ids == sorted(ids)

    def test_returns_empty_when_no_tickers(self, db_engine):
        with db_engine.connect() as conn:
            signals = derive_signals_for_all_active_stocks(
                conn, now=datetime.now(UTC),
            )
        assert signals == []
