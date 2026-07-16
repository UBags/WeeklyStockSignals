"""B2: three-layer retry routing in _solo_retry_once.

These tests assert on HOW _solo_retry_once dispatches (which repair flag,
which tickers, in which order, on which gate) by INJECTING fakes through the
process_batch_fn and source_factory seams rather than patching module globals.
The contract they pin: intcast tickers go to a repair=False pass FIRST, generic
tickers get repair=True then repair=False, and the shared gate is built with
trip_on_empty=False so the retry cohort's expected empties never manufacture a
phantom backoff window.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from trip_ema.domain import Timeframe
from trip_ema.etl.runner import _BatchStats, _TickerInfo, _solo_retry_once
from trip_ema.etl.types import Tier


def _ticker(tid: int, symbol: str) -> _TickerInfo:
    return _TickerInfo(
        ticker_id=tid, symbol=symbol, tier=Tier.TIER_1, listing_date=None,
    )


def _config():
    """Minimal config carrying only the etl knobs _solo_retry_once reads."""
    etl = SimpleNamespace(
        yfinance_request_delay_seconds=0.0,
        yfinance_throttle_backoff_seconds=1.0,
        yfinance_batch_size=25,
    )
    return SimpleNamespace(etl=etl)


class _FakeSource:
    """Injected via source_factory. Records the kwargs it was built with —
    crucially `repair` and the shared `throttle` gate — so tests can read
    them back without patching YFinanceSource or ThrottleConfig."""

    built: list[dict] = []

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        # Class-level ledger of every source the run constructed.
        _FakeSource.built.append(kwargs)


class _Recorder:
    """Injected via process_batch_fn. Records each call and returns a
    programmed _BatchStats. A ticker "recovers" only if its id is in
    recover_ids, on whichever pass first sees it."""

    def __init__(self, recover_ids: set[int]):
        self.recover_ids = set(recover_ids)
        self.calls: list[dict] = []

    def __call__(self, *, db_engine, config, src, batch, start, end,
                 timeframe, intraday) -> _BatchStats:
        self.calls.append({
            "repair": getattr(src, "repair", None),
            "batch_ids": [t.ticker_id for t in batch],
            "throttle": getattr(src, "throttle", None),
        })
        won = [t for t in batch if t.ticker_id in self.recover_ids]
        lost = [t for t in batch if t.ticker_id not in self.recover_ids]
        return _BatchStats(
            attempted=len(batch),
            succeeded=len(won),
            failed=len(lost),
            bars_written=len(won),
            failed_tickers=tuple(lost),
        )


@pytest.fixture(autouse=True)
def _reset_source_ledger():
    _FakeSource.built = []
    yield
    _FakeSource.built = []


def _run(recorder, *, failed, intcast_ids):
    _solo_retry_once(
        db_engine=object(),
        config=_config(),
        exchange="NSE",
        failed_tickers=failed,
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        timeframe=Timeframe.DAILY,
        intraday=False,
        intcast_ids=intcast_ids,
        process_batch_fn=recorder,
        source_factory=_FakeSource,
    )


class TestLayerRouting:

    def test_intcast_goes_repair_false_first(self):
        rec = _Recorder(recover_ids={1})
        _run(rec, failed=[_ticker(1, "CONCOR")], intcast_ids={1})
        repairs = [c["repair"] for c in rec.calls if 1 in c["batch_ids"]]
        assert repairs, "intcast ticker was never fetched"
        assert all(r is False for r in repairs)
        assert True not in repairs

    def test_generic_goes_repair_true_then_false(self):
        rec = _Recorder(recover_ids=set())  # never recovers
        _run(rec, failed=[_ticker(2, "FLAKE")], intcast_ids=set())
        repairs = [c["repair"] for c in rec.calls if 2 in c["batch_ids"]]
        assert True in repairs, "generic never got repair=True Layer 2"
        assert False in repairs, "generic never got repair=False Layer 3"
        assert repairs.index(True) < repairs.index(False)

    def test_generic_recovered_in_layer2_skips_layer3(self):
        rec = _Recorder(recover_ids={3})  # recovers on first sight
        _run(rec, failed=[_ticker(3, "RECOVERS")], intcast_ids=set())
        repairs = [c["repair"] for c in rec.calls if 3 in c["batch_ids"]]
        assert repairs == [True], (
            f"expected one repair=True call, got {repairs}"
        )

    def test_intcast_and_generic_are_partitioned(self):
        rec = _Recorder(recover_ids=set())  # nobody recovers
        _run(
            rec,
            failed=[_ticker(10, "INTCAST"), _ticker(20, "GENERIC")],
            intcast_ids={10},
        )
        ic = [c["repair"] for c in rec.calls if 10 in c["batch_ids"]]
        gen = [c["repair"] for c in rec.calls if 20 in c["batch_ids"]]
        assert ic and all(r is False for r in ic)
        assert True in gen and False in gen


class TestGateConfig:

    def test_shared_gate_built_with_trip_on_empty_false(self):
        """D2 contract: the recovery gate disables shape-based empty
        tripping. Observed via the gate the fake source was handed —
        no ThrottleConfig patch."""
        rec = _Recorder(recover_ids=set())
        _run(rec, failed=[_ticker(1, "X")], intcast_ids=set())
        gates = {id(c["throttle"]): c["throttle"] for c in rec.calls}
        assert gates, "no source was constructed with a gate"
        # One shared gate across the whole recovery.
        assert len(gates) == 1
        gate = next(iter(gates.values()))
        assert gate._config.trip_on_empty is False


class TestBounds:

    def test_empty_failed_list_is_a_noop(self):
        rec = _Recorder(recover_ids=set())
        _run(rec, failed=[], intcast_ids=set())
        assert rec.calls == []

    def test_no_intcast_ids_still_runs_generic_layers(self):
        """intcast_ids omitted (None default) behaves like an empty set:
        everything generic, no intcast layer fires."""
        rec = _Recorder(recover_ids=set())
        _solo_retry_once(
            db_engine=object(), config=_config(), exchange="NSE",
            failed_tickers=[_ticker(7, "PLAIN")],
            start=date(2026, 1, 1), end=date(2026, 1, 31),
            timeframe=Timeframe.DAILY, intraday=False,
            process_batch_fn=rec, source_factory=_FakeSource,
        )
        repairs = [c["repair"] for c in rec.calls if 7 in c["batch_ids"]]
        assert True in repairs and False in repairs
