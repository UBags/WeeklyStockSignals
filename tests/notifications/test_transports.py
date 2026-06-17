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


# ---------- EmailTransport (Brevo HTTP API) ---------- #

def test_email_transport_calls_injected_poster():
    captured = []

    def fake_poster(url, headers, payload):
        captured.append((url, headers, payload))

    transport = EmailTransport(
        api_key="xkeysib-test-fake-key",
        from_address="alerts@example.com",
        poster=fake_poster,
    )
    ok = transport.send("user@example.com", _batch(2))
    assert ok is True
    assert len(captured) == 1
    url, headers, payload = captured[0]
    assert url == "https://api.brevo.com/v3/smtp/email"
    # api-key header must be present and equal to the configured key
    assert headers["api-key"] == "xkeysib-test-fake-key"
    assert headers["content-type"] == "application/json"
    # Payload shape matches Brevo's /v3/smtp/email contract
    assert payload["sender"] == {
        "email": "alerts@example.com", "name": "WeeklyStockSignals",
    }
    assert payload["to"] == [{"email": "user@example.com"}]
    assert payload["subject"] == "Trip EMA signal updates"
    body = payload["textContent"]
    assert "BUY signal on NSE:NIFTY" in body
    # 2 events ⇒ 2 indented lines in the body
    assert body.count("BUY signal") == 2


def test_email_transport_empty_batch_short_circuits():
    """No events ⇒ nothing sent, but result is still True (trivially)."""
    called = []
    transport = EmailTransport(
        api_key="xkeysib-test-fake-key",
        from_address="x@example.com",
        poster=lambda u, h, p: called.append((u, h, p)),
    )
    assert transport.send("u@x.com", _batch(0)) is True
    assert called == []


def test_email_transport_returns_false_on_poster_exception():
    def boom(url, headers, payload):
        raise RuntimeError("brevo 500")

    transport = EmailTransport(
        api_key="xkeysib-test-fake-key",
        from_address="x@example.com",
        poster=boom,
    )
    assert transport.send("u@x.com", _batch()) is False


def test_email_transport_uses_configured_subject_and_from_name():
    captured = []
    transport = EmailTransport(
        api_key="xkeysib-test-fake-key",
        from_address="x@example.com",
        from_name="Operator Bot",
        subject="Trip EMA — your alerts",
        poster=lambda u, h, p: captured.append((u, h, p)),
    )
    transport.send("u@x.com", _batch())
    _, _, payload = captured[0]
    assert payload["subject"] == "Trip EMA — your alerts"
    assert payload["sender"]["name"] == "Operator Bot"


def test_email_transport_uses_configured_api_url():
    captured = []
    transport = EmailTransport(
        api_key="xkeysib-test-fake-key",
        from_address="x@example.com",
        api_url="https://internal-proxy.example.com/brevo",
        poster=lambda u, h, p: captured.append((u, h, p)),
    )
    transport.send("u@x.com", _batch())
    url, _, _ = captured[0]
    assert url == "https://internal-proxy.example.com/brevo"


def test_email_transport_requires_api_key():
    """Guards the no-silent-NullTransport invariant: a misconfigured
    EmailTransport must fail loudly at construction, not silently
    succeed at send time."""
    import pytest
    with pytest.raises(ValueError, match="api_key is required"):
        EmailTransport(api_key="", from_address="x@example.com")
    with pytest.raises(ValueError, match="api_key is required"):
        EmailTransport(api_key=None, from_address="x@example.com")  # type: ignore[arg-type]


def test_email_transport_requires_from_address():
    import pytest
    with pytest.raises(ValueError, match="from_address is required"):
        EmailTransport(api_key="xkeysib-test-fake-key", from_address="")


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
