"""B2: three-layer retry routing in _solo_retry_once.

These tests replace the module-level _process_one_batch with a recorder, so
they assert on HOW _solo_retry_once dispatches (which repair flag, which
tickers, in which order) without a DB or yfinance. The one integration point
they pin is the contract B2 depends on: intcast tickers go to a repair=False
pass FIRST, generic tickers get repair=True then repair=False, and the shared
gate is constructed with trip_on_empty=False so the retry cohort's expected
empties never manufacture a phantom backoff window.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from trip_ema.domain import Timeframe
from trip_ema.etl import runner as R
from trip_ema.etl.runner import _BatchStats, _TickerInfo, _solo_retry_once
from trip_ema.etl.types import Tier


def _ticker(tid: int, symbol: str) -> _TickerInfo:
    return _TickerInfo(
        ticker_id=tid, symbol=symbol, tier=Tier.TIER_1, listing_date=None,
    )


def _config():
    """A minimal config carrying only the etl knobs _solo_retry_once reads."""
    etl = SimpleNamespace(
        yfinance_request_delay_seconds=0.0,
        yfinance_throttle_backoff_seconds=1.0,
        yfinance_batch_size=25,
    )
    return SimpleNamespace(etl=etl)


class _Recorder:
    """Stands in for _process_one_batch. Records each call and returns a
    programmed _BatchStats. `outcomes` maps ticker_id -> succeeded?(bool);
    a missing id defaults to failure (0 succeeded), so a ticker "recovers"
    only if we say so, on whichever pass it is seen."""

    def __init__(self, recover_ids: set[int]):
        self.recover_ids = set(recover_ids)
        self.calls: list[dict] = []

    def __call__(self, *, db_engine, config, src, batch, start, end,
                 timeframe, intraday) -> _BatchStats:
        # src.repair is the flag the pass built the YFinanceSource with.
        repair = getattr(src, "repair", None)
        self.calls.append({
            "repair": repair,
            "batch_ids": [t.ticker_id for t in batch],
            "batch_size_arg": getattr(src, "batch_size", None),
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


class _FakeSource:
    """Captures the kwargs YFinanceSource was built with, so the recorder
    can read `.repair` and `.batch_size` off it."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@pytest.fixture
def wired(monkeypatch):
    """Wire the runner module: fake YFinanceSource + capture ThrottleConfig."""
    captured = {"throttle_configs": []}

    real_throttle_config = R.ThrottleConfig

    def _capturing_config(*args, **kwargs):
        captured["throttle_configs"].append(kwargs)
        return real_throttle_config(*args, **kwargs)

    monkeypatch.setattr(R, "YFinanceSource", _FakeSource)
    monkeypatch.setattr(R, "ThrottleConfig", _capturing_config)
    return captured


def _run(recorder, *, failed, intcast_ids, monkeypatch):
    monkeypatch.setattr(R, "_process_one_batch", recorder)
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
    )


class TestLayerRouting:

    def test_intcast_goes_repair_false_first(self, wired, monkeypatch):
        """An intcast ticker must be fetched with repair=False, and never
        with repair=True — it is a repair casualty by construction."""
        t = _ticker(1, "CONCOR")
        rec = _Recorder(recover_ids={1})
        _run(rec, failed=[t], intcast_ids={1}, monkeypatch=monkeypatch)

        repairs_for_1 = [
            c["repair"] for c in rec.calls if 1 in c["batch_ids"]
        ]
        assert repairs_for_1, "intcast ticker was never fetched"
        assert all(r is False for r in repairs_for_1)
        assert True not in repairs_for_1

    def test_generic_goes_repair_true_then_false(self, wired, monkeypatch):
        """A generic empty that never recovers is seen with repair=True
        (Layer 2) and then repair=False (Layer 3)."""
        t = _ticker(2, "FLAKE")
        rec = _Recorder(recover_ids=set())  # never recovers
        _run(rec, failed=[t], intcast_ids=set(), monkeypatch=monkeypatch)

        repairs_for_2 = [
            c["repair"] for c in rec.calls if 2 in c["batch_ids"]
        ]
        assert True in repairs_for_2, "generic never got repair=True Layer 2"
        assert False in repairs_for_2, "generic never got repair=False Layer 3"
        # repair=True must come before repair=False.
        assert repairs_for_2.index(True) < repairs_for_2.index(False)

    def test_generic_recovered_in_layer2_skips_layer3(self, wired, monkeypatch):
        """If repair=True recovers a generic ticker, it is NOT re-fetched
        with repair=False — Layer 3 only sees Layer-2 survivors."""
        t = _ticker(3, "RECOVERS")
        rec = _Recorder(recover_ids={3})  # recovers on first sight
        _run(rec, failed=[t], intcast_ids=set(), monkeypatch=monkeypatch)

        repairs_for_3 = [
            c["repair"] for c in rec.calls if 3 in c["batch_ids"]
        ]
        assert repairs_for_3 == [True], (
            f"expected exactly one repair=True call, got {repairs_for_3}"
        )

    def test_intcast_and_generic_are_partitioned(self, wired, monkeypatch):
        """Mixed cohort: intcast id only ever repair=False; generic id gets
        the repair=True/repair=False gauntlet. No cross-contamination."""
        t_ic = _ticker(10, "INTCAST")
        t_gen = _ticker(20, "GENERIC")
        rec = _Recorder(recover_ids=set())  # nobody recovers
        _run(
            rec, failed=[t_ic, t_gen], intcast_ids={10},
            monkeypatch=monkeypatch,
        )

        ic_repairs = [c["repair"] for c in rec.calls if 10 in c["batch_ids"]]
        gen_repairs = [c["repair"] for c in rec.calls if 20 in c["batch_ids"]]

        assert ic_repairs and all(r is False for r in ic_repairs)
        assert True in gen_repairs and False in gen_repairs


class TestGateConfig:

    def test_shared_gate_built_with_trip_on_empty_false(
        self, wired, monkeypatch,
    ):
        """D2 contract: the recovery gate disables shape-based empty
        tripping. Only an explicit 429 may open a backoff window in the
        retry cohort, because the cohort is full of expected empties."""
        t = _ticker(1, "X")
        rec = _Recorder(recover_ids=set())
        _run(rec, failed=[t], intcast_ids=set(), monkeypatch=monkeypatch)

        configs = wired["throttle_configs"]
        assert configs, "ThrottleConfig was never constructed"
        # Exactly one gate for the whole recovery.
        assert len(configs) == 1
        assert configs[0].get("trip_on_empty") is False


class TestBounds:

    def test_empty_failed_list_is_a_noop(self, wired, monkeypatch):
        rec = _Recorder(recover_ids=set())
        _run(rec, failed=[], intcast_ids=set(), monkeypatch=monkeypatch)
        assert rec.calls == []

    def test_no_intcast_ids_still_runs_generic_layers(
        self, wired, monkeypatch,
    ):
        """intcast_ids=None (the default) must behave like an empty set:
        everything is generic, no intcast layer fires."""
        t = _ticker(7, "PLAIN")
        rec = _Recorder(recover_ids=set())
        monkeypatch.setattr(R, "_process_one_batch", rec)
        _solo_retry_once(
            db_engine=object(), config=_config(), exchange="NSE",
            failed_tickers=[t], start=date(2026, 1, 1), end=date(2026, 1, 31),
            timeframe=Timeframe.DAILY, intraday=False,
            # intcast_ids omitted entirely — exercises the None default.
        )
        repairs = [c["repair"] for c in rec.calls if 7 in c["batch_ids"]]
        assert True in repairs and False in repairs
