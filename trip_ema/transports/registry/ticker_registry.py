"""TickerRegistry — the warm in-memory cache of TickerEngines.

Each (ticker, timeframe) gets one TickerEngine, primed at startup with the
last N historical bars and updated incrementally by the ComputeScheduler.
The registry is read by the scheduler to iterate active tickers and by
the API layer to fetch a specific engine.

Threading:
    The registry is written to only during startup (prime_all) and never
    again. Subsequent access is read-only — the engines themselves are
    mutated by the ComputeScheduler, but the registry's dict structure
    is stable. So no locking is needed on the registry itself; locking
    happens at the SignalCache boundary instead.

    If we ever need to add tickers at runtime (e.g. user adds a new
    symbol via API), we'll add a registration lock then.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..domain import IndicatorParams, OHLCVBar, TickerRef, Timeframe
from ..engine import TickerEngine
from ..indicators.aggregation import daily_to_weekly
from ..persistence import (
    BarsRepository,
    TickerRecord,
    TickersRepository,
    transaction,
)
from sqlalchemy import Engine

logger = logging.getLogger(__name__)


# Cache key: (ticker, timeframe). A single ticker can have engines at
# multiple timeframes (e.g. weekly for trading signals, daily for short-term).
EngineKey = tuple[TickerRef, Timeframe]


@dataclass(frozen=True, slots=True)
class PrimeResult:
    """Summary of one prime_all run, useful for logging and tests."""
    tickers_primed: int
    tickers_skipped_no_bars: int
    tickers_failed: int


class TickerRegistry:
    """Map of (TickerRef, Timeframe) -> TickerEngine.

    Lifecycle:
        registry = TickerRegistry(params=..., timeframes=[WEEKLY], history_limit=500)
        registry.prime_all(db_engine)        # at startup
        engine = registry.get(ref, WEEKLY)   # at every request
    """

    def __init__(
        self,
        params: IndicatorParams,
        timeframes: list[Timeframe],
        *,
        history_limit: int = 500,
        state_history_retain: int = 200,
    ):
        self._params = params
        self._timeframes = list(timeframes)
        self._history_limit = history_limit
        self._state_history_retain = state_history_retain
        self._engines: dict[EngineKey, TickerEngine] = {}
        # Side-channel: map (TickerRef -> ticker_id) so the scheduler doesn't
        # have to re-query the DB on every compute pass.
        self._ticker_ids: dict[TickerRef, int] = {}
        # B4: per-ticker metadata needed by aggregation logic.
        self._ticker_meta: dict[TickerRef, TickerRecord] = {}

    # ---------- Lifecycle ---------- #

    def prime_all(self, db_engine: Engine) -> PrimeResult:
        """For every active ticker × configured timeframe, create a
        TickerEngine and prime it with the last N bars from the DB.

        B4: When the engine's target timeframe is WEEKLY and the
        ticker's source_timeframe is finer, this method fetches source
        bars and aggregates them to weekly (using the ticker's tz for
        the week anchor) before priming. When source == target, bars
        are used directly.

        Tickers with no bars are skipped (logged, not failed). Tickers
        that raise during priming are caught individually so one bad
        ticker doesn't break startup."""
        primed = skipped = failed = 0

        with db_engine.connect() as conn:
            tickers_repo = TickersRepository(conn)
            ticker_records: list[TickerRecord] = tickers_repo.find_active()

        for record in ticker_records:
            self._ticker_ids[record.ref] = record.ticker_id
            self._ticker_meta[record.ref] = record
            for tf in self._timeframes:
                try:
                    bars = self._fetch_and_aggregate_for_engine(
                        db_engine, record, tf,
                    )
                    if not bars:
                        skipped += 1
                        logger.debug(
                            "No bars for %s at %s; skipping engine",
                            record.ref, tf.value,
                        )
                        continue
                    engine = TickerEngine(
                        record.ref, tf, self._params,
                        history_retain=self._state_history_retain,
                    )
                    engine.prime(bars)
                    self._engines[(record.ref, tf)] = engine
                    primed += 1
                except Exception:
                    failed += 1
                    logger.exception(
                        "Failed to prime engine for %s @ %s",
                        record.ref, tf.value,
                    )

        logger.info(
            "Registry primed: %d engines, %d skipped (no bars), %d failed",
            primed, skipped, failed,
        )
        return PrimeResult(
            tickers_primed=primed,
            tickers_skipped_no_bars=skipped,
            tickers_failed=failed,
        )

    def _fetch_and_aggregate_for_engine(
        self,
        db_engine: Engine,
        record: TickerRecord,
        engine_tf: Timeframe,
    ) -> list[OHLCVBar]:
        """Fetch bars at the ticker's source resolution and aggregate
        to the engine's target timeframe.

        Three cases:
          1. source == target (e.g. weekly engine, weekly source):
             fetch directly. No aggregation.
          2. source finer than target, target == WEEKLY: fetch source
             bars, aggregate via daily_to_weekly() using the ticker's
             tz for week anchor. Note `daily_to_weekly` despite the
             name handles any finer-than-weekly source — it just uses
             `_monday_of` to bucket each bar.
          3. anything else: not currently supported (e.g. hourly engine
             from 15-min source). Raises NotImplementedError.
        """
        source_tf = Timeframe(record.source_timeframe)
        with db_engine.connect() as conn:
            source_bars = BarsRepository(conn).fetch_history(
                record.ticker_id, source_tf, limit=self._history_limit,
            )
        if not source_bars:
            return []

        if source_tf == engine_tf:
            return source_bars
        if engine_tf == Timeframe.WEEKLY:
            return daily_to_weekly(
                source_bars,
                include_partial_last_week=True,
                tz=record.tz,
            )
        raise NotImplementedError(
            f"Aggregation from {source_tf.value} to {engine_tf.value} "
            f"is not implemented"
        )

    def reprime(
        self,
        ref: TickerRef,
        timeframe: Timeframe,
        db_engine: Engine,
        *,
        refresh_metadata: bool = False,
    ) -> bool:
        """Rebuild the engine for a single (ticker, timeframe) from raw
        bars in the DB. Used by ComputeScheduler when it detects a
        historical revision (a bar OLDER than the engine's last_ts was
        updated in the DB).

        Uses the same source-fetch-and-aggregate logic as prime_all,
        so engines with source_timeframe != target stay correct.

        Args:
            refresh_metadata: When True, force-reload `TickerRecord` from
                the DB even if a cached one exists. This catches tz /
                source_timeframe / is_active changes that an operator
                applied via direct DB update or a future admin endpoint.
                Defaults False to preserve the scheduler's
                revision-detection call-site behavior, where the
                metadata isn't expected to have changed and an extra
                DB round-trip per re-prime is wasteful.

        Returns True if the engine was rebuilt, False if the ticker has
        no bars (in which case the engine is removed from the registry).
        """
        ticker_id = self._ticker_ids.get(ref)
        if ticker_id is None:
            raise KeyError(f"Unknown ticker: {ref}")

        if refresh_metadata:
            # Force-reload from DB; ignore any cached record. This is
            # the path an admin endpoint should take.
            with db_engine.connect() as conn:
                record = TickersRepository(conn).get_by_id(ticker_id)
            if record is None:
                raise KeyError(f"Ticker disappeared from DB: {ref}")
            self._ticker_meta[ref] = record
        else:
            record = self._ticker_meta.get(ref)
            if record is None:
                # Defensive — _ticker_meta is populated in lockstep with
                # _ticker_ids during prime_all. Refresh from DB if missing.
                with db_engine.connect() as conn:
                    record = TickersRepository(conn).get_by_id(ticker_id)
                if record is None:
                    raise KeyError(f"Ticker disappeared from DB: {ref}")
                self._ticker_meta[ref] = record

        bars = self._fetch_and_aggregate_for_engine(db_engine, record, timeframe)
        if not bars:
            self._engines.pop((ref, timeframe), None)
            logger.warning(
                "Re-prime requested for %s@%s but no bars in DB; engine removed",
                ref, timeframe.value,
            )
            return False
        new_engine = TickerEngine(
            ref, timeframe, self._params,
            history_retain=self._state_history_retain,
        )
        new_engine.prime(bars)
        self._engines[(ref, timeframe)] = new_engine
        logger.info(
            "Re-primed engine for %s@%s with %d bars",
            ref, timeframe.value, len(bars),
        )
        return True

    # ---------- Read accessors ---------- #

    def get(self, ref: TickerRef, timeframe: Timeframe) -> TickerEngine | None:
        return self._engines.get((ref, timeframe))

    def get_ticker_id(self, ref: TickerRef) -> int | None:
        """The DB ticker_id corresponding to a ref. Populated during
        prime_all. Used by the scheduler to avoid re-querying."""
        return self._ticker_ids.get(ref)

    def get_ticker_meta(self, ref: TickerRef) -> TickerRecord | None:
        """The TickerRecord (with tz, source_timeframe, etc.) for a
        ref. Populated during prime_all. Used by the scheduler to know
        the ticker's timezone for week-anchor computation."""
        return self._ticker_meta.get(ref)

    def all_engines(self) -> list[tuple[EngineKey, TickerEngine]]:
        """Snapshot of (key, engine) tuples for iteration.
        Returns a list so callers can iterate safely even if the registry
        gets mutated mid-loop (it won't, today)."""
        return list(self._engines.items())

    def __len__(self) -> int:
        return len(self._engines)

    def __contains__(self, key: EngineKey) -> bool:
        return key in self._engines

    # ---------- Test/utility helpers ---------- #

    def register_engine(
        self,
        ref: TickerRef,
        timeframe: Timeframe,
        engine: TickerEngine,
        *,
        ticker_id: int | None = None,
    ) -> None:
        """Directly register a pre-built engine. Used by tests and by code
        paths that build engines outside the DB-priming flow."""
        self._engines[(ref, timeframe)] = engine
        if ticker_id is not None:
            self._ticker_ids[ref] = ticker_id
