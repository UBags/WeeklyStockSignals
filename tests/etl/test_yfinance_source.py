"""Tests for the yfinance source.

The source takes a Downloader (trip_ema/etl/ports.py) — the raw call into
yfinance — plus an injected sleeper and throttle gate. These tests hand it a
FakeDownloader instead of patching `yfinance.download` globally, so they assert
on what the source ASKED for rather than on a mock's call records, and they run
without yfinance installed.

What is under test here is our normalisation, batching, retry and throttle
behaviour, not yfinance's."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from trip_ema.etl.ports import BarSource
from trip_ema.etl.throttle import ThrottleConfig, ThrottleGate
from trip_ema.etl.yfinance_source import (
    YFinanceSource,
    from_yfinance_symbol,
    to_yfinance_symbol,
)

from .fakes import FakeDownloader, RecordingSleeper, SleepingClock


class TestSymbolMapping:

    def test_to_yfinance_appends_ns(self):
        assert to_yfinance_symbol("RELIANCE") == "RELIANCE.NS"
        assert to_yfinance_symbol("tcs") == "TCS.NS"

    def test_from_yfinance_strips_suffix(self):
        assert from_yfinance_symbol("RELIANCE.NS") == "RELIANCE"
        assert from_yfinance_symbol("TCS") == "TCS"

    def test_round_trip(self):
        for s in ["RELIANCE", "TCS", "ICICIBANK", "AXISBANK"]:
            assert from_yfinance_symbol(to_yfinance_symbol(s)) == s


def _build_multi_df(symbol_data: dict):
    """Build a pandas multi-index DataFrame that mimics what yfinance
    returns for yf.download(['A.NS', 'B.NS'], ...). Each symbol's data
    is a dict of {col: list_of_values}; index is shared.
    """
    import pandas as pd
    cols = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    # Common date index
    first_data = next(iter(symbol_data.values()))
    n = len(first_data["Open"])
    idx = pd.DatetimeIndex([
        pd.Timestamp(2026, 1, 1) + pd.Timedelta(days=i) for i in range(n)
    ])
    # Build columns as multi-index (symbol, field)
    arrays = []
    for sym in symbol_data:
        for c in cols:
            arrays.append((sym, c))
    multi_cols = pd.MultiIndex.from_tuples(arrays)
    data = {}
    for sym, fields in symbol_data.items():
        for c in cols:
            data[(sym, c)] = fields.get(c, [0.0] * n)
    df = pd.DataFrame(data, index=idx, columns=multi_cols)
    return df


def _build_flat_df(fields: dict):
    """Build a flat DataFrame as yfinance returns for a single-symbol
    yf.download(). No multi-index."""
    import pandas as pd
    cols = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    n = len(fields["Open"])
    idx = pd.DatetimeIndex([
        pd.Timestamp(2026, 1, 1) + pd.Timedelta(days=i) for i in range(n)
    ])
    return pd.DataFrame({c: fields.get(c, [0.0] * n) for c in cols}, index=idx)


class TestNormalization:

    def test_two_symbols_both_succeed(self):
        dl = FakeDownloader(_build_multi_df({
            "RELIANCE.NS": {
                "Open": [100.0, 101.0],
                "High": [102.0, 103.0],
                "Low": [99.0, 100.0],
                "Close": [101.5, 102.5],
                "Adj Close": [101.5, 102.5],
                "Volume": [1000.0, 1100.0],
            },
            "TCS.NS": {
                "Open": [3000.0, 3010.0],
                "High": [3020.0, 3030.0],
                "Low": [2990.0, 3000.0],
                "Close": [3015.0, 3025.0],
                "Adj Close": [3015.0, 3025.0],
                "Volume": [500.0, 550.0],
            },
        }))
        src = YFinanceSource(download=dl)
        results = src.fetch_daily_history(
            ["RELIANCE", "TCS"], start=date(2026, 1, 1),
        )
        assert "RELIANCE" in results and "TCS" in results
        # Both succeed
        assert results["RELIANCE"].succeeded
        assert results["TCS"].succeeded
        # 2 bars each
        assert len(results["RELIANCE"].bars) == 2
        assert results["RELIANCE"].bars[0].close == 101.5
        assert results["TCS"].bars[1].close == 3025.0

    def test_missing_symbol_returns_error_outcome(self):
        """yfinance returns a DataFrame missing one of the requested
        symbols — we report an error for it but succeed on the other."""
        dl = FakeDownloader(_build_multi_df({
            "RELIANCE.NS": {
                "Open": [100.0], "High": [101.0], "Low": [99.0],
                "Close": [100.5], "Adj Close": [100.5], "Volume": [1000.0],
            },
            # TCS.NS not in the response
        }))
        src = YFinanceSource(download=dl)
        results = src.fetch_daily_history(
            ["RELIANCE", "TCS"], start=date(2026, 1, 1),
        )
        assert results["RELIANCE"].succeeded
        assert not results["TCS"].succeeded
        assert "not in returned columns" in results["TCS"].error_message

    def test_nan_rows_are_skipped_not_written(self):
        # A symbol whose first row is all NaN (missing day) — should
        # be skipped in the bars output.
        dl = FakeDownloader(_build_flat_df({
            "Open": [float("nan"), 101.0],
            "High": [float("nan"), 103.0],
            "Low": [float("nan"), 100.0],
            "Close": [float("nan"), 102.5],
            "Adj Close": [float("nan"), 102.5],
            "Volume": [float("nan"), 1100.0],
        }))
        src = YFinanceSource(download=dl)
        results = src.fetch_daily_history(["RELIANCE"], start=date(2026, 1, 1))
        # Only the non-NaN bar
        assert len(results["RELIANCE"].bars) == 1
        assert results["RELIANCE"].bars[0].close == 102.5

    def test_empty_dataframe_all_symbols_fail(self):
        import pandas as pd
        src = YFinanceSource(download=FakeDownloader(pd.DataFrame()))
        results = src.fetch_daily_history(
            ["RELIANCE", "TCS"], start=date(2026, 1, 1),
        )
        for s in ["RELIANCE", "TCS"]:
            assert not results[s].succeeded
            assert "empty" in results[s].error_message.lower()


class TestRetryAndFailure:

    def test_all_retries_failing_yields_error_outcomes(self):
        dl = FakeDownloader(RuntimeError("simulated yfinance error"))
        sleeper = RecordingSleeper()
        src = YFinanceSource(
            max_retries=2, retry_backoff_base=0.0,
            download=dl, sleep=sleeper,
        )
        results = src.fetch_daily_history(["RELIANCE"], start=date(2026, 1, 1))
        assert not results["RELIANCE"].succeeded
        assert "simulated yfinance error" in results["RELIANCE"].error_message
        # Verify it retried twice
        assert dl.call_count == 2

    def test_retry_succeeds_after_initial_failure(self):
        good_df = _build_flat_df({
            "Open": [100.0], "High": [101.0], "Low": [99.0],
            "Close": [100.5], "Adj Close": [100.5], "Volume": [1000.0],
        })
        dl = FakeDownloader(RuntimeError("first try fails"), good_df)
        src = YFinanceSource(
            max_retries=3, retry_backoff_base=0.0,
            download=dl, sleep=RecordingSleeper(),
        )
        results = src.fetch_daily_history(["RELIANCE"], start=date(2026, 1, 1))
        assert results["RELIANCE"].succeeded
        assert len(results["RELIANCE"].bars) == 1
        assert dl.call_count == 2


class TestBatching:

    def test_large_list_is_split_into_batches(self):
        # 75 symbols, batch_size=50 → 2 batches
        def fake_download(tickers=None, **kwargs):
            return _build_multi_df({
                t: {
                    "Open": [100.0], "High": [101.0], "Low": [99.0],
                    "Close": [100.5], "Adj Close": [100.5], "Volume": [1000.0],
                }
                for t in tickers
            })
        dl = FakeDownloader(fake_download)
        src = YFinanceSource(batch_size=50, download=dl)
        symbols = [f"SYM{i}" for i in range(75)]
        results = src.fetch_daily_history(symbols, start=date(2026, 1, 1))
        assert len(results) == 75
        # 2 batches expected
        assert dl.call_count == 2
        # First batch had 50 tickers, second had 25
        assert len(dl.calls[0]["tickers"]) == 50
        assert len(dl.calls[1]["tickers"]) == 25


def _empty_multi_df(symbols):
    """The shape a THROTTLED response takes: every requested column present,
    every value NaN. Yahoo does not raise; it hands back nothing and calls it a
    success."""
    return _build_multi_df({
        s: {c: [float("nan")] for c in
            ("Open", "High", "Low", "Close", "Adj Close", "Volume")}
        for s in symbols
    })


class TestThrottleDetection:
    """The batched path could always see a throttle. The solo path — the one the
    recovery layers actually use — could not, because a lone empty frame is
    ambiguous. The gate is shared, so it can."""

    def test_unanimous_empty_batch_backs_off_and_retries(self):
        good = _build_multi_df({
            "A.NS": {"Open": [1.0], "High": [1.0], "Low": [1.0],
                     "Close": [1.0], "Adj Close": [1.0], "Volume": [1.0]},
            "B.NS": {"Open": [2.0], "High": [2.0], "Low": [2.0],
                     "Close": [2.0], "Adj Close": [2.0], "Volume": [2.0]},
        })
        dl = FakeDownloader(_empty_multi_df(["A.NS", "B.NS"]), good)
        sc = SleepingClock()
        gate = ThrottleGate(
            ThrottleConfig(base_seconds=30.0),
            sleep=sc.sleep, clock=sc.clock, jitter=lambda: 0.5,
        )
        src = YFinanceSource(download=dl, throttle=gate, sleep=sc.sleep)

        results = src.fetch_daily_history(["A", "B"], start=date(2026, 1, 1))

        assert dl.call_count == 2          # it retried
        assert sc.slept == [30.0]          # after backing off
        assert results["A"].bars           # and got the data on the retry

    def test_solo_empty_fetches_corroborate_across_a_shared_gate(self):
        """Three separate single-ticker fetches, all empty, on ONE gate. No
        individual fetch can call that a throttle. Together they can — which is
        exactly the shape of the solo-retry recovery pass."""
        empty = _empty_multi_df(["X.NS"])
        dl = FakeDownloader(empty)         # always empty
        sc = SleepingClock()
        gate = ThrottleGate(
            ThrottleConfig(base_seconds=30.0, solo_empty_streak_to_trip=3),
            sleep=sc.sleep, clock=sc.clock, jitter=lambda: 0.5,
        )
        src = YFinanceSource(
            batch_size=1, download=dl, throttle=gate, sleep=sc.sleep,
        )

        for _ in range(3):
            src.fetch_daily_history(["X"], start=date(2026, 1, 1))

        # Two ambiguous empties, then the third trips it and the source retries
        # behind a real backoff instead of walking on at full speed.
        assert sc.slept, "third consecutive empty solo fetch must back off"
        assert gate.snapshot()["backoff_level"] >= 1

    def test_a_single_empty_solo_fetch_does_not_back_off(self):
        """A delisted ticker must not cost the pass a 30-second penalty."""
        dl = FakeDownloader(_empty_multi_df(["X.NS"]))
        sc = SleepingClock()
        gate = ThrottleGate(
            ThrottleConfig(base_seconds=30.0, solo_empty_streak_to_trip=3),
            sleep=sc.sleep, clock=sc.clock, jitter=lambda: 0.5,
        )
        src = YFinanceSource(
            batch_size=1, download=dl, throttle=gate, sleep=sc.sleep,
        )
        src.fetch_daily_history(["X"], start=date(2026, 1, 1))
        assert sc.slept == []
        assert dl.call_count == 1

    def test_429_takes_the_long_backoff_not_the_flake_backoff(self):
        """retry_backoff_base would give this 2s. A rate-limit needs 30."""
        good = _build_flat_df({
            "Open": [1.0], "High": [1.0], "Low": [1.0],
            "Close": [1.0], "Adj Close": [1.0], "Volume": [1.0],
        })
        dl = FakeDownloader(Exception("HTTP Error 429: Too Many Requests"), good)
        sc = SleepingClock()
        gate = ThrottleGate(
            ThrottleConfig(base_seconds=30.0),
            sleep=sc.sleep, clock=sc.clock, jitter=lambda: 0.5,
        )
        src = YFinanceSource(
            retry_backoff_base=2.0, download=dl, throttle=gate, sleep=sc.sleep,
        )
        results = src.fetch_daily_history(["RELIANCE"], start=date(2026, 1, 1))
        assert sc.slept == [30.0]
        assert results["RELIANCE"].succeeded

    def test_ordinary_error_still_takes_the_short_backoff(self):
        good = _build_flat_df({
            "Open": [1.0], "High": [1.0], "Low": [1.0],
            "Close": [1.0], "Adj Close": [1.0], "Volume": [1.0],
        })
        dl = FakeDownloader(RuntimeError("connection reset"), good)
        sleeper = RecordingSleeper()
        src = YFinanceSource(
            retry_backoff_base=2.0, download=dl, sleep=sleeper,
        )
        src.fetch_daily_history(["RELIANCE"], start=date(2026, 1, 1))
        assert sleeper.slept == [2.0]      # 2 ** 1, the flake backoff


class TestPortConformance:

    def test_yfinance_source_is_a_bar_source(self):
        assert isinstance(YFinanceSource(), BarSource)


# ---- B1: failure_kind classification ----


class TestFailureKindDefault:

    def test_fetch_outcome_default_failure_kind_is_ok(self):
        """B1 is pure extension: existing FetchOutcome construction sites
        (in tests + prod code) get failure_kind='ok' by default, so nothing
        old breaks."""
        from trip_ema.etl.types import FetchOutcome
        o = FetchOutcome(yfinance_symbol="FOO.NS")
        assert o.failure_kind == "ok"

    def test_fetch_outcome_failure_kind_settable(self):
        from trip_ema.etl.types import FetchOutcome
        o = FetchOutcome(yfinance_symbol="FOO.NS", failure_kind="intcast")
        assert o.failure_kind == "intcast"


class TestClassifyEmpty:

    def test_intcast_message_classifies_as_intcast(self):
        from trip_ema.etl.yfinance_source import _classify_empty
        assert _classify_empty(
            "CONCOR.NS",
            {"CONCOR.NS": "IntCastingNaNError('cannot convert')"},
        ) == "intcast"

    def test_delisted_message_classifies_as_delisted(self):
        from trip_ema.etl.yfinance_source import _classify_empty
        assert _classify_empty(
            "OLDCO.NS",
            {"OLDCO.NS": "possibly delisted; no price data found"},
        ) == "delisted"

    def test_unknown_or_missing_classifies_as_empty(self):
        """B1: no error surface for this symbol -> we cannot tell it apart
        from a legitimately-dataless ticker; label 'empty' and let the
        retry layer cover it under repair=True as before. The intcast fast
        path is opt-in on positive IntCastingNaN evidence, never a guess."""
        from trip_ema.etl.yfinance_source import _classify_empty
        assert _classify_empty("MYSTERY.NS", {}) == "empty"
        assert _classify_empty("MYSTERY.NS", {"OTHER.NS": "x"}) == "empty"


class TestSnapshotAndClearYfErrors:

    def test_captures_matching_symbols_and_clears_them(self):
        """B1: entries for the batch's symbols are captured, then removed
        from the shared dict so a concurrent batch does not see them.
        Entries belonging to OTHER batches must be left alone."""
        import yfinance.shared as yfs
        from trip_ema.etl.ports import snapshot_and_clear_yf_errors

        yfs._ERRORS.clear()
        yfs._ERRORS["FOO.NS"] = "IntCastingNaNError"
        yfs._ERRORS["BAR.NS"] = "possibly delisted"
        yfs._ERRORS["BAZ.NS"] = "should_stay"

        try:
            captured = snapshot_and_clear_yf_errors(["FOO.NS", "BAR.NS"])
            assert captured == {
                "FOO.NS": "IntCastingNaNError",
                "BAR.NS": "possibly delisted",
            }
            assert "FOO.NS" not in yfs._ERRORS
            assert "BAR.NS" not in yfs._ERRORS
            assert yfs._ERRORS["BAZ.NS"] == "should_stay"
        finally:
            yfs._ERRORS.clear()

    def test_missing_symbol_is_silently_skipped(self):
        import yfinance.shared as yfs
        from trip_ema.etl.ports import snapshot_and_clear_yf_errors
        yfs._ERRORS.clear()
        try:
            assert snapshot_and_clear_yf_errors(["ABSENT.NS"]) == {}
        finally:
            yfs._ERRORS.clear()


class TestNormalizePropagatesFailureKind:

    def test_all_nan_symbol_with_intcast_error_gets_intcast_kind(self):
        """B1 integration: build a multi-symbol frame where one column set
        is all-NaN, and register that symbol's error as IntCastingNaN in
        yfinance.shared._ERRORS. The resulting FetchOutcome carries
        failure_kind='intcast' — proving the whole chain
        (_snapshot -> normalize -> _classify_empty) is wired."""
        import numpy as np
        import yfinance.shared as yfs

        yfs._ERRORS.clear()
        yfs._ERRORS["BAD.NS"] = "IntCastingNaNError(\'cannot convert NaN\')"

        try:
            df = _build_multi_df({
                "GOOD.NS": {
                    "Open": [1.0, 2.0], "High": [1.5, 2.5],
                    "Low": [0.5, 1.5], "Close": [1.2, 2.2],
                    "Adj Close": [1.2, 2.2], "Volume": [100.0, 200.0],
                },
                "BAD.NS": {
                    "Open": [np.nan, np.nan], "High": [np.nan, np.nan],
                    "Low": [np.nan, np.nan], "Close": [np.nan, np.nan],
                    "Adj Close": [np.nan, np.nan], "Volume": [np.nan, np.nan],
                },
            })

            src = YFinanceSource(
                exchange="NSE",
                download=FakeDownloader(df),
                sleep=lambda _s: None,
            )
            result = src.fetch_daily_history(
                ["GOOD", "BAD"],
                start=date(2026, 1, 1), end=date(2026, 1, 2),
            )
            assert result["GOOD"].failure_kind == "ok"
            assert result["GOOD"].bars
            assert result["BAD"].failure_kind == "intcast"
            assert not result["BAD"].bars
        finally:
            yfs._ERRORS.clear()

    def test_all_nan_symbol_without_error_gets_empty_kind(self):
        """B1: same shape but no IntCastingNaN record. Without positive
        evidence we tag 'empty', not 'intcast' — the retry falls back to
        the repair=True path first, as it always has."""
        import numpy as np
        import yfinance.shared as yfs

        yfs._ERRORS.clear()

        try:
            df = _build_multi_df({
                "GOOD.NS": {
                    "Open": [1.0], "High": [1.5], "Low": [0.5],
                    "Close": [1.2], "Adj Close": [1.2], "Volume": [100.0],
                },
                "BAD.NS": {
                    "Open": [np.nan], "High": [np.nan], "Low": [np.nan],
                    "Close": [np.nan], "Adj Close": [np.nan],
                    "Volume": [np.nan],
                },
            })
            src = YFinanceSource(
                exchange="NSE",
                download=FakeDownloader(df),
                sleep=lambda _s: None,
            )
            result = src.fetch_daily_history(
                ["GOOD", "BAD"],
                start=date(2026, 1, 1), end=date(2026, 1, 2),
            )
            assert result["BAD"].failure_kind == "empty"
        finally:
            yfs._ERRORS.clear()
