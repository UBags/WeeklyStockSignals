"""Compose a NotificationBatch into transport-ready payloads.

Two renderings:
  - text:  human-readable, one event per line. Used by email/SMS/Slack.
  - json:  array of structured events. Used by webhook POST bodies.

Kept here (rather than inside each transport) so the format is consistent
across transports and so multiple transports can share the work for the
same batch.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import timezone

from ..domain import ChangeSignal, NotificationBatch, SignalEvent


_SIGNAL_VERB = {
    ChangeSignal.BUY: "BUY",
    ChangeSignal.SELL: "SELL",
    ChangeSignal.EXIT_FROM_BUY: "EXIT",
    ChangeSignal.EXIT_FROM_SELL: "EXIT",
}


def render_event_line(event: SignalEvent, *, tz=None) -> str:
    """One human-readable line for one event.

    Format: 'BUY signal on EXCHANGE:SYMBOL at PRICE at TIMESTAMP'.
    Matches the user's spec phrasing.
    """
    verb = _SIGNAL_VERB.get(event.signal, "CHANGE")
    when = event.bar_timestamp.astimezone(tz) if tz else event.bar_timestamp
    when_str = when.strftime("%Y-%m-%d %H:%M %Z").strip()
    return (
        f"{verb} signal on {event.ticker.exchange}:{event.ticker.symbol} "
        f"at {event.price_at_signal:.2f} at {when_str}"
    )


def render_text(batch: NotificationBatch, *, tz=None) -> str:
    """Multi-line text body for email / SMS / Slack."""
    if not batch.events:
        return ""
    header = "Signal updates for your subscriptions:"
    lines = [render_event_line(e, tz=tz) for e in batch.events]
    return header + "\n" + "\n".join(f"  {line}" for line in lines)


def render_json(batch: NotificationBatch) -> list[dict]:
    """JSON-serializable array of events. Used as the webhook POST body."""
    out = []
    for e in batch.events:
        out.append({
            "ticker": {
                "exchange": e.ticker.exchange,
                "symbol": e.ticker.symbol,
                "instrument": e.ticker.instrument,
            },
            "timeframe": e.timeframe.value,
            "signal": e.signal.name,
            "bar_timestamp": e.bar_timestamp.isoformat(),
            "price_at_signal": e.price_at_signal,
            "computed_at": e.computed_at.isoformat(),
        })
    return out
