"""Tests for the in-memory merge protocol in signal_generator.

Pure-function tests — no DB, no engine. Build an `in_memory` dict
in the shape `_load_existing_or_empty` produces, call
`_merge_signals_into`, and assert on the resulting dict.

The three transitions from design §4.2.3 each get a dedicated test;
plus stability properties (dates outside window untouched, empty
initial file, sort order, deletion-count return value).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from trip_ema.domain import ChangeSignal, SignalEvent, TickerRef, Timeframe
from trip_ema.etl.signal_generator import _merge_signals_into


# ---------- Fixtures ---------- #


def _utc(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=timezone.utc)


def _ev(
    symbol: str,
    bar_dt: datetime,
    signal: ChangeSignal,
    price: float,
    timeframe: Timeframe = Timeframe.WEEKLY,
) -> SignalEvent:
    """Build a SignalEvent with sensible defaults."""
    return SignalEvent(
        ticker=TickerRef(exchange="NSE", symbol=symbol),
        timeframe=timeframe,
        bar_timestamp=bar_dt,
        signal=signal,
        price_at_signal=price,
        computed_at=_utc(2026, 5, 21),
    )


def _empty_in_memory() -> dict:
    return {"by_date": {}}


# ---------- Three transitions from §4.2.3 ---------- #


class TestMergeThreeTransitions:
    """Design §4.2.3 lists three transitions the merge must handle.
    Each gets a dedicated test."""

    def test_new_date_no_prior_signal_then_engine_emits_one(self):
        """Transition 1: date had no prior signal, engine emits one
        now → added."""
        in_memory = _empty_in_memory()
        events = [
            _ev("RELIANCE", _utc(2024, 1, 15), ChangeSignal.BUY, 945.5),
        ]
        deleted = _merge_signals_into(
            in_memory,
            ticker_symbol="RELIANCE",
            timeframe=Timeframe.WEEKLY,
            recomputed_window=(date(2024, 1, 1), date(2024, 1, 31)),
            new_events=events,
        )
        assert deleted == 0
        assert "2024-01-15" in in_memory["by_date"]
        records = in_memory["by_date"]["2024-01-15"]
        assert len(records) == 1
        assert records[0]["ticker"] == "RELIANCE"
        assert records[0]["signal"] == "BUY"
        assert records[0]["price_at_signal"] == 945.5
        assert records[0]["timeframe"] == "1W"

    def test_date_with_prior_signal_engine_emits_different_one(self):
        """Transition 2a: date had a prior signal, engine emits a
        different signal now → replaced."""
        in_memory = {
            "by_date": {
                "2024-01-15": [
                    {
                        "ticker": "RELIANCE",
                        "timeframe": "1W",
                        "signal": "SELL",
                        "price_at_signal": 940.0,
                        "bar_timestamp": "2024-01-15T00:00:00+00:00",
                    }
                ]
            }
        }
        events = [
            _ev("RELIANCE", _utc(2024, 1, 15), ChangeSignal.BUY, 945.5),
        ]
        deleted = _merge_signals_into(
            in_memory,
            ticker_symbol="RELIANCE",
            timeframe=Timeframe.WEEKLY,
            recomputed_window=(date(2024, 1, 1), date(2024, 1, 31)),
            new_events=events,
        )
        assert deleted == 1
        records = in_memory["by_date"]["2024-01-15"]
        assert len(records) == 1
        assert records[0]["signal"] == "BUY"      # new wins
        assert records[0]["price_at_signal"] == 945.5

    def test_date_with_prior_signal_engine_emits_same_one(self):
        """Transition 2b: date had a prior signal, engine emits the
        same kind now (perhaps with refined price). The old entry
        is removed and the new one inserted — there's no value-equal
        short-circuit. This is the simplest invariant to preserve."""
        in_memory = {
            "by_date": {
                "2024-01-15": [
                    {
                        "ticker": "RELIANCE",
                        "timeframe": "1W",
                        "signal": "BUY",
                        "price_at_signal": 800.0,   # stale price
                        "bar_timestamp": "2024-01-15T00:00:00+00:00",
                    }
                ]
            }
        }
        events = [
            _ev("RELIANCE", _utc(2024, 1, 15), ChangeSignal.BUY, 945.5),
        ]
        deleted = _merge_signals_into(
            in_memory,
            ticker_symbol="RELIANCE",
            timeframe=Timeframe.WEEKLY,
            recomputed_window=(date(2024, 1, 1), date(2024, 1, 31)),
            new_events=events,
        )
        assert deleted == 1
        records = in_memory["by_date"]["2024-01-15"]
        assert len(records) == 1
        assert records[0]["price_at_signal"] == 945.5   # new price

    def test_date_with_prior_signal_engine_emits_none_now(self):
        """Transition 3: date had a prior signal, engine emits no
        signal for that date now → removed."""
        in_memory = {
            "by_date": {
                "2024-01-15": [
                    {
                        "ticker": "RELIANCE",
                        "timeframe": "1W",
                        "signal": "BUY",
                        "price_at_signal": 945.5,
                        "bar_timestamp": "2024-01-15T00:00:00+00:00",
                    }
                ]
            }
        }
        deleted = _merge_signals_into(
            in_memory,
            ticker_symbol="RELIANCE",
            timeframe=Timeframe.WEEKLY,
            recomputed_window=(date(2024, 1, 1), date(2024, 1, 31)),
            new_events=[],
        )
        assert deleted == 1
        # Date should be entirely gone from by_date (the entry's
        # array would be empty and the merge tidies empty dates).
        assert "2024-01-15" not in in_memory["by_date"]


# ---------- Window scoping ---------- #


class TestMergeWindowScoping:
    def test_dates_outside_recomputed_window_are_untouched(self):
        """A merge for (start=2024-01-01, end=2024-01-31) MUST NOT
        touch records dated 2023 or 2024-02 or later. Even if those
        records are for the SAME ticker."""
        in_memory = {
            "by_date": {
                "2023-12-15": [
                    {
                        "ticker": "RELIANCE",
                        "timeframe": "1W",
                        "signal": "SELL",
                        "price_at_signal": 900.0,
                        "bar_timestamp": "2023-12-15T00:00:00+00:00",
                    }
                ],
                "2024-02-15": [
                    {
                        "ticker": "RELIANCE",
                        "timeframe": "1W",
                        "signal": "EXIT_FROM_BUY",
                        "price_at_signal": 950.0,
                        "bar_timestamp": "2024-02-15T00:00:00+00:00",
                    }
                ],
            }
        }
        deleted = _merge_signals_into(
            in_memory,
            ticker_symbol="RELIANCE",
            timeframe=Timeframe.WEEKLY,
            recomputed_window=(date(2024, 1, 1), date(2024, 1, 31)),
            new_events=[],
        )
        assert deleted == 0
        # Both untouched.
        assert "2023-12-15" in in_memory["by_date"]
        assert "2024-02-15" in in_memory["by_date"]
        assert in_memory["by_date"]["2023-12-15"][0]["signal"] == "SELL"
        assert in_memory["by_date"]["2024-02-15"][0]["signal"] == "EXIT_FROM_BUY"

    def test_other_tickers_in_same_date_are_untouched(self):
        """A merge for ticker=RELIANCE must not affect TCS's record
        on the same date, even if it falls in the recomputed window."""
        in_memory = {
            "by_date": {
                "2024-01-15": [
                    {
                        "ticker": "RELIANCE",
                        "timeframe": "1W",
                        "signal": "SELL",
                        "price_at_signal": 940.0,
                        "bar_timestamp": "2024-01-15T00:00:00+00:00",
                    },
                    {
                        "ticker": "TCS",
                        "timeframe": "1W",
                        "signal": "BUY",
                        "price_at_signal": 3100.0,
                        "bar_timestamp": "2024-01-15T00:00:00+00:00",
                    },
                ]
            }
        }
        events = [
            _ev("RELIANCE", _utc(2024, 1, 15), ChangeSignal.BUY, 945.5),
        ]
        deleted = _merge_signals_into(
            in_memory,
            ticker_symbol="RELIANCE",
            timeframe=Timeframe.WEEKLY,
            recomputed_window=(date(2024, 1, 1), date(2024, 1, 31)),
            new_events=events,
        )
        assert deleted == 1
        records = in_memory["by_date"]["2024-01-15"]
        symbols = sorted(r["ticker"] for r in records)
        assert symbols == ["RELIANCE", "TCS"]
        # TCS untouched, RELIANCE refreshed
        tcs = next(r for r in records if r["ticker"] == "TCS")
        rel = next(r for r in records if r["ticker"] == "RELIANCE")
        assert tcs["signal"] == "BUY" and tcs["price_at_signal"] == 3100.0
        assert rel["signal"] == "BUY" and rel["price_at_signal"] == 945.5

    def test_other_timeframes_for_same_ticker_are_untouched(self):
        """A merge for (RELIANCE, 1W) must not touch (RELIANCE, 1D)
        on the same date — they're distinct rows in the file's
        keyspace."""
        in_memory = {
            "by_date": {
                "2024-01-15": [
                    {
                        "ticker": "RELIANCE",
                        "timeframe": "1D",
                        "signal": "BUY",
                        "price_at_signal": 945.5,
                        "bar_timestamp": "2024-01-15T00:00:00+00:00",
                    },
                    {
                        "ticker": "RELIANCE",
                        "timeframe": "1W",
                        "signal": "SELL",
                        "price_at_signal": 940.0,
                        "bar_timestamp": "2024-01-15T00:00:00+00:00",
                    },
                ]
            }
        }
        events = [
            _ev("RELIANCE", _utc(2024, 1, 15), ChangeSignal.BUY,
                945.5, timeframe=Timeframe.WEEKLY),
        ]
        deleted = _merge_signals_into(
            in_memory,
            ticker_symbol="RELIANCE",
            timeframe=Timeframe.WEEKLY,
            recomputed_window=(date(2024, 1, 1), date(2024, 1, 31)),
            new_events=events,
        )
        assert deleted == 1
        records = in_memory["by_date"]["2024-01-15"]
        # Both timeframes still present — 1D is unchanged, 1W
        # got replaced.
        daily = [r for r in records if r["timeframe"] == "1D"]
        weekly = [r for r in records if r["timeframe"] == "1W"]
        assert len(daily) == 1 and len(weekly) == 1
        assert daily[0]["signal"] == "BUY"      # untouched
        assert weekly[0]["signal"] == "BUY"     # was SELL, replaced

    def test_window_edges_are_inclusive(self):
        """Bars on exactly start_date or end_date are inside the
        window; bars one day outside are not."""
        in_memory = {
            "by_date": {
                "2024-01-01": [   # start_date — IN
                    {
                        "ticker": "X", "timeframe": "1W", "signal": "BUY",
                        "price_at_signal": 1.0,
                        "bar_timestamp": "2024-01-01T00:00:00+00:00",
                    }
                ],
                "2024-01-31": [   # end_date — IN
                    {
                        "ticker": "X", "timeframe": "1W", "signal": "SELL",
                        "price_at_signal": 1.0,
                        "bar_timestamp": "2024-01-31T00:00:00+00:00",
                    }
                ],
                "2023-12-31": [   # just before — OUT
                    {
                        "ticker": "X", "timeframe": "1W", "signal": "BUY",
                        "price_at_signal": 1.0,
                        "bar_timestamp": "2023-12-31T00:00:00+00:00",
                    }
                ],
                "2024-02-01": [   # just after — OUT
                    {
                        "ticker": "X", "timeframe": "1W", "signal": "SELL",
                        "price_at_signal": 1.0,
                        "bar_timestamp": "2024-02-01T00:00:00+00:00",
                    }
                ],
            }
        }
        deleted = _merge_signals_into(
            in_memory,
            ticker_symbol="X",
            timeframe=Timeframe.WEEKLY,
            recomputed_window=(date(2024, 1, 1), date(2024, 1, 31)),
            new_events=[],
        )
        # Both edge dates removed; both outside-window dates kept.
        assert deleted == 2
        assert "2024-01-01" not in in_memory["by_date"]
        assert "2024-01-31" not in in_memory["by_date"]
        assert "2023-12-31" in in_memory["by_date"]
        assert "2024-02-01" in in_memory["by_date"]


# ---------- Misc properties ---------- #


class TestMergeMisc:
    def test_empty_initial_file_with_no_new_events(self):
        """Vacuous merge into empty file — no change, no error."""
        in_memory = _empty_in_memory()
        deleted = _merge_signals_into(
            in_memory,
            ticker_symbol="RELIANCE",
            timeframe=Timeframe.WEEKLY,
            recomputed_window=(date(2024, 1, 1), date(2024, 1, 31)),
            new_events=[],
        )
        assert deleted == 0
        assert in_memory["by_date"] == {}

    def test_sort_order_within_date_is_ticker_then_timeframe(self):
        """Design §4.3: within each date's array, order is
        (ticker, timeframe) for stable diffs across runs."""
        in_memory = _empty_in_memory()
        # Emit in NON-sorted order to verify the merge does the sort.
        events = [
            _ev("TCS",      _utc(2024, 1, 15), ChangeSignal.BUY, 3000.0),
            _ev("RELIANCE", _utc(2024, 1, 15), ChangeSignal.SELL, 945.0,
                timeframe=Timeframe.WEEKLY),
            _ev("RELIANCE", _utc(2024, 1, 15), ChangeSignal.BUY,  945.5,
                timeframe=Timeframe.DAILY),
        ]
        _merge_signals_into(
            in_memory,
            ticker_symbol="TCS",
            timeframe=Timeframe.WEEKLY,
            recomputed_window=(date(2024, 1, 1), date(2024, 1, 31)),
            new_events=[events[0]],
        )
        _merge_signals_into(
            in_memory,
            ticker_symbol="RELIANCE",
            timeframe=Timeframe.WEEKLY,
            recomputed_window=(date(2024, 1, 1), date(2024, 1, 31)),
            new_events=[events[1]],
        )
        _merge_signals_into(
            in_memory,
            ticker_symbol="RELIANCE",
            timeframe=Timeframe.DAILY,
            recomputed_window=(date(2024, 1, 1), date(2024, 1, 31)),
            new_events=[events[2]],
        )
        records = in_memory["by_date"]["2024-01-15"]
        # Expected sort: (ticker, timeframe) → RELIANCE 1D, RELIANCE 1W, TCS 1W
        assert [(r["ticker"], r["timeframe"]) for r in records] == [
            ("RELIANCE", "1D"),
            ("RELIANCE", "1W"),
            ("TCS", "1W"),
        ]

    def test_new_event_outside_window_still_inserted(self):
        """If new_events contains a bar dated outside the
        recomputed_window, the merge still inserts it (no defensive
        guard). The signal generator's bar-filter ensures this
        doesn't happen in practice, but the merge is defensive about
        the input — adds new entries for dates not yet present.

        This documents the contract: the merge trusts its inputs.
        The window only governs which EXISTING entries get touched.
        """
        in_memory = _empty_in_memory()
        events = [
            _ev("RELIANCE", _utc(2025, 6, 1), ChangeSignal.BUY, 1500.0),
        ]
        _merge_signals_into(
            in_memory,
            ticker_symbol="RELIANCE",
            timeframe=Timeframe.WEEKLY,
            recomputed_window=(date(2024, 1, 1), date(2024, 1, 31)),
            new_events=events,
        )
        # The 2025 date is added, even though it falls outside the
        # window. The signal generator never feeds us this in practice.
        assert "2025-06-01" in in_memory["by_date"]
