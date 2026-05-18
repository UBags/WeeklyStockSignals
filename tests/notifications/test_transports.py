"""Tests for EmailTransport and WebhookTransport.

Both transports take an injectable callable (sender / poster) so we can
verify behavior without making network calls.
"""

from __future__ import annotations

from datetime import datetime, timezone

from trip_ema.domain import (
    ChangeSignal,
    NotificationBatch,
    SignalEvent,
    TickerRef,
    Timeframe,
)
from trip_ema.notifications import EmailTransport, NullTransport, WebhookTransport


NIFTY = TickerRef("NSE", "NIFTY", "INDEX")


def _batch(n_events: int = 1) -> NotificationBatch:
    t = datetime(2026, 5, 18, 4, 30, tzinfo=timezone.utc)
    events = [
        SignalEvent(
            ticker=NIFTY, timeframe=Timeframe.WEEKLY,
            bar_timestamp=t, signal=ChangeSignal.BUY,
            price_at_signal=24856.20 + i, computed_at=t,
        )
        for i in range(n_events)
    ]
    return NotificationBatch(subscriber_id=1, events=events, composed_at=t)


# ---------- NullTransport ---------- #

def test_null_transport_always_succeeds():
    t = NullTransport()
    assert t.send("anything", _batch()) is True
    assert t.send("anything", _batch(0)) is True


# ---------- EmailTransport ---------- #

def test_email_transport_calls_injected_sender():
    captured = []

    def fake_sender(msg):
        captured.append(msg)

    transport = EmailTransport(
        from_address="alerts@example.com",
        smtp_host="ignored",
        sender=fake_sender,
    )
    ok = transport.send("user@example.com", _batch(2))
    assert ok is True
    assert len(captured) == 1
    msg = captured[0]
    assert msg["To"] == "user@example.com"
    assert msg["From"] == "alerts@example.com"
    body = msg.get_content()
    assert "BUY signal on NSE:NIFTY" in body
    # 2 events ⇒ 2 indented lines in the body
    assert body.count("BUY signal") == 2


def test_email_transport_empty_batch_short_circuits():
    """No events ⇒ nothing sent, but result is still True (trivially)."""
    called = []
    transport = EmailTransport(
        from_address="x", smtp_host="x",
        sender=lambda m: called.append(m),
    )
    assert transport.send("u@x.com", _batch(0)) is True
    assert called == []


def test_email_transport_returns_false_on_sender_exception():
    def boom(msg):
        raise RuntimeError("smtp down")

    transport = EmailTransport(
        from_address="x", smtp_host="x", sender=boom,
    )
    assert transport.send("u@x.com", _batch()) is False


def test_email_transport_uses_configured_subject():
    captured = []
    transport = EmailTransport(
        from_address="x", smtp_host="x",
        subject="Trip EMA — your alerts",
        sender=lambda m: captured.append(m),
    )
    transport.send("u@x.com", _batch())
    assert captured[0]["Subject"] == "Trip EMA — your alerts"


# ---------- WebhookTransport ---------- #

def test_webhook_transport_posts_json_array():
    captured = []

    def fake_poster(url, body):
        captured.append((url, body))
        return True

    transport = WebhookTransport(poster=fake_poster)
    ok = transport.send("https://hook.example.com/x", _batch(3))
    assert ok is True
    assert len(captured) == 1
    url, body = captured[0]
    assert url == "https://hook.example.com/x"
    assert isinstance(body, list)
    assert len(body) == 3
    assert all(e["signal"] == "BUY" for e in body)


def test_webhook_transport_empty_batch_no_post():
    called = []
    transport = WebhookTransport(
        poster=lambda u, b: called.append((u, b)) or True,
    )
    assert transport.send("https://x", _batch(0)) is True
    assert called == []


def test_webhook_transport_returns_false_on_poster_failure():
    transport = WebhookTransport(poster=lambda u, b: False)
    assert transport.send("https://x", _batch()) is False
