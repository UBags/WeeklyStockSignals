"""Retry layers re-batch at yfinance_batch_size // 2, single-threaded.

Both cohorts -- the generic failed/empty stragglers and the IntCasting
casualties -- are chunked at half the bulk batch size. Every layer runs on the
calling thread; the retry path must never fan out, because it is one download
PER CHUNK against a limiter that has already pushed back once.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace

from trip_ema.domain import Timeframe
from trip_ema.etl.runner import _solo_retry_once, _TickerInfo
from trip_ema.etl.types import Tier


@dataclass
class _Stats:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    bars_written: int = 0
    failed_tickers: tuple = ()
    empty_tickers: tuple = ()
    short_tickers: tuple = ()
    intcast_tickers: tuple = ()


def _config(batch_size: int) -> SimpleNamespace:
    return SimpleNamespace(etl=SimpleNamespace(
        yfinance_batch_size=batch_size,
        yfinance_request_delay_seconds=0.0,
        yfinance_throttle_backoff_seconds=1.0,
        yfinance_min_request_gap_seconds=0.0,
    ))


def _tickers(n: int, start_id: int = 1) -> list[_TickerInfo]:
    return [
        _TickerInfo(
            ticker_id=start_id + i,
            symbol=f"SYM{start_id + i}",
            tier=Tier.TIER_3,
            listing_date=date(2010, 1, 1),
        )
        for i in range(n)
    ]


class _Recorder:
    """Records every chunk handed to _process_one_batch, and its thread."""

    def __init__(self, *, succeed: bool) -> None:
        self.chunks: list[list[_TickerInfo]] = []
        self.threads: set[int] = set()
        self._succeed = succeed

    def __call__(self, *, batch, **_kwargs) -> _Stats:
        self.chunks.append(list(batch))
        self.threads.add(threading.get_ident())
        if self._succeed:
            return _Stats(attempted=len(batch), succeeded=len(batch))
        return _Stats(
            attempted=len(batch), succeeded=0, failed=0,
            empty_tickers=tuple(batch),
        )

    @property
    def sizes(self) -> list[int]:
        return [len(c) for c in self.chunks]


def _run(recorder, *, snapshot, intcast_ids, batch_size) -> None:
    _solo_retry_once(
        None,
        _config(batch_size),
        exchange="NSE",
        failed_tickers=snapshot,
        start=date(2020, 1, 1),
        end=date(2020, 2, 1),
        timeframe=Timeframe.DAILY,
        intraday=False,
        intcast_ids=intcast_ids,
        process_batch_fn=recorder,
        source_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        parked_lookup=lambda *_a, **_k: set(),
        park_fn=lambda *_a, **_k: None,
    )


def test_generic_cohort_is_chunked_at_half_the_batch_size() -> None:
    rec = _Recorder(succeed=True)
    _run(rec, snapshot=_tickers(25), intcast_ids=set(), batch_size=10)
    # 10 // 2 == 5 -> 25 tickers -> 5 chunks of 5.
    assert rec.sizes == [5, 5, 5, 5, 5]


def test_intcast_cohort_is_chunked_at_half_the_batch_size() -> None:
    snapshot = _tickers(12)
    rec = _Recorder(succeed=True)
    _run(
        rec,
        snapshot=snapshot,
        intcast_ids={t.ticker_id for t in snapshot},
        batch_size=10,
    )
    assert rec.sizes == [5, 5, 2]


def test_layer3_survivors_are_chunked_at_half_the_batch_size() -> None:
    """Nothing recovers, so L2 hands its whole cohort to L3 -- and L3 must
    chunk at the same half-size, not one ticker at a time."""
    rec = _Recorder(succeed=False)
    _run(rec, snapshot=_tickers(6), intcast_ids=set(), batch_size=8)
    # 8 // 2 == 4. L2: [4, 2], then L3 over the same 6 survivors: [4, 2].
    assert rec.sizes == [4, 2, 4, 2]


def test_every_retry_layer_runs_on_the_calling_thread() -> None:
    rec = _Recorder(succeed=False)
    snapshot = _tickers(9)
    _run(
        rec,
        snapshot=snapshot,
        intcast_ids={snapshot[0].ticker_id, snapshot[1].ticker_id},
        batch_size=6,
    )
    assert rec.threads == {threading.get_ident()}


def test_batch_size_of_one_does_not_collapse_to_zero() -> None:
    """1 // 2 == 0; the floor must keep chunks non-empty or the pass loops."""
    rec = _Recorder(succeed=True)
    _run(rec, snapshot=_tickers(3), intcast_ids=set(), batch_size=1)
    assert rec.sizes == [1, 1, 1]
