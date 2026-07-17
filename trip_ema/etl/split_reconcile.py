"""Reconcile the yfinance-adjusted daily series against the authoritative
nselib corporate actions, per ticker.

This is the bridge between the nselib actions table and the OHLCV seam
corrector, and it backs two callers:

  * the **survey** (`reconcile_ticker` / `reconcile_all`) — report-only,
    tallies BOTCHED / SEAM_PRESENT / … to find which tickers need fixing;
  * the **pipeline correction** (`correct_ticker`) — same measurement, but
    returns the corrected bars + the censored windows to apply.

Both share ONE classification path (the corrector's snap logic), so the
survey and the fix can never drift apart.

Composition rule: several nselib scale events can fall on one ex_date
(a split AND a bonus). They are composed multiplicatively into a SINGLE
boundary factor before the corrector sees them, so the corrector measures
one seam per date. Demerger / capital-reduction events don't measure — they
emit a censor window around their ex_date directly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..domain import Timeframe
from ..domain.market_session import market_tz
from ..domain.ohlcv_corrector import (
    BoundaryReport,
    CensorWindow,
    CorrectionConfig,
    CorrectionResult,
    PriceBar,
    SplitAction,
    Verdict,
    reconcile_and_correct,
)
from ..persistence import transaction
from ..persistence.bars_repo import BarsRepository
from ..persistence.tickers_repo import TickersRepository
from .nse_corporate_actions_repo import (
    FactorEvent,
    NseCorporateActionsRepository,
)

logger = logging.getLogger(__name__)

# The non-clean verdicts — a ticker with any of these needs attention.
BACKLOG_VERDICTS = frozenset({
    Verdict.SEAM_PRESENT,
    Verdict.BOTCHED,
    Verdict.UNEXPLAINED,
    Verdict.AMBIGUOUS_REVIEW,
})


# --------------------------------------------------------------------------- #
# pure core
# --------------------------------------------------------------------------- #


def build_split_actions_and_censors(
    events: Iterable[FactorEvent],
    *,
    censor_pad_days: int = 3,
) -> tuple[list[SplitAction], list[CensorWindow]]:
    """Group a ticker's nselib factor events into corrector inputs.

    Scale events (price_factor set) are composed multiplicatively per ex_date
    into one SplitAction. Censor events (demerger / capital_reduction) become
    censor windows padded ``censor_pad_days`` either side of the ex_date.
    """
    composed: dict[date, float] = {}
    censors: list[CensorWindow] = []
    for e in events:
        if e.price_factor is not None:
            composed[e.ex_date] = composed.get(e.ex_date, 1.0) * e.price_factor
        elif e.censor:
            censors.append(
                CensorWindow(
                    e.ex_date - timedelta(days=censor_pad_days),
                    e.ex_date + timedelta(days=censor_pad_days),
                    f"nselib {e.action_type} on {e.ex_date.isoformat()}",
                )
            )
    actions = [
        SplitAction(effective_date=d, factor=f, kind="split")
        for d, f in sorted(composed.items())
    ]
    return actions, censors


def correct_ticker(
    bars: Sequence[PriceBar],
    events: Iterable[FactorEvent],
    *,
    config: CorrectionConfig | None = None,
    censor_pad_days: int = 3,
) -> CorrectionResult:
    """Run the corrector over a ticker's bars using nselib factors, and fold
    in the demerger / capital-reduction censor windows. Returns corrected bars
    + per-boundary reports + all censored windows (corrector-derived and
    nselib value-transfer events)."""
    actions, nselib_censors = build_split_actions_and_censors(
        events, censor_pad_days=censor_pad_days
    )
    result = reconcile_and_correct(list(bars), actions, config)
    if not nselib_censors:
        return result
    merged = tuple(
        sorted(result.censored + tuple(nselib_censors), key=lambda w: w.start)
    )
    return CorrectionResult(result.bars, result.reports, merged)


def reconcile_ticker(
    bars: Sequence[PriceBar],
    events: Iterable[FactorEvent],
    *,
    config: CorrectionConfig | None = None,
) -> list[BoundaryReport]:
    """Report-only: the per-boundary verdicts, without keeping corrected bars."""
    return list(correct_ticker(bars, events, config=config).reports)


# --------------------------------------------------------------------------- #
# survey runner (find botched / seam_present tickers)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TickerVerdict:
    ticker_id: int
    symbol: str
    ex_date: date
    factor: float
    verdict: str
    measured_r: float | None


@dataclass(frozen=True, slots=True)
class ReconcileSummary:
    tickers_scanned: int
    tickers_with_actions: int
    tally: dict[str, int]
    backlog: list[TickerVerdict] = field(default_factory=list)


def reconcile_all(
    list_tickers: Callable[[], list[tuple[int, str]]],
    load_events: Callable[[int], list[FactorEvent]],
    load_bars: Callable[[int], list[PriceBar]],
    *,
    config: CorrectionConfig | None = None,
) -> ReconcileSummary:
    """Iterate tickers, reconcile each, tally verdicts, collect the backlog
    (the non-clean boundaries). Injected loaders keep this DB-free for tests."""
    tally: dict[str, int] = {}
    backlog: list[TickerVerdict] = []
    with_actions = 0
    tickers = list_tickers()
    for ticker_id, symbol in tickers:
        events = load_events(ticker_id)
        if not events:
            continue
        with_actions += 1
        bars = load_bars(ticker_id)
        for rep in reconcile_ticker(bars, events, config=config):
            tally[rep.verdict.value] = tally.get(rep.verdict.value, 0) + 1
            if rep.verdict in BACKLOG_VERDICTS:
                backlog.append(
                    TickerVerdict(
                        ticker_id=ticker_id,
                        symbol=symbol,
                        ex_date=rep.boundary or rep.effective_date,
                        factor=rep.factor,
                        verdict=rep.verdict.value,
                        measured_r=rep.measured_r,
                    )
                )
    return ReconcileSummary(
        tickers_scanned=len(tickers),
        tickers_with_actions=with_actions,
        tally=tally,
        backlog=backlog,
    )


def _bars_to_pricebars(bars, tz) -> list[PriceBar]:
    """OHLCVBar (tz-aware instant) -> PriceBar keyed by the exchange-local
    calendar day. A daily bar's timestamp is local-midnight in the exchange
    zone, so the local date is the trading day."""
    out = [
        PriceBar(
            day=b.timestamp.astimezone(tz).date(),
            open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume,
        )
        for b in bars
    ]
    out.sort(key=lambda p: p.day)
    return out


def _active_tickers_for_exchange(tickers_repo, exchange: str) -> list[tuple[int, str]]:
    """(ticker_id, symbol) for active tickers on `exchange`.

    TickerRecord carries the exchange/symbol on its `ref` (a TickerRef), not
    as top-level attributes — kept as a named helper so the access path is
    unit-tested rather than only exercised against a live DB.
    """
    return [
        (t.ticker_id, t.ref.symbol)
        for t in tickers_repo.find_active()
        if t.ref.exchange == exchange
    ]


def run_split_reconcile(
    engine,
    *,
    exchange: str = "NSE",
    config: CorrectionConfig | None = None,
    bar_limit: int = 100_000,
) -> ReconcileSummary:
    """Production survey: for every active ticker on `exchange`, reconcile the
    stored daily series against its nselib actions and report the backlog.
    Read-only."""
    tz = market_tz(exchange)
    with transaction(engine) as conn:
        actions_repo = NseCorporateActionsRepository(conn)
        bars_repo = BarsRepository(conn)
        tickers_repo = TickersRepository(conn)

        def list_tickers() -> list[tuple[int, str]]:
            return _active_tickers_for_exchange(tickers_repo, exchange)

        def load_events(ticker_id: int) -> list[FactorEvent]:
            return actions_repo.factor_events_for_ticker(ticker_id)

        def load_bars(ticker_id: int) -> list[PriceBar]:
            bars = bars_repo.fetch_history(
                ticker_id, Timeframe.DAILY, limit=bar_limit
            )
            return _bars_to_pricebars(bars, tz)

        summary = reconcile_all(
            list_tickers, load_events, load_bars, config=config
        )
    logger.info(
        "split reconcile [%s]: scanned=%d with_actions=%d tally=%s backlog=%d",
        exchange, summary.tickers_scanned, summary.tickers_with_actions,
        summary.tally, len(summary.backlog),
    )
    return summary
