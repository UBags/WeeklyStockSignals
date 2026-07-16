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
        # Drop 2: inject a no-op parked lookup so the filter never touches the
        # sentinel db_engine. Tests that exercise the filter itself pass their
        # own parked_lookup (see TestParkedExclusion).
        parked_lookup=lambda _e, ids: set(),
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
            parked_lookup=lambda _e, ids: set(),
        )
        repairs = [c["repair"] for c in rec.calls if 7 in c["batch_ids"]]
        assert True in repairs and False in repairs


# ---- D6: recovery metrics ----


class TestRecoveryMetrics:

    def _val(self, outcome, layer):
        from trip_ema.observability import metrics as M
        try:
            return M.etl_fetch_recovery_total.labels(
                outcome=outcome, layer=layer,
            )._value.get()
        except Exception:
            return 0.0

    def test_recovered_counter_increments(self):
        """A generic ticker that recovers under repair=True bumps the
        'recovered' counter on the L2 layer."""
        before = self._val("recovered", "L2")
        rec = _Recorder(recover_ids={100})
        _run(rec, failed=[_ticker(100, "WINS")], intcast_ids=set())
        after = self._val("recovered", "L2")
        assert after >= before + 1

    def test_still_empty_counter_increments(self):
        """A ticker that survives every layer empty bumps the 'still_empty'
        counter on the L3 layer (the last pass it fails)."""
        before = self._val("still_empty", "L3")
        rec = _Recorder(recover_ids=set())  # never recovers
        _run(rec, failed=[_ticker(200, "DARK")], intcast_ids=set())
        after = self._val("still_empty", "L3")
        assert after >= before + 1


# ---- D3: parked tickers excluded from the retry cohort ----


class TestParkedExclusion:

    def test_parked_ticker_is_dropped_before_layers(self):
        """A parked ticker must never reach _process_batch — the injected
        parked_lookup removes it from the cohort up front."""
        parked = _ticker(999, "DELISTED")
        live = _ticker(1, "ALIVE")
        rec = _Recorder(recover_ids={1})

        _solo_retry_once(
            db_engine=object(),
            config=_config(),
            exchange="NSE",
            failed_tickers=[parked, live],
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            timeframe=Timeframe.DAILY,
            intraday=False,
            process_batch_fn=rec,
            source_factory=_FakeSource,
            parked_lookup=lambda _e, ids: {999},   # 999 is parked
        )
        seen = {tid for c in rec.calls for tid in c["batch_ids"]}
        assert 999 not in seen, "parked ticker was retried"
        assert 1 in seen, "live ticker was not retried"

    def test_all_parked_is_a_clean_noop(self):
        """If every cohort ticker is parked, the retry is a no-op — no
        _process_batch calls at all."""
        rec = _Recorder(recover_ids=set())
        _solo_retry_once(
            db_engine=object(),
            config=_config(),
            exchange="NSE",
            failed_tickers=[_ticker(7, "A"), _ticker(8, "B")],
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            timeframe=Timeframe.DAILY,
            intraday=False,
            process_batch_fn=rec,
            source_factory=_FakeSource,
            parked_lookup=lambda _e, ids: {7, 8},
        )
        assert rec.calls == []


# ---- D3-back: park tickers that survive all layers empty ----


class TestParkSurvivors:

    def _run_with_park(self, *, failed, recover_ids, park_calls):
        rec = _Recorder(recover_ids=recover_ids)

        def _park(db_engine, config, ticker, reason):
            park_calls.append((ticker.ticker_id, reason))

        _solo_retry_once(
            db_engine=object(),
            config=_config(),
            exchange="NSE",
            failed_tickers=failed,
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            timeframe=Timeframe.DAILY,
            intraday=False,
            process_batch_fn=rec,
            source_factory=_FakeSource,
            parked_lookup=lambda _e, ids: set(),   # nothing pre-parked
            park_fn=_park,
        )
        return rec

    def test_all_layer_survivor_is_parked(self):
        """A ticker that never recovers (empty through L2 and L3) must be
        handed to park_fn exactly once, with the dataless reason."""
        park_calls: list = []
        self._run_with_park(
            failed=[_ticker(50, "DARK")],
            recover_ids=set(),          # never recovers
            park_calls=park_calls,
        )
        assert park_calls == [(50, "no data after all retry layers")]

    def test_recovered_ticker_is_not_parked(self):
        """A ticker that recovers under repair=True must NOT be parked."""
        park_calls: list = []
        self._run_with_park(
            failed=[_ticker(60, "WINS")],
            recover_ids={60},           # recovers in L2
            park_calls=park_calls,
        )
        assert park_calls == []

    def test_mixed_only_survivor_parked(self):
        """Two tickers, one recovers and one doesn't — only the survivor is
        parked."""
        park_calls: list = []
        self._run_with_park(
            failed=[_ticker(70, "WINS"), _ticker(80, "DARK")],
            recover_ids={70},
            park_calls=park_calls,
        )
        assert park_calls == [(80, "no data after all retry layers")]
