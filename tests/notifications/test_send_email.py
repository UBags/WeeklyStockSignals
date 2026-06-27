"""Tests for `trip_ema.notifications.send_email`.

Pure-Python — no DB. The function does a Brevo HTTP POST via an
injected `poster` callable; tests inject fakes that either return
normally (success) or raise (various failure modes).
"""
from __future__ import annotations

from datetime import datetime, UTC

import pytest

from trip_ema.notifications.compose_email import EmailBody
from trip_ema.notifications.send_email import (
    SendResult,
    send_email,
)
from trip_ema.persistence.subscribers_repo import SubscriberRecord


# ============================================================
# Helpers
# ============================================================

_SUBSCRIBER = SubscriberRecord(
    subscriber_id=42,
    transport="email",
    address="user@example.com",
    notify_cadence_minutes=60,
    last_notified_at=None,
    is_active=True,
)


_BODY = EmailBody(
    subject="[trip_ema] X — 1 signal (1 BUY, 0 SELL, 0 EXIT)",
    html="<div>body</div>",
    plain_text="body",
    signal_count=1,
)


def _now() -> datetime:
    return datetime(2026, 6, 26, 15, 0, tzinfo=UTC)


# ============================================================
# Success path
# ============================================================

class TestSuccess:

    def test_success_returns_success_true(self):
        captured: dict = {}

        def fake_poster(url, headers, payload):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = payload
            # Success: return normally

        result = send_email(
            _SUBSCRIBER, _BODY,
            api_key="xkeysib-test-key",
            from_address="sender@outlook.com",
            now=_now(),
            poster=fake_poster,
        )

        assert isinstance(result, SendResult)
        assert result.success is True
        assert result.error is None
        assert result.subscriber_id == _SUBSCRIBER.subscriber_id
        assert result.address == _SUBSCRIBER.address
        assert result.sent_at == _now()

    def test_payload_has_brevo_shape(self):
        captured: dict = {}

        def fake_poster(url, headers, payload):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = payload

        send_email(
            _SUBSCRIBER, _BODY,
            api_key="xkeysib-test-key",
            from_address="sender@outlook.com",
            from_name="Custom Name",
            now=_now(),
            poster=fake_poster,
        )

        payload = captured["payload"]
        assert payload["sender"]["email"] == "sender@outlook.com"
        assert payload["sender"]["name"] == "Custom Name"
        assert payload["to"] == [{"email": "user@example.com"}]
        assert payload["subject"] == _BODY.subject
        assert payload["htmlContent"] == _BODY.html
        assert payload["textContent"] == _BODY.plain_text

    def test_headers_include_api_key(self):
        captured: dict = {}

        def fake_poster(url, headers, payload):
            captured["headers"] = headers

        send_email(
            _SUBSCRIBER, _BODY,
            api_key="xkeysib-specific-key-123",
            from_address="sender@outlook.com",
            now=_now(),
            poster=fake_poster,
        )

        assert captured["headers"]["api-key"] == (
            "xkeysib-specific-key-123"
        )
        assert captured["headers"]["Content-Type"] == "application/json"
        assert captured["headers"]["Accept"] == "application/json"

    def test_default_api_url(self):
        captured: dict = {}

        def fake_poster(url, headers, payload):
            captured["url"] = url

        send_email(
            _SUBSCRIBER, _BODY,
            api_key="xkeysib-test",
            from_address="s@x.com",
            now=_now(),
            poster=fake_poster,
        )
        assert captured["url"] == "https://api.brevo.com/v3/smtp/email"

    def test_custom_api_url_override(self):
        captured: dict = {}

        def fake_poster(url, headers, payload):
            captured["url"] = url

        send_email(
            _SUBSCRIBER, _BODY,
            api_key="xkeysib-test",
            from_address="s@x.com",
            api_url="https://api.brevo-staging.example/v3/smtp/email",
            now=_now(),
            poster=fake_poster,
        )
        assert captured["url"] == (
            "https://api.brevo-staging.example/v3/smtp/email"
        )


# ============================================================
# Failure paths
# ============================================================

class TestFailure:

    def test_poster_raises_returns_success_false(self):
        def failing_poster(url, headers, payload):
            raise ConnectionError("simulated network failure")

        result = send_email(
            _SUBSCRIBER, _BODY,
            api_key="xkeysib-test",
            from_address="s@x.com",
            now=_now(),
            poster=failing_poster,
        )

        assert result.success is False
        assert result.error is not None
        assert "ConnectionError" in result.error
        assert "simulated network failure" in result.error

    def test_4xx_response_returns_failure(self):
        """A 4xx is signalled by `raise_for_status()` in the default
        poster; tests simulate by raising HTTPError-like objects."""
        class FakeHTTPError(Exception):
            pass

        def failing_poster(url, headers, payload):
            raise FakeHTTPError("400 Bad Request")

        result = send_email(
            _SUBSCRIBER, _BODY,
            api_key="xkeysib-test",
            from_address="s@x.com",
            now=_now(),
            poster=failing_poster,
        )

        assert result.success is False
        assert "FakeHTTPError" in result.error
        assert "400 Bad Request" in result.error

    def test_timeout_returns_failure(self):
        class FakeTimeoutError(Exception):
            pass

        def slow_poster(url, headers, payload):
            raise FakeTimeoutError("read timeout after 30s")

        result = send_email(
            _SUBSCRIBER, _BODY,
            api_key="xkeysib-test",
            from_address="s@x.com",
            now=_now(),
            poster=slow_poster,
        )

        assert result.success is False
        assert "FakeTimeoutError" in result.error

    def test_never_raises(self):
        """send_email's contract is that it NEVER raises out, no
        matter what the poster does. Failure → SendResult with
        success=False."""

        def evil_poster(url, headers, payload):
            raise RuntimeError("boom")

        # If send_email raised, this would propagate
        result = send_email(
            _SUBSCRIBER, _BODY,
            api_key="xkeysib-test",
            from_address="s@x.com",
            now=_now(),
            poster=evil_poster,
        )
        assert result.success is False


# ============================================================
# Result shape
# ============================================================

class TestResultShape:

    def test_subscriber_id_and_address_round_trip(self):
        def fake_poster(url, headers, payload):
            pass

        result = send_email(
            _SUBSCRIBER, _BODY,
            api_key="x", from_address="s@x.com",
            now=_now(), poster=fake_poster,
        )
        assert result.subscriber_id == 42
        assert result.address == "user@example.com"

    def test_sent_at_from_now_param(self):
        explicit = datetime(2026, 12, 31, 23, 59, tzinfo=UTC)

        def fake_poster(url, headers, payload):
            pass

        result = send_email(
            _SUBSCRIBER, _BODY,
            api_key="x", from_address="s@x.com",
            now=explicit, poster=fake_poster,
        )
        assert result.sent_at == explicit
