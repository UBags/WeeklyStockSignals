"""SignalsReader — V1 read path for last-N-signals queries.

The single sanctioned consumer for "last N signals for ticker X"
queries. Uses a `current_signals` → `historical_signals` fallback
strategy: at most one row from current_signals (the cycle's latest
signal for this ticker, if any), plus N-1 (or N) from
historical_signals.

**No UNION** between the two tables. Two separate queries,
composed in Python. This matches non-negotiable N6 of
CLEAN_PIPELINE_REFACTOR.

In Step 5 of the V1 cutover plan, `SignalCache.rebuild_from`
will mirror `current_signals` exactly. When that lands, this
reader will switch its "current" lookup to the cache (perf
optimization — avoid the DB hit for the cache-hit path). The
public API stays the same.
"""
from __future__ import annotations

from ..domain.signal_event import SignalEvent
from ..persistence.current_signals_repo import CurrentSignalsRepository
from ..persistence.historical_signals_repo import (
    HistoricalSignalsRepository,
)


class SignalsReader:
    """Cache-first (current_signals) + historical fallback reader.

    Construction takes the two repos already bound to a Connection.
    Callers manage the transaction; SignalsReader is stateless
    beyond its repo references.
    """

    def __init__(
        self,
        current_repo: CurrentSignalsRepository,
        historical_repo: HistoricalSignalsRepository,
    ):
        self._current = current_repo
        self._historical = historical_repo

    def last_n_for_ticker(
        self, ticker_id: int, n: int,
    ) -> list[SignalEvent]:
        """Return up to N most-recent signals for one ticker.

        Strategy:
            - Check current_signals for this ticker. If a row is
              there, it's the freshest signal (just fired this
              cycle). Take it.
            - Top up from historical_signals (newest-first by
              bar_timestamp DESC, computed_at DESC) for the
              remaining (n - 1) rows.
            - If current_signals doesn't have a row, take all n
              from historical_signals.

        Edge cases:
            - n == 0 → returns []. No queries issued.
            - n < 0 → raises ValueError.
            - Ticker has fewer than n total signals → returns
              what's available (no padding, no error).
        """
        if n < 0:
            raise ValueError(f"n must be >= 0, got {n}")
        if n == 0:
            return []

        current = self._current.get_for_ticker(ticker_id)

        if current is not None:
            if n == 1:
                return [current]
            historical = self._historical.get_for_ticker(
                ticker_id, limit=n - 1,
            )
            return [current] + historical

        return self._historical.get_for_ticker(ticker_id, limit=n)

    def latest_for_ticker(
        self, ticker_id: int,
    ) -> SignalEvent | None:
        """Return the most recent signal for one ticker, if any.

        Equivalent to `last_n_for_ticker(ticker_id, 1)[0]` when a
        signal exists, but slightly cheaper — one DB hit on cache
        miss instead of two.
        """
        current = self._current.get_for_ticker(ticker_id)
        if current is not None:
            return current

        results = self._historical.get_for_ticker(
            ticker_id, limit=1,
        )
        return results[0] if results else None
