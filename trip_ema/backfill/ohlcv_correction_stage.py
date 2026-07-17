"""Backfill stage: correct botched / seam-present OHLCV in place.

Runs after the (auto_adjust=False) fetch and before the indicator recompute.
For each scoped ticker with nselib corporate actions, it loads the stored
daily bars, reconciles them against the authoritative factors via the seam
corrector, and writes the CORRECTED bars back to ohlcv_bars — model (A):
ohlcv_bars becomes the corrected series that every downstream consumer (the
signal walk AND the ML label table) reads.

Only bars whose values actually changed are written (the pre-boundary leg of a
SEAM_PRESENT / BOTCHED splice); a ticker whose series is already ADJUSTED_OK is
left untouched. `adj_close` is preserved (upsert_bar leaves it alone when not
passed) — correcting the parked dividend-adjusted column waits on the §8
adj_close-storage change.

Censored windows (demerger / capital-reduction / unexplained) are counted and
logged; wiring them into the Monday walk as `window_censored` is a follow-on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..domain import Timeframe
from ..domain.market_session import market_tz
from ..domain.ohlcv import OHLCVBar
from ..domain.ohlcv_corrector import CorrectionConfig, CorrectionResult, PriceBar
from ..persistence import transaction
from ..persistence.bars_repo import BarsRepository
from ..etl.nse_corporate_actions_repo import NseCorporateActionsRepository
from ..etl.split_reconcile import correct_ticker

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CorrectionStageSummary:
    tickers_scanned: int        # tickers that had nselib actions
    tickers_corrected: int      # tickers where at least one bar changed
    bars_written: int
    censored_windows: int


def corrected_bar_writes(
    orig_bars: list[OHLCVBar],
    result: CorrectionResult,
    tz,
) -> list[OHLCVBar]:
    """The corrected bars whose OHLCV actually differs from the stored bar.

    Matched by exchange-local calendar day (the corrector keys on day; the
    stored bar carries the tz-aware instant we must write back to). Pure —
    the money-path decision of *which* bars to rewrite is unit-tested here.
    """
    orig_by_day = {b.timestamp.astimezone(tz).date(): b for b in orig_bars}
    writes: list[OHLCVBar] = []
    for pb in result.bars:
        ob = orig_by_day.get(pb.day)
        if ob is None:
            continue
        if (pb.open, pb.high, pb.low, pb.close, pb.volume) != (
            ob.open, ob.high, ob.low, ob.close, ob.volume
        ):
            writes.append(
                OHLCVBar(
                    timestamp=ob.timestamp,
                    open=pb.open, high=pb.high, low=pb.low,
                    close=pb.close, volume=pb.volume,
                )
            )
    return writes


def correct_ohlcv_stage(
    engine,
    *,
    exchange: str = "NSE",
    scoped_tickers: list[int] | tuple[int, ...],
    config: CorrectionConfig | None = None,
    bar_limit: int = 100_000,
    min_bars: int = 3,
    transaction=transaction,
    make_actions_repo=NseCorporateActionsRepository,
    make_bars_repo=BarsRepository,
) -> CorrectionStageSummary:
    """Correct the daily series for every scoped ticker that has nselib
    actions, persisting the corrected bars. Idempotent: re-running on an
    already-corrected series is a no-op (seams measure ≈1 → ADJUSTED_OK →
    no writes).

    The transaction opener and the two repositories are injected (defaulting
    to the production collaborators) so the stage is testable without patching
    — inject fakes, per the closed-seam discipline.
    """
    tz = market_tz(exchange)
    scanned = corrected = bars_written = censored = 0

    with transaction(engine) as conn:
        actions_repo = make_actions_repo(conn)
        bars_repo = make_bars_repo(conn)

        for ticker_id in scoped_tickers:
            events = actions_repo.factor_events_for_ticker(ticker_id)
            if not events:
                continue
            scanned += 1

            orig = bars_repo.fetch_history(
                ticker_id, Timeframe.DAILY, limit=bar_limit
            )
            if len(orig) < min_bars:
                continue

            pricebars = [
                PriceBar(
                    day=b.timestamp.astimezone(tz).date(),
                    open=b.open, high=b.high, low=b.low,
                    close=b.close, volume=b.volume,
                )
                for b in orig
            ]
            result = correct_ticker(pricebars, events, config=config)
            censored += len(result.censored)

            writes = corrected_bar_writes(orig, result, tz)
            if not writes:
                continue
            for bar in writes:
                bars_repo.upsert_bar(ticker_id, Timeframe.DAILY, bar)
            bars_written += len(writes)
            corrected += 1

    summary = CorrectionStageSummary(
        tickers_scanned=scanned,
        tickers_corrected=corrected,
        bars_written=bars_written,
        censored_windows=censored,
    )
    logger.info(
        "correct_ohlcv stage [%s]: scanned=%d corrected=%d bars_written=%d "
        "censored_windows=%d",
        exchange, summary.tickers_scanned, summary.tickers_corrected,
        summary.bars_written, summary.censored_windows,
    )
    return summary
