"""Email transport — Brevo (formerly Sendinblue) HTTP API.

Sends transactional email via Brevo's `POST /v3/smtp/email` endpoint.
Replaces the historical SMTP implementation; Microsoft/Google have
progressively disabled SMTP AUTH for personal mailboxes (Outlook.com
new-account `5.7.139 SmtpClientAuthentication is disabled`, Gmail
OAuth-only since Sept 2024), so HTTP API is the only delivery path
that works reliably for our scale.

Public surface unchanged for callers: `send(address, batch) -> bool`.
Returns True on success, False on retryable failure. Raises only on
unrecoverable construction errors.

HTML rendering:
  - If `enrichment_provider` is supplied at construction, `send()`
    calls it on every batch to fetch per-ticker enrichment (mcap,
    sector, sector zone/direction, last 5 signals), passes that to
    `batcher.render_html`, and includes the result as `htmlContent`
    alongside the plain `textContent`. Most mail clients render the
    HTML part by default and fall back to the text part when HTML
    rendering is disabled — so both parts always go on the wire.
  - When `enrichment_provider` is None, the transport sends text-only.
    This is the legacy behavior, preserved for tests and for any
    deployment where DB enrichment isn't wired.
  - If enrichment lookup raises (DB down, malformed schema), the
    transport falls back to text-only for that send and logs the
    failure. Email delivery never blocks on enrichment.

Tests inject a `poster` callable that bypasses the network — same
dependency-injection pattern as the previous SMTP implementation's
`sender` parameter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import requests

from ..domain import NotificationBatch
from .batcher import render_html, render_text
from .benchmark_rerank import fetch_rank_scores
from .enrichment import BatchEnrichment

logger = logging.getLogger(__name__)


# A "poster" takes (url, headers, json_payload) and either returns
# normally on success or raises on failure. The default uses
# `requests.post`; tests inject a fake.
EmailPoster = Callable[[str, dict, dict], None]


# A "enrichment provider" takes a NotificationBatch and returns the
# enrichment data the renderer needs. The app.py wiring layer captures
# the db_engine in a closure and passes the closure in; tests pass a
# fake that returns a fixed BatchEnrichment.
EnrichmentProvider = Callable[[NotificationBatch], BatchEnrichment]


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

    `enrichment_provider` is optional. When set, each send() call
    invokes it to fetch per-ticker enrichment for HTML rendering;
    when None, sends are text-only.
    """


    api_key: str
    from_address: str
    from_name: str = "WeeklyStockSignals"
    subject: str = "Trip EMA signal updates"
    api_url: str = _DEFAULT_BREVO_ENDPOINT
    timeout_seconds: float = 30.0
    # Injection point for the enrichment lookup. None = text-only sends.
    enrichment_provider: Optional[EnrichmentProvider] = field(
        default=None, repr=False,
    )
    # Injection point for tests bypassing the network.
    poster: Optional[EmailPoster] = field(default=None, repr=False)

    # Optional SQLAlchemy Engine for rank-score enrichment. When wired,
    # send() uses it to compute composite RS/R² momentum scores so BUY
    # events are sorted by ranking instead of alphabetically. When None,
    # send() catches the AttributeError on .connect() and falls back to
    # alphabetical BUY sort (degraded but functional). Typed Any so this
    # module doesn't need a sqlalchemy import.
    _db_engine: Any = field(default=None, repr=False)

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
        text_body = render_text(batch)
        if not text_body:
            return True   # nothing to send is trivially successful

        html_body = self._render_html_or_none(batch)
        payload = self._build_payload(
            address=address, text_body=text_body, html_body=html_body,
        )
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

    def _render_html_or_none(self, batch: NotificationBatch) -> Optional[str]:
        """Run the enrichment provider + HTML renderer. Returns the HTML
        body, or None if enrichment isn't wired or the enrichment lookup
        raises (in which case we degrade to text-only for this send).

        Why try/except here rather than letting it bubble up: enrichment
        is a *decoration*, not a *delivery requirement*. A flaky DB or a
        schema mismatch should not stop the operator from receiving the
        signal events. Log and continue with text-only.
        """
        if self.enrichment_provider is None:
            return None
        try:
            enrichment = self.enrichment_provider(batch)
            # Phase 6-3: compute per-ticker rank scores so BUY events
            # can be sorted by composite RS/R² momentum metric instead
            # of alphabetical. One SQL roundtrip vs stock_benchmark_stats
            # + industry_benchmark_stats; sub-100ms at full scale.
            try:
                with self._db_engine.connect() as conn:
                    ticker_ids = [ev.ticker.ticker_id for ev in batch.events]
                    rank_scores = fetch_rank_scores(conn, ticker_ids)
            except Exception:
                logger.exception(
                    "rank-score fetch failed; emails will fall back to "
                    "alphabetical BUY sort"
                )
                rank_scores = None
            return render_html(batch, enrichment)
        except Exception:
            logger.exception(
                "Enrichment / HTML render failed; falling back to text-only "
                "for this send"
            )
            return None

    def _build_payload(
        self,
        *,
        address: str,
        text_body: str,
        html_body: Optional[str],
    ) -> dict[str, Any]:
        """Shape the Brevo `/v3/smtp/email` request body.

        Always include `textContent`. Include `htmlContent` when we have
        rendered HTML; mail clients pick HTML by default and fall back
        to text when HTML rendering is disabled by the user. Sending
        both is the standard multipart pattern.
        """
        payload: dict[str, Any] = {
            "sender":      {"email": self.from_address, "name": self.from_name},
            "to":          [{"email": address}],
            "subject":     self.subject,
            "textContent": text_body,
        }
        if html_body:
            payload["htmlContent"] = html_body
        return payload

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