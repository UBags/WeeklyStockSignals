"""Tests for the HTML email rendering path.

Covers:
  - batcher.render_html with various enrichment shapes
  - The Indian-numbering crore formatter
  - EmailTransport's enrichment_provider path: htmlContent appears
    alongside textContent when a provider is wired, falls back to
    text-only when the provider raises, and stays text-only when no
    provider is wired (legacy behavior).

No DB involved — enrichment.BatchEnrichment is constructed directly in
the tests. The end-to-end DB-driven path is integration-tested
separately (`tests/notifications/test_enrichment_sql.py` if/when added).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trip_ema.domain import (
    ChangeSignal,
    NotificationBatch,
    SignalEvent,
    TickerRef,
    Timeframe,
)
from trip_ema.notifications import EmailTransport
from trip_ema.notifications.batcher import (
    _format_indian_crores,
    render_html,
)
from trip_ema.notifications.enrichment import (
    BatchEnrichment,
    EMPTY_ENRICHMENT,
    HistoricalSignal,
    TickerEnrichment,
)


# ---------- Helpers ---------- #


RELIANCE = TickerRef("NSE", "RELIANCE", "EQ")
TCS = TickerRef("NSE", "TCS", "EQ")
HDFC = TickerRef("NSE", "HDFCBANK", "EQ")

_NOW = datetime(2026, 6, 18, 4, 30, tzinfo=timezone.utc)


def _event(
    ticker: TickerRef,
    signal: ChangeSignal,
    price: float = 100.0,
) -> SignalEvent:
    return SignalEvent(
        ticker=ticker,
        timeframe=Timeframe.WEEKLY,
        bar_timestamp=_NOW,
        signal=signal,
        price_at_signal=price,
        computed_at=_NOW,
    )


def _batch(events: list[SignalEvent]) -> NotificationBatch:
    return NotificationBatch(subscriber_id=1, events=events, composed_at=_NOW)


def _enrichment_for(
    *items: tuple[TickerRef, dict],
) -> BatchEnrichment:
    """Build a BatchEnrichment from (ref, kwargs) pairs."""
    by_ticker: dict[tuple[str, str, str], TickerEnrichment] = {}
    for ref, kwargs in items:
        by_ticker[(ref.exchange, ref.symbol, ref.instrument)] = TickerEnrichment(
            ticker_ref=ref, **kwargs,
        )
    return BatchEnrichment(by_ticker=by_ticker)


# ---------- _format_indian_crores ---------- #


class TestIndianCrores:
    """RELIANCE-shaped market caps reformat into Indian lakh/crore
    grouping — '₹19,84,302 Cr' rather than the Western '₹1,984,302 Cr'."""

    def test_reliance_shape(self):
        # 19,84,302 Cr = 19_84_302 * 10^7 raw rupees
        assert _format_indian_crores(19_84_302 * 10_000_000) == "₹19,84,302 Cr"

    def test_five_digit_crores(self):
        assert _format_indian_crores(50_000 * 10_000_000) == "₹50,000 Cr"

    def test_one_crore(self):
        assert _format_indian_crores(1 * 10_000_000) == "₹1 Cr"

    def test_sub_crore_rounds_down(self):
        assert _format_indian_crores(5_000_000) == "₹0 Cr"

    def test_one_thousand_crores(self):
        assert _format_indian_crores(1_000 * 10_000_000) == "₹1,000 Cr"

    def test_ten_thousand_crores(self):
        assert _format_indian_crores(10_000 * 10_000_000) == "₹10,000 Cr"

    def test_one_lakh_crores(self):
        # 1,00,000 Cr
        assert _format_indian_crores(1_00_000 * 10_000_000) == "₹1,00,000 Cr"

    def test_one_lakh_thirty_thousand_crores(self):
        # HDFCBANK-ish: 11,87,440 Cr
        assert _format_indian_crores(11_87_440 * 10_000_000) == "₹11,87,440 Cr"

    def test_none_renders_em_dash(self):
        assert _format_indian_crores(None) == "—"


# ---------- render_html ---------- #


class TestRenderHTML:

    def test_empty_batch_returns_empty_string(self):
        assert render_html(_batch([])) == ""

    def test_renders_table_with_buy_section(self):
        html = render_html(
            _batch([_event(RELIANCE, ChangeSignal.BUY, 2955.10)]),
            EMPTY_ENRICHMENT,
        )
        assert "<table" in html
        assert "BUY signals" in html
        assert "RELIANCE" in html
        assert "Last 5 trading days signals" in html

    def test_no_enrichment_renders_em_dashes(self):
        """When the BatchEnrichment is empty, decoration columns
        degrade to '—' / NEUTRAL — the firing event still renders
        (Stock + Last 5 absent but the row is there)."""
        html = render_html(
            _batch([_event(RELIANCE, ChangeSignal.BUY, 2955.10)]),
            EMPTY_ENRICHMENT,
        )
        assert "—" in html
        assert "NEUTRAL" in html

    def test_sections_appear_in_fixed_order(self):
        """Even if events arrive in a different order, sections render
        BUY → SELL → Exit Buy → Exit Sell."""
        events = [
            _event(HDFC, ChangeSignal.EXIT_FROM_SELL),
            _event(TCS, ChangeSignal.SELL),
            _event(RELIANCE, ChangeSignal.BUY),
        ]
        html = render_html(_batch(events), EMPTY_ENRICHMENT)
        i_buy = html.index("BUY signals")
        i_sell = html.index("SELL signals")
        i_exit = html.index("Exit Sell signals")
        assert i_buy < i_sell < i_exit

    def test_empty_sections_are_skipped(self):
        """A batch with only BUYs shows the BUY section and NO others."""
        html = render_html(
            _batch([_event(RELIANCE, ChangeSignal.BUY)]),
            EMPTY_ENRICHMENT,
        )
        assert "BUY signals" in html
        assert "SELL signals" not in html
        assert "Exit Buy signals" not in html
        assert "Exit Sell signals" not in html

    def test_rows_within_section_sorted_alphabetically(self):
        events = [
            _event(TCS, ChangeSignal.BUY),
            _event(HDFC, ChangeSignal.BUY),
            _event(RELIANCE, ChangeSignal.BUY),
        ]
        html = render_html(_batch(events), EMPTY_ENRICHMENT)
        i_hdfc = html.index("HDFCBANK")
        i_rel  = html.index("RELIANCE")
        i_tcs  = html.index("TCS")
        assert i_hdfc < i_rel < i_tcs

    def test_enrichment_fields_appear_in_row(self):
        recent = (
            HistoricalSignal(
                computed_at=_NOW - timedelta(days=2),
                signal_type="BUY",
                price_at_signal=2941.20,
            ),
            HistoricalSignal(
                computed_at=_NOW,
                signal_type="BUY",
                price_at_signal=2955.10,
            ),
        )
        enrichment = _enrichment_for(
            (RELIANCE, dict(
                sector="Energy",
                industry="Oil & Gas Integrated",
                market_cap_rupees=19_84_302 * 10_000_000,
                sector_zone=1,
                sector_direction=1,
                recent_signals=recent,
            )),
        )
        html = render_html(
            _batch([_event(RELIANCE, ChangeSignal.BUY, 2955.10)]),
            enrichment,
        )
        assert "₹19,84,302 Cr" in html
        # Industry contains '&', so it must be HTML-escaped to '&amp;'
        assert "Oil &amp; Gas Integrated" in html
        assert "GREEN" in html
        assert "▲" in html
        # Both recent signals appear, separated by <br>
        assert "2,941.20" in html
        assert "2,955.10" in html
        assert "<br>" in html

    def test_negative_zone_renders_red(self):
        enrichment = _enrichment_for(
            (TCS, dict(sector_zone=-1, sector_direction=-1)),
        )
        html = render_html(
            _batch([_event(TCS, ChangeSignal.SELL)]),
            enrichment,
        )
        assert "RED" in html
        assert "▼" in html

    def test_neutral_zone_uses_grey_dot(self):
        enrichment = _enrichment_for(
            (TCS, dict(sector_zone=0, sector_direction=0)),
        )
        html = render_html(
            _batch([_event(TCS, ChangeSignal.SELL)]),
            enrichment,
        )
        assert "NEUTRAL" in html

    def test_html_escapes_ticker_symbol(self):
        """Defensive: if a ticker symbol ever contained '<' or '&'
        (it shouldn't, but the renderer must not fail open) it should
        be escaped."""
        weird = TickerRef("NSE", "A&B", "EQ")
        html = render_html(
            _batch([_event(weird, ChangeSignal.BUY)]),
            EMPTY_ENRICHMENT,
        )
        assert "A&amp;B" in html
        assert "<b>A&B</b>" not in html


# ---------- EmailTransport with enrichment_provider ---------- #


class TestEmailTransportEnrichment:

    def _capture_payload(self, **transport_kwargs) -> tuple[EmailTransport, list]:
        captured: list = []
        def fake_poster(url, headers, payload):
            captured.append((url, headers, payload))
        t = EmailTransport(
            api_key="xkeysib-test-fake-key",
            from_address="alerts@example.com",
            poster=fake_poster,
            **transport_kwargs,
        )
        return t, captured

    def test_no_provider_sends_text_only(self):
        """Default construction (no enrichment_provider) preserves the
        legacy behavior: textContent only, no htmlContent key."""
        t, captured = self._capture_payload()
        t.send("u@x.com", _batch([_event(RELIANCE, ChangeSignal.BUY)]))
        _, _, payload = captured[0]
        assert "textContent" in payload
        assert "htmlContent" not in payload

    def test_provider_produces_html_part(self):
        """When wired, the provider's output is rendered to HTML and
        attached to the Brevo payload alongside the text part."""
        provider_calls = []
        def provider(batch):
            provider_calls.append(batch)
            return _enrichment_for(
                (RELIANCE, dict(
                    sector="Energy",
                    industry="Oil & Gas Integrated",
                    market_cap_rupees=19_84_302 * 10_000_000,
                    sector_zone=1,
                    sector_direction=1,
                    recent_signals=(),
                )),
            )

        t, captured = self._capture_payload(enrichment_provider=provider)
        t.send("u@x.com", _batch([_event(RELIANCE, ChangeSignal.BUY)]))

        assert len(provider_calls) == 1
        _, _, payload = captured[0]
        assert "textContent" in payload  # still there for fallback
        assert "htmlContent" in payload
        # Spot-check HTML body content
        assert "<table" in payload["htmlContent"]
        assert "RELIANCE" in payload["htmlContent"]
        assert "₹19,84,302 Cr" in payload["htmlContent"]

    def test_provider_exception_falls_back_to_text_only(self):
        """An enrichment provider that raises should NOT block delivery
        — the send still goes through with text-only content."""
        def broken_provider(batch):
            raise RuntimeError("simulated DB down")

        t, captured = self._capture_payload(enrichment_provider=broken_provider)
        ok = t.send("u@x.com", _batch([_event(RELIANCE, ChangeSignal.BUY)]))

        assert ok is True            # delivery still succeeded
        assert len(captured) == 1    # POST was made
        _, _, payload = captured[0]
        assert "textContent" in payload
        assert "htmlContent" not in payload  # degraded gracefully

    def test_empty_batch_does_not_call_provider(self):
        """Short-circuit on empty batch: nothing to send, no DB
        roundtrip for enrichment."""
        provider_calls = []
        def provider(batch):
            provider_calls.append(batch)
            return EMPTY_ENRICHMENT

        t, captured = self._capture_payload(enrichment_provider=provider)
        ok = t.send("u@x.com", _batch([]))

        assert ok is True
        assert captured == []
        assert provider_calls == []