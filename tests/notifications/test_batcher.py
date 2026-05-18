"""Tests for the notification batcher (render_text, render_json)."""

from __future__ import annotations

from datetime import datetime, timezone

from trip_ema.domain import (
    ChangeSignal,
    NotificationBatch,
    SignalEvent,
    TickerRef,
    Timeframe,
)
from trip_ema.notifications.batcher import (
    render_event_line,
    render_json,
    render_text,
)


NIFTY = TickerRef("NSE", "NIFTY", "INDEX")


def _event(
    signal: ChangeSignal = ChangeSignal.BUY,
    price: float = 24856.20,
) -> SignalEvent:
    t = datetime(2026, 5, 18, 4, 30, tzinfo=timezone.utc)
    return SignalEvent(
        ticker=NIFTY,
        timeframe=Timeframe.WEEKLY,
        bar_timestamp=t,
        signal=signal,
        price_at_signal=price,
        computed_at=t,
    )


def _batch(events: list[SignalEvent]) -> NotificationBatch:
    return NotificationBatch(
        subscriber_id=1, events=events,
        composed_at=datetime(2026, 5, 18, 5, 0, tzinfo=timezone.utc),
    )


# ---------- render_event_line ---------- #

def test_render_event_line_uses_buy_verb_for_buy_signal():
    line = render_event_line(_event(ChangeSignal.BUY))
    assert line.startswith("BUY signal on NSE:NIFTY")
    assert "24856.20" in line


def test_render_event_line_uses_sell_verb_for_sell_signal():
    line = render_event_line(_event(ChangeSignal.SELL))
    assert line.startswith("SELL signal on NSE:NIFTY")


def test_render_event_line_uses_exit_for_both_exit_variants():
    """Both EXIT_FROM_BUY and EXIT_FROM_SELL render as 'EXIT signal'.
    The receiving human cares about the direction of the move, which is
    implicit in 'EXIT' coming after a known BUY/SELL alert."""
    line_buy_exit = render_event_line(_event(ChangeSignal.EXIT_FROM_BUY))
    line_sell_exit = render_event_line(_event(ChangeSignal.EXIT_FROM_SELL))
    assert line_buy_exit.startswith("EXIT signal on NSE:NIFTY")
    assert line_sell_exit.startswith("EXIT signal on NSE:NIFTY")


def test_render_event_line_formats_price_to_two_decimals():
    line = render_event_line(_event(price=100.123456))
    assert "100.12" in line
    assert "100.123456" not in line


# ---------- render_text ---------- #

def test_render_text_empty_batch_returns_empty_string():
    assert render_text(_batch([])) == ""


def test_render_text_single_event():
    body = render_text(_batch([_event()]))
    assert "Signal updates for your subscriptions:" in body
    assert "BUY signal on NSE:NIFTY" in body


def test_render_text_multiple_events_each_on_own_line():
    body = render_text(_batch([
        _event(ChangeSignal.BUY),
        _event(ChangeSignal.SELL),
        _event(ChangeSignal.EXIT_FROM_BUY),
    ]))
    # 3 indented event lines + 1 header line
    assert body.count("\n") == 3
    assert "BUY signal" in body
    assert "SELL signal" in body
    assert "EXIT signal" in body


# ---------- render_json ---------- #

def test_render_json_empty_batch_returns_empty_list():
    assert render_json(_batch([])) == []


def test_render_json_event_shape():
    [item] = render_json(_batch([_event(ChangeSignal.BUY, price=24856.20)]))
    assert item["ticker"] == {
        "exchange": "NSE", "symbol": "NIFTY", "instrument": "INDEX",
    }
    assert item["timeframe"] == "1W"
    assert item["signal"] == "BUY"
    assert item["price_at_signal"] == 24856.20
    # ISO 8601 timestamps
    assert "T" in item["bar_timestamp"]
    assert "T" in item["computed_at"]


def test_render_json_is_serializable():
    """Output should JSON-serialize without custom encoders."""
    import json
    body = render_json(_batch([_event(), _event(ChangeSignal.SELL)]))
    s = json.dumps(body)
    assert "BUY" in s and "SELL" in s
