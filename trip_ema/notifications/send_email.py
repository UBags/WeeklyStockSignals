"""V1 email sender: thin wrapper over the Brevo HTTP API.

Self-contained — does NOT depend on the legacy `EmailTransport`
class (whose `send()` takes a NotificationBatch shape that doesn't
fit the V1 `EmailBody`). The Brevo POST is simple enough (~10 lines)
that duplicating it here is cheaper than threading an adaptor
through EmailTransport. Step 14's legacy demolition can later
consolidate if Uddi wants.

`poster` injection for tests: a callable taking
`(url, headers, json_payload)`. Default uses `requests.post` with
`raise_for_status()`. Tests inject a fake that records the call or
raises a synthetic error.

`send_email` NEVER raises. Failures are captured in
`SendResult.error` and `SendResult.success=False` so the caller
(`notify_subscribers`, Step 9) can keep iterating across remaining
subscribers without a try/except around each call.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections.abc import Callable

from .compose_email import EmailBody
from ..persistence.subscribers_repo import SubscriberRecord


_DEFAULT_BREVO_ENDPOINT = "https://api.brevo.com/v3/smtp/email"


# Poster signature: (url, headers, json_payload) -> None.
# Success: returns normally. Failure: raises.
EmailPoster = Callable[[str, dict, dict], None]


@dataclass(frozen=True, slots=True)
class SendResult:
    """Outcome of one send_email call. Always populated — never
    raises out of send_email itself."""
    subscriber_id: int
    address: str
    success: bool
    error: str | None
    sent_at: datetime


def send_email(
    subscriber: SubscriberRecord,
    body: EmailBody,
    *,
    api_key: str,
    from_address: str,
    from_name: str = "WeeklyStockSignals",
    api_url: str = _DEFAULT_BREVO_ENDPOINT,
    timeout_seconds: float = 30.0,
    now: datetime,
    poster: EmailPoster | None = None,
) -> SendResult:
    """POST the V1 `EmailBody` to Brevo via the injected poster.

    Args:
        subscriber: target. Only `subscriber_id` and `address` are
            referenced; the rest of the record is informational.
        body: pre-rendered subject + HTML + plain text from
            `compose_email`.
        api_key: Brevo API key. Caller resolves this from
            `secrets/brevo.yaml` via the existing
            `trip_ema.config.brevo_secrets` loader.
        from_address: verified sender address registered with Brevo.
            V1 uses `weeklystocksignals@outlook.com` per the
            current operator setup.
        from_name: display name. Defaults to "WeeklyStockSignals".
        api_url: Brevo endpoint. Defaults to the v3 transactional
            endpoint; override only for testing against a different
            host.
        timeout_seconds: forwarded to the default poster's
            `requests.post(..., timeout=...)`. Ignored if `poster`
            is injected.
        now: stamped into `SendResult.sent_at`.
        poster: optional injection for tests. When None, uses
            `requests.post` with `raise_for_status()`.

    Returns:
        SendResult — never raises. On failure, success=False and
        error captures the exception class + message.
    """
    payload = {
        "sender": {"email": from_address, "name": from_name},
        "to": [{"email": subscriber.address}],
        "subject": body.subject,
        "htmlContent": body.html,
        "textContent": body.plain_text,
    }
    headers = {
        "api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    actual_poster = poster if poster is not None else (
        _make_default_poster(timeout_seconds)
    )

    try:
        actual_poster(api_url, headers, payload)
    except Exception as e:
        return SendResult(
            subscriber_id=subscriber.subscriber_id,
            address=subscriber.address,
            success=False,
            error=f"{type(e).__name__}: {e}",
            sent_at=now,
        )

    return SendResult(
        subscriber_id=subscriber.subscriber_id,
        address=subscriber.address,
        success=True,
        error=None,
        sent_at=now,
    )


def _make_default_poster(timeout_seconds: float) -> EmailPoster:
    """Closure that captures the timeout. `requests` is imported
    lazily so this module imports cheaply even when tests inject
    their own poster."""
    def post(url: str, headers: dict, payload: dict) -> None:
        import requests
        r = requests.post(
            url, headers=headers, json=payload,
            timeout=timeout_seconds,
        )
        r.raise_for_status()
    return post
