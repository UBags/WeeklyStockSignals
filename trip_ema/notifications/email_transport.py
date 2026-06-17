"""Email transport — Brevo (formerly Sendinblue) HTTP API.

Sends transactional email via Brevo's `POST /v3/smtp/email` endpoint.
Replaces the historical SMTP implementation; Microsoft/Google have
progressively disabled SMTP AUTH for personal mailboxes (Outlook.com
new-account `5.7.139 SmtpClientAuthentication is disabled`, Gmail
OAuth-only since Sept 2024), so HTTP API is the only delivery path
that works reliably for our scale.

Public surface unchanged: `send(address, batch) -> bool`. Returns True
on success, False on retryable failure. Raises only on unrecoverable
construction errors.

Tests inject a `poster` callable that bypasses the network — same
dependency-injection pattern as the previous SMTP implementation's
`sender` parameter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from ..domain import NotificationBatch
from .batcher import render_text

logger = logging.getLogger(__name__)


# A "poster" takes (url, headers, json_payload) and either returns
# normally on success or raises on failure. The default uses
# `requests.post`; tests inject a fake.
EmailPoster = Callable[[str, dict, dict], None]


_DEFAULT_BREVO_ENDPOINT = "https://api.brevo.com/v3/smtp/email"


@dataclass
class EmailTransport:
    """Brevo HTTP API email transport.

    Construction-time validation: api_key is required and must be a
    non-empty string. We deliberately do NOT default it from the
    environment here — config-layer loaders (config.loader,
    config.brevo_secrets) are responsible for resolving the secret
    and passing it in. This keeps the transport itself environment-
    agnostic (testable, deterministic) and avoids the silent-
    NullTransport bug-class we hit with the SMTP version's
    env-var fallback.
    """

    api_key: str
    from_address: str
    from_name: str = "WeeklyStockSignals"
    subject: str = "Trip EMA signal updates"
    api_url: str = _DEFAULT_BREVO_ENDPOINT
    timeout_seconds: float = 30.0
    # Injection point for tests.
    poster: EmailPoster | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.api_key or not isinstance(self.api_key, str):
            raise ValueError(
                "EmailTransport.api_key is required (non-empty str). "
                "If you intended to disable email delivery, construct "
                "NullTransport() instead."
            )
        if not self.from_address:
            raise ValueError("EmailTransport.from_address is required")

    def send(self, address: str, batch: NotificationBatch) -> bool:
        body = render_text(batch)
        if not body:
            return True   # nothing to send is trivially successful

        payload = self._build_payload(address=address, body=body)
        headers = {
            "api-key": self.api_key,
            "accept": "application/json",
            "content-type": "application/json",
        }

        try:
            if self.poster is not None:
                self.poster(self.api_url, headers, payload)
            else:
                self._post(headers, payload)
            return True
        except Exception:
            logger.exception("Email delivery failed for %s", address)
            return False

    def _build_payload(self, *, address: str, body: str) -> dict[str, Any]:
        """Shape the Brevo `/v3/smtp/email` request body.

        We send plain text only (no HTML alt). The Pine-style batcher
        output is already designed for human-readable scanning; HTML
        wrapping adds Promotions-tab signal to Gmail without helping
        the operator. If we later want HTML, attach an `htmlContent`
        key here built from the same `body` via a tiny wrapper.
        """
        return {
            "sender":      {"email": self.from_address, "name": self.from_name},
            "to":          [{"email": address}],
            "subject":     self.subject,
            "textContent": body,
        }

    def _post(self, headers: dict, payload: dict) -> None:
        """Production POST path. Raises on any non-2xx response so the
        outer `send()` returns False and the circuit breaker counts a
        failure. Brevo returns 201 Created with `{"messageId": "..."}`
        on success."""
        resp = requests.post(
            self.api_url,
            headers=headers,
            json=payload,
            timeout=self.timeout_seconds,
        )
        # raise_for_status covers 4xx and 5xx. Brevo's 400s on bad
        # sender / missing fields are NOT retryable, but we don't
        # try to distinguish here — the circuit breaker and DLQ
        # ladder above us absorb both cases. A persistent 400 trips
        # the breaker just like a persistent 503, which is what we
        # want: stop hitting Brevo until an operator intervenes.
        resp.raise_for_status()


# ---------- Backward-compat alias ------------------------------------------
# Some older modules may still import `EmailSender` from this file. The
# old name described an `EmailMessage`-shaped callable; the new name is
# `EmailPoster` (url, headers, payload). The alias keeps the import
# resolvable so a downstream file that hasn't been updated yet doesn't
# crash at import-time. Will be removed after a deprecation cycle.
EmailSender = EmailPoster
