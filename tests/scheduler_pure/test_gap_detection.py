"""Phase 9 A2 — Gap detection & stale-ticker handling tests.

These tests use lightweight fakes for the DB/repos rather than a real
Postgres, so the gap-detection logic can be validated unit-style. The
end-to-end DB-integrated tests live in test_compute_scheduler.py and
require pytest-postgresql.

Strategy: drive ComputeScheduler.run_once() with a fake db_engine that
returns canned bar lists. Verify:
  - When expected closed weeks have source bars: no gap, normal flow.
  - When a closed week has zero source bars: gap detected, cache entry
    marked stale, ingest skipped.
  - When a previously-stale ticker has data filled in: stale_since is
    cleared, normal flow resumes.
  - Stale flag preserves the original stale_since across multiple passes.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from trip_ema.cache import SignalCache
from trip_ema.cache.signal_cache import build_cached_signal
from trip_ema.domain import (
    BarState,
    ChangeSignal,
    CoreRegime,
    IndicatorParams,
    OHLCVBar,
    TickerRef,
    Timeframe,
)
from trip_ema.engine import TickerEngine
from trip_ema.persistence.tickers_repo import TickerRecord


# -------------------- Fakes -------------------- #


class FakeBarsRepo:
    """Stand-in for BarsRepository that returns pre-canned bar lists."""
    def __init__(
        self,
        *,
        source_bars: dict[tuple[int, str], list[OHLCVBar]] | None = None,
        max_updated_at: datetime | None = None,
        historical_revision_count: int = 0,
    ):
        # Keyed by (ticker_id, timeframe.value)
        self._source_bars = source_bars or {}
        self._max_updated = max_updated_at
        self._historical_count = historical_revision_count

    def fetch_since(self, ticker_id, timeframe, since_exclusive):
        bars = self._source_bars.get((ticker_id, timeframe.value), [])
        return [b for b in bars if b.timestamp > since_exclusive]

    def max_updated_at_since(self, ticker_id, timeframe, history_start_inclusive):
        return self._max_updated

    def weekly_buckets_with_bars(
        self, ticker_id, timeframe, since_inclusive, tz,
    ):
        """Mirrors BarsRepository.weekly_buckets_with_bars by bucketing
        source bars in Python. The real DB version uses date_trunc; we
        use _monday_of which is equivalent for our test inputs."""
        from trip_ema.indicators.aggregation import _monday_of
        bars = self._source_bars.get((ticker_id, timeframe.value), [])
        return {
            _monday_of(b.timestamp, tz=tz)
            for b in bars
            if b.timestamp >= since_inclusive
        }


class FakeRegistry:
    """Minimal registry stub exposing the methods the scheduler calls."""

    def __init__(
        self,
        *,
        ticker_id: int,
        ref: TickerRef,
        engine: TickerEngine,
        tz: str = "UTC",
    ):
        self._engines = {(ref, engine.timeframe): engine}
        self._ticker_ids = {ref: ticker_id}
        self._records = {
            ref: TickerRecord(
                ticker_id=ticker_id,
                ref=ref,
                is_active=True,
                full_name=None,
                tz=tz,
            )
        }

    def all_engines(self):
        return list(self._engines.items())

    def get(self, ref, tf):
        return self._engines.get((ref, tf))

    def get_ticker_id(self, ref):
        return self._ticker_ids.get(ref)

    def get_ticker_meta(self, ref):
        return self._records.get(ref)

    def reprime(self, ref, tf, db_engine):
        # Not exercised in gap-detection tests
        pass


class FakeDBEngine:
    """A db_engine substitute for the scheduler. The scheduler uses
    `db_engine.connect()` and `db_engine.begin()` as context managers
    that yield a Connection. We yield a stub object that BarsRepository
    can ignore because we patch the repository class directly."""

    def __init__(self):
        self.connection_count = 0

    @contextmanager
    def connect(self):
        self.connection_count += 1
        yield MagicMock(name="FakeConnection")

    @contextmanager
    def begin(self):
        self.connection_count += 1
        yield MagicMock(name="FakeConnection")


# -------------------- Builders -------------------- #


def _ticker():
    return TickerRef(exchange="NSE", symbol="NIFTY", instrument="INDEX")


def _build_primed_engine(
    *,
    last_monday: datetime,
    n_weeks_history: int = 40,
    history_retain: int = 200,
) -> TickerEngine:
    """Build a TickerEngine primed with `n_weeks_history` weekly bars
    ending at `last_monday`. The engine's state will have a real
    BarState with `timestamp == last_monday`."""
    ticker = _ticker()
    eng = TickerEngine(
        ticker, Timeframe.WEEKLY, IndicatorParams(),
        history_retain=history_retain,
    )
    bars = []
    for i in range(n_weeks_history):
        ts = last_monday - timedelta(days=7 * (n_weeks_history - 1 - i))
        close = 100.0 + i * 0.5
        bars.append(OHLCVBar(
            timestamp=ts, open=close - 0.5, high=close + 1, low=close - 1,
            close=close, volume=1000.0,
        ))
    eng.prime(bars)
    return eng


def _seed_cache_for_engine(cache: SignalCache, engine: TickerEngine, now: datetime):
    """Seed the cache as prime_all_tickers would."""
    latest = engine.latest_state()
    assert latest is not None
    signal = build_cached_signal(
        ticker=engine.ticker,
        timeframe=engine.timeframe,
        latest_state=latest,
        latest_close=engine.last_close() or 100.0,
        state_history=engine.state_history(),
        last_change=ChangeSignal.NO_CHANGE,
        last_change_bar=None,
        last_change_computed_at=None,
        computed_at=now,
    )
    cache.put(signal)


@dataclass
class _SchedulerHarness:
    """Bundles the moving parts so each test gets a fresh setup."""
    db_engine: FakeDBEngine
    registry: FakeRegistry
    cache: SignalCache
    bars_repo: FakeBarsRepo
    ticker: TickerRef
    engine: TickerEngine


def _make_harness(
    *,
    last_monday: datetime,
    now: datetime,
    source_bars: list[OHLCVBar],
    tz: str = "UTC",
    bars_timeframe: str = "1D",
) -> _SchedulerHarness:
    ticker = _ticker()
    engine = _build_primed_engine(last_monday=last_monday)
    cache = SignalCache()
    _seed_cache_for_engine(cache, engine, now=now - timedelta(hours=1))
    bars_repo = FakeBarsRepo(
        source_bars={(1, bars_timeframe): source_bars},
        max_updated_at=None,    # no revision detection in these tests
    )
    registry = FakeRegistry(
        ticker_id=1, ref=ticker, engine=engine,
        tz=tz,
    )
    db_engine = FakeDBEngine()
    return _SchedulerHarness(
        db_engine=db_engine, registry=registry, cache=cache,
        bars_repo=bars_repo, ticker=ticker, engine=engine,
    )


def _patch_bars_repo(monkeypatch, harness: _SchedulerHarness):
    """Replace BarsRepository(conn) with a factory that returns our fake.
    This is the cleanest seam — the scheduler instantiates BarsRepository
    inline at multiple call sites."""
    def factory(conn):
        return harness.bars_repo
    monkeypatch.setattr(
        "trip_ema.scheduler.compute_scheduler.BarsRepository",
        factory,
    )


# -------------------- Tests -------------------- #


class TestGapDetectionScenarios:
    """End-to-end: drive run_once() and verify gap detection / stale flag."""

    UTC = timezone.utc

    def test_no_gap_when_all_expected_weeks_have_bars(self, monkeypatch):
        """The engine's last_ts is Mon 5/4; now is Tue 5/19. Expected
        closed weeks: 5/11. Provide source bars for 5/11. No gap should
        be detected; stale_since stays None."""
        from trip_ema.scheduler.compute_scheduler import ComputeScheduler

        last_monday = datetime(2026, 5, 4, tzinfo=self.UTC)
        now = datetime(2026, 5, 19, 10, 0, tzinfo=self.UTC)
        source_bars = [
            OHLCVBar(
                timestamp=datetime(2026, 5, 11 + i, tzinfo=self.UTC),
                open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0,
            )
            for i in range(5)   # daily bars for the week of 5/11
        ] + [
            OHLCVBar(
                timestamp=datetime(2026, 5, 18 + i, tzinfo=self.UTC),
                open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0,
            )
            for i in range(2)   # current week, partial
        ]
        h = _make_harness(
            last_monday=last_monday, now=now, source_bars=source_bars,
        )
        _patch_bars_repo(monkeypatch, h)

        sched = ComputeScheduler(
            db_engine=h.db_engine, registry=h.registry, cache=h.cache,
            cadence_minutes=60,
        )

        # Stub the partition extension so it doesn't try to talk to a real DB
        monkeypatch.setattr(sched, "_maybe_extend_partitions", lambda now: None)

        # Patch datetime.now to return our fixed `now`
        import trip_ema.scheduler.compute_scheduler as sched_mod
        original_dt = sched_mod.datetime
        class FakeDT(original_dt):
            @classmethod
            def now(cls, tz=None):
                return now
        monkeypatch.setattr(sched_mod, "datetime", FakeDT)

        summary = sched.run_once()

        # No gaps reported
        assert summary.tickers_with_gaps == []
        # Cache entry's stale_since remains None
        cached = h.cache.get(h.ticker, Timeframe.WEEKLY)
        assert cached is not None
        assert cached.stale_since is None

    def test_gap_detected_when_a_closed_week_has_no_bars(self, monkeypatch):
        """engine_last = Mon 5/4; now = Tue 5/26. Expected closed weeks:
        5/11 and 5/18. Provide bars only for 5/11. Gap = 5/18.
        The ticker should be marked stale; ingest should be skipped."""
        from trip_ema.scheduler.compute_scheduler import ComputeScheduler

        last_monday = datetime(2026, 5, 4, tzinfo=self.UTC)
        now = datetime(2026, 5, 26, 10, 0, tzinfo=self.UTC)
        source_bars = [
            OHLCVBar(
                timestamp=datetime(2026, 5, 11 + i, tzinfo=self.UTC),
                open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0,
            )
            for i in range(5)
        ]
        # NO bars for week of 5/18
        # Current week (5/25-): one bar
        source_bars.append(OHLCVBar(
            timestamp=datetime(2026, 5, 25, tzinfo=self.UTC),
            open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0,
        ))

        h = _make_harness(
            last_monday=last_monday, now=now, source_bars=source_bars,
        )
        _patch_bars_repo(monkeypatch, h)

        sched = ComputeScheduler(
            db_engine=h.db_engine, registry=h.registry, cache=h.cache,
            cadence_minutes=60,
        )
        monkeypatch.setattr(sched, "_maybe_extend_partitions", lambda now: None)
        import trip_ema.scheduler.compute_scheduler as sched_mod
        original_dt = sched_mod.datetime
        class FakeDT(original_dt):
            @classmethod
            def now(cls, tz=None):
                return now
        monkeypatch.setattr(sched_mod, "datetime", FakeDT)

        cached_before = h.cache.get(h.ticker, Timeframe.WEEKLY)
        engine_last_before = h.engine.last_bar_timestamp()

        summary = sched.run_once()

        # Gap is reported
        assert len(summary.tickers_with_gaps) == 1
        ticker_str, missing = summary.tickers_with_gaps[0]
        assert "NIFTY" in ticker_str
        assert len(missing) == 1
        # Cache: latest_state and last_change preserved; stale_since set
        cached_after = h.cache.get(h.ticker, Timeframe.WEEKLY)
        assert cached_after is not None
        assert cached_after.stale_since is not None
        assert cached_after.stale_since == now
        assert cached_after.latest_state == cached_before.latest_state
        # Engine state untouched — ingest was skipped
        assert h.engine.last_bar_timestamp() == engine_last_before
        # Bars ingested counter is unchanged
        assert summary.bars_ingested == 0

    def test_stale_since_preserved_across_passes_while_gap_persists(self, monkeypatch):
        """If the gap isn't filled, subsequent passes must preserve the
        original stale_since timestamp (the moment the gap was first
        observed), not overwrite it with each pass's `now`."""
        from trip_ema.scheduler.compute_scheduler import ComputeScheduler

        last_monday = datetime(2026, 5, 4, tzinfo=self.UTC)
        first_now = datetime(2026, 5, 26, 10, 0, tzinfo=self.UTC)
        source_bars = [
            OHLCVBar(
                timestamp=datetime(2026, 5, 11 + i, tzinfo=self.UTC),
                open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0,
            )
            for i in range(5)
        ]
        # No bars for week of 5/18 — gap

        h = _make_harness(
            last_monday=last_monday, now=first_now, source_bars=source_bars,
        )
        _patch_bars_repo(monkeypatch, h)

        sched = ComputeScheduler(
            db_engine=h.db_engine, registry=h.registry, cache=h.cache,
            cadence_minutes=60,
        )
        monkeypatch.setattr(sched, "_maybe_extend_partitions", lambda now: None)

        import trip_ema.scheduler.compute_scheduler as sched_mod
        original_dt = sched_mod.datetime
        current_now = first_now
        class FakeDT(original_dt):
            @classmethod
            def now(cls, tz=None):
                return current_now
        monkeypatch.setattr(sched_mod, "datetime", FakeDT)

        # Pass 1
        sched.run_once()
        cached_pass_1 = h.cache.get(h.ticker, Timeframe.WEEKLY)
        original_stale_since = cached_pass_1.stale_since
        assert original_stale_since == first_now

        # Pass 2 — one hour later, gap still present
        current_now = first_now + timedelta(hours=1)
        sched.run_once()
        cached_pass_2 = h.cache.get(h.ticker, Timeframe.WEEKLY)
        # stale_since must STILL be the first observation, not the second
        assert cached_pass_2.stale_since == original_stale_since
        assert cached_pass_2.stale_since != current_now

    def test_stale_cleared_when_gap_is_filled(self, monkeypatch):
        """A previously-stale ticker recovers when ETL backfills."""
        from trip_ema.scheduler.compute_scheduler import ComputeScheduler

        last_monday = datetime(2026, 5, 4, tzinfo=self.UTC)
        first_now = datetime(2026, 5, 26, 10, 0, tzinfo=self.UTC)

        # Pass 1: gap (no bars for week of 5/18)
        source_bars_pass1 = [
            OHLCVBar(
                timestamp=datetime(2026, 5, 11 + i, tzinfo=self.UTC),
                open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0,
            )
            for i in range(5)
        ]
        h = _make_harness(
            last_monday=last_monday, now=first_now,
            source_bars=source_bars_pass1,
        )
        _patch_bars_repo(monkeypatch, h)

        sched = ComputeScheduler(
            db_engine=h.db_engine, registry=h.registry, cache=h.cache,
            cadence_minutes=60,
        )
        monkeypatch.setattr(sched, "_maybe_extend_partitions", lambda now: None)
        import trip_ema.scheduler.compute_scheduler as sched_mod
        original_dt = sched_mod.datetime
        current_now = first_now
        class FakeDT(original_dt):
            @classmethod
            def now(cls, tz=None):
                return current_now
        monkeypatch.setattr(sched_mod, "datetime", FakeDT)

        sched.run_once()
        assert h.cache.get(h.ticker, Timeframe.WEEKLY).stale_since == first_now

        # Pass 2 — ETL has filled in week of 5/18
        h.bars_repo._source_bars[(1, "1D")] = source_bars_pass1 + [
            OHLCVBar(
                timestamp=datetime(2026, 5, 18 + i, tzinfo=self.UTC),
                open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0,
            )
            for i in range(5)
        ] + [
            OHLCVBar(
                timestamp=datetime(2026, 5, 25, tzinfo=self.UTC),
                open=100.0, high=101.0, low=99.0, close=100.5, volume=1000.0,
            )
        ]
        current_now = first_now + timedelta(hours=1)
        summary = sched.run_once()

        cached_after = h.cache.get(h.ticker, Timeframe.WEEKLY)
        assert cached_after.stale_since is None, "Stale flag must clear after gap fills"
        assert summary.tickers_recovered_from_gap == 1


class TestStaleSignalsCacheAccessor:
    """SignalCache.stale_signals() returns only entries with stale_since set."""

    def test_returns_empty_when_no_stale_entries(self):
        cache = SignalCache()
        assert cache.stale_signals() == []

    def test_returns_only_stale_entries(self):
        from trip_ema.cache.signal_cache import build_cached_signal
        cache = SignalCache()
        ticker_a = TickerRef("NSE", "AAA", "EQ")
        ticker_b = TickerRef("NSE", "BBB", "EQ")
        # Build minimal valid BarState
        bs = BarState(
            timestamp=datetime(2026, 5, 4, tzinfo=timezone.utc),
            med_ema=100.0, long_ema=100.0, longest_ema=100.0, atr=1.0,
            upper_inner=101.0, lower_inner=99.0,
            upper_mid=102.0, lower_mid=98.0,
            upper_outer=104.0, lower_outer=96.0,
            pierces_upper_mid=False, pierces_lower_mid=False,
            core_bg=CoreRegime.NEUTRAL, display_bg=CoreRegime.NEUTRAL,
        )
        now = datetime(2026, 5, 26, tzinfo=timezone.utc)
        cache.put(build_cached_signal(
            ticker=ticker_a, timeframe=Timeframe.WEEKLY,
            latest_state=bs, latest_close=100.0,
            state_history=[bs], last_change=ChangeSignal.NO_CHANGE,
            last_change_bar=None, last_change_computed_at=None,
            computed_at=now, stale_since=None,
        ))
        cache.put(build_cached_signal(
            ticker=ticker_b, timeframe=Timeframe.WEEKLY,
            latest_state=bs, latest_close=100.0,
            state_history=[bs], last_change=ChangeSignal.NO_CHANGE,
            last_change_bar=None, last_change_computed_at=None,
            computed_at=now, stale_since=now,
        ))

        stale = cache.stale_signals()
        assert len(stale) == 1
        assert stale[0].ticker == ticker_b
