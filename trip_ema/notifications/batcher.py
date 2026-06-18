"""Compose a NotificationBatch into transport-ready payloads.

Three renderings:
  - text:  human-readable, one event per line. Used by all transports
           as the multipart text/plain part (and the SMS / Slack body).
  - html:  rich, color-coded, grouped-by-signal-type tables. Used as
           the multipart text/html part for email — built around the
           per-ticker enrichment loaded by `notifications.enrichment`.
  - json:  array of structured events. Used by webhook POST bodies.

Kept here (rather than inside each transport) so the format is
consistent across transports and so multiple transports can share the
work for the same batch.

For HTML rendering:
  - Sections are emitted in a fixed order: BUY, SELL, EXIT_FROM_BUY,
    EXIT_FROM_SELL. Empty sections are skipped silently.
  - Within a section, rows are sorted by symbol alphabetically — the
    cycle's emission order is not informative to a human reader, and
    alphabetical makes diffing two consecutive emails much easier.
  - Colors match the Signals-tab chart palette: green=#1f9c5e,
    red=#c84a4a, neutral=#555. Keeps the chart and the email visually
    coherent.
  - All styles are inline. Gmail strips <style> blocks aggressively;
    inline survives.
  - The Indian numbering helper (lakhs/crores) gives outputs like
    '₹19,84,302 Cr' to match the spec.
"""

from __future__ import annotations

import html
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Iterable, Optional

from ..domain import ChangeSignal, NotificationBatch, SignalEvent
from .enrichment import (
    BatchEnrichment,
    EMPTY_ENRICHMENT,
    HistoricalSignal,
    TickerEnrichment,
)


_SIGNAL_VERB = {
    ChangeSignal.BUY: "BUY",
    ChangeSignal.SELL: "SELL",
    ChangeSignal.EXIT_FROM_BUY: "EXIT",
    ChangeSignal.EXIT_FROM_SELL: "EXIT",
}

# Section order in the HTML email — fixed, NOT alphabetical, because
# operators read 'fresh BUY' and 'fresh SELL' before EXIT housekeeping.
_HTML_SECTION_ORDER = [
    ("BUY",            "BUY"),
    ("SELL",           "SELL"),
    ("EXIT_FROM_BUY",  "Exit Buy"),
    ("EXIT_FROM_SELL", "Exit Sell"),
]

# Palette matches gui/views/signals.py so chart and email read together.
_COLOR_BULL = "#1f9c5e"   # green
_COLOR_BEAR = "#c84a4a"   # red
_COLOR_NEUT = "#555555"   # grey
_COLOR_TEXT = "#222222"
_COLOR_MUTED = "#888888"
_COLOR_BORDER = "#dddddd"
_COLOR_BORDER_STRONG = "#333333"


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
    """Multi-line text body for email (text/plain part), SMS, Slack."""
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


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def render_html(
    batch: NotificationBatch,
    enrichment: BatchEnrichment | None = None,
    *,
    tz=None,
) -> str:
    """Grouped, color-coded HTML email body.

    Args:
        batch: the notification batch to render. Empty batches return ''.
        enrichment: optional per-ticker enrichment. When None or empty,
            the table still renders with '—' placeholders in the
            decoration columns — the firing event is always known so
            the Stock / Last 5 columns are populated regardless.
        tz: optional timezone to render timestamps in. None = render
            in whatever zone the event timestamps carry. The bar
            timestamps in this project are UTC-stored, IST-bucketed,
            so passing IST here is conventional.
    """
    if not batch.events:
        return ""
    if enrichment is None:
        enrichment = EMPTY_ENRICHMENT

    # Group events by signal_type. Each ticker appears at most once per
    # section (a single (ticker, signal_type) fires once per cycle).
    by_type: dict[str, list[SignalEvent]] = {key: [] for key, _ in _HTML_SECTION_ORDER}
    for ev in batch.events:
        name = ev.signal.name
        by_type.setdefault(name, []).append(ev)

    parts: list[str] = []
    parts.append(_html_preamble(total_events=len(batch.events)))

    for signal_key, signal_label in _HTML_SECTION_ORDER:
        events = by_type.get(signal_key, [])
        if not events:
            continue
        parts.append(_html_section(signal_label, signal_key, events, enrichment, tz))

    parts.append(_html_footer(total_events=len(batch.events)))
    return "\n".join(parts)


# ---------- HTML helpers ---------- #


def _html_preamble(*, total_events: int) -> str:
    """Top of the email body: greeting + total-count line.

    Kept plain so it survives Gmail's preview pane truncation cleanly —
    the operator should see 'N signals' even if Gmail clips the rest.
    """
    return (
        f'<div style="font-family: Arial, Helvetica, sans-serif; '
        f'color: {_COLOR_TEXT}; max-width: 1100px;">'
        f'<p style="font-size: 14px; margin: 0 0 16px 0;">'
        f"Signal updates for your subscriptions — {total_events} event"
        f"{'s' if total_events != 1 else ''} in this batch."
        f'</p>'
    )


def _html_footer(*, total_events: int) -> str:
    """Bottom of the email body: small grey caption + closing div."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f'<p style="font-size: 11px; color: {_COLOR_MUTED}; '
        f'margin: 24px 0 0 0;">'
        f"{total_events} total event{'s' if total_events != 1 else ''}; "
        f"generated at {now}. "
        f"Sector Zone = latest display_regime of the ticker's industry; "
        f"Sector Direction = sign of the day-over-day change in the "
        f"industry index value. ▲/▼/● render natively as colored "
        f"glyphs in HTML mail; plain-text fallback uses uncolored ^/v/-."
        f'</p>'
        f'</div>'
    )


def _html_section(
    label: str,
    signal_key: str,
    events: list[SignalEvent],
    enrichment: BatchEnrichment,
    tz,
) -> str:
    """One <h2> heading + one <table> for a single signal-type section."""
    sorted_events = sorted(events, key=lambda e: e.ticker.symbol)

    rows_html: list[str] = []
    for ev in sorted_events:
        rows_html.append(_html_row(ev, enrichment.get(ev.ticker), tz))

    return (
        f'<h2 style="font-size: 16px; margin: 24px 0 8px 0; '
        f'color: {_COLOR_TEXT};">'
        f'{html.escape(label)} signals '
        f'<span style="color: {_COLOR_MUTED}; font-weight: normal;">'
        f'({len(events)})'
        f'</span>'
        f'</h2>'
        f'<table style="width: 100%; max-width: 1100px; '
        f'border-collapse: collapse; font-family: Arial, sans-serif; '
        f'font-size: 13px; color: {_COLOR_TEXT};">'
        f'<thead>'
        f'<tr style="border-bottom: 2px solid {_COLOR_BORDER_STRONG};">'
        f'<th align="left"   style="padding: 8px 10px; width: 110px;">Stock</th>'
        f'<th align="right"  style="padding: 8px 10px; width: 130px;">Mkt Cap</th>'
        f'<th align="left"   style="padding: 8px 10px; width: 200px;">Sector</th>'
        f'<th align="center" style="padding: 8px 10px; width: 110px;">Sector Zone</th>'
        f'<th align="center" style="padding: 8px 10px; width: 110px;">Sector Direction</th>'
        f'<th align="left"   style="padding: 8px 10px;">Last 5 trading days signals (asc)</th>'
        f'</tr>'
        f'</thead>'
        f'<tbody>'
        + "".join(rows_html) +
        f'</tbody>'
        f'</table>'
    )


def _html_row(
    event: SignalEvent,
    enrichment: TickerEnrichment | None,
    tz,
) -> str:
    """One <tr> for one ticker × signal."""
    # Stock cell
    stock = f'<b>{html.escape(event.ticker.symbol)}</b>'

    # Mkt Cap
    if enrichment is not None and enrichment.market_cap_rupees is not None:
        mcap = _format_indian_crores(enrichment.market_cap_rupees)
    else:
        mcap = '—'

    # Sector
    sector_text = '—'
    if enrichment is not None:
        # Show industry primarily (more specific than sector), matching
        # the image. Fall back to sector if industry is missing.
        sector_text = enrichment.industry or enrichment.sector or '—'

    # Sector Zone — colored ● + label
    zone = enrichment.sector_zone if enrichment is not None else 0
    zone_html = _zone_cell(zone)

    # Sector Direction — colored arrow
    direction = enrichment.sector_direction if enrichment is not None else 0
    direction_html = _direction_cell(direction)

    # Last 5 signals — vertical list inside the cell
    recent = enrichment.recent_signals if enrichment is not None else ()
    last5_html = _recent_signals_cell(recent, tz)

    return (
        f'<tr style="border-bottom: 1px solid {_COLOR_BORDER}; '
        f'vertical-align: top;">'
        f'<td style="padding: 8px 10px;">{stock}</td>'
        f'<td style="padding: 8px 10px; text-align: right; '
        f'white-space: nowrap;">{html.escape(mcap)}</td>'
        f'<td style="padding: 8px 10px;">{html.escape(sector_text)}</td>'
        f'<td style="padding: 8px 10px; text-align: center; '
        f'white-space: nowrap;">{zone_html}</td>'
        f'<td style="padding: 8px 10px; text-align: center;">{direction_html}</td>'
        f'<td style="padding: 8px 10px;">{last5_html}</td>'
        f'</tr>'
    )


def _zone_cell(zone: int) -> str:
    """Colored bullet + label for the Sector Zone column."""
    if zone > 0:
        return (
            f'<span style="color: {_COLOR_BULL}; font-size: 14px;">●</span> '
            f'<span style="color: {_COLOR_BULL}; font-weight: bold;">GREEN</span>'
        )
    if zone < 0:
        return (
            f'<span style="color: {_COLOR_BEAR}; font-size: 14px;">●</span> '
            f'<span style="color: {_COLOR_BEAR}; font-weight: bold;">RED</span>'
        )
    return (
        f'<span style="color: {_COLOR_NEUT}; font-size: 14px;">●</span> '
        f'<span style="color: {_COLOR_NEUT};">NEUTRAL</span>'
    )


def _direction_cell(direction: int) -> str:
    """Colored arrow for the Sector Direction column."""
    if direction > 0:
        return (
            f'<span style="color: {_COLOR_BULL}; font-size: 16px; '
            f'font-weight: bold;">▲</span>'
        )
    if direction < 0:
        return (
            f'<span style="color: {_COLOR_BEAR}; font-size: 16px; '
            f'font-weight: bold;">▼</span>'
        )
    return (
        f'<span style="color: {_COLOR_NEUT}; font-size: 16px;">●</span>'
    )


def _recent_signals_cell(
    recent: tuple[HistoricalSignal, ...], tz
) -> str:
    """Render the last-5-signals cell as one signal per line.

    Each line: 'DD-Mon-YY HH:MM SIGNAL_TYPE at PRICE'. Signal-type word
    is color-coded (BUY/SELL/EXIT) for fast scanning.
    """
    if not recent:
        return f'<span style="color: {_COLOR_MUTED};">—</span>'
    lines: list[str] = []
    for sig in recent:
        when = sig.computed_at.astimezone(tz) if tz else sig.computed_at
        when_str = when.strftime("%d-%b-%y %H:%M")
        verb_html = _signal_word_html(sig.signal_type)
        price_str = f"{sig.price_at_signal:,.2f}"
        lines.append(
            f'{html.escape(when_str)} {verb_html} at '
            f'{html.escape(price_str)}'
        )
    return "<br>".join(lines)


def _signal_word_html(signal_type: str) -> str:
    """Color-coded signal verb for the Last-5 cell.

    BUY → green, SELL → red, EXIT_FROM_BUY / EXIT_FROM_SELL → grey.
    """
    if signal_type == "BUY":
        return (
            f'<span style="color: {_COLOR_BULL}; font-weight: bold;">'
            f'BUY</span>'
        )
    if signal_type == "SELL":
        return (
            f'<span style="color: {_COLOR_BEAR}; font-weight: bold;">'
            f'SELL</span>'
        )
    # EXIT_FROM_BUY / EXIT_FROM_SELL share the muted color
    return (
        f'<span style="color: {_COLOR_MUTED}; font-weight: bold;">'
        f'{html.escape(signal_type)}</span>'
    )


def _format_indian_crores(raw_rupees: float) -> str:
    """Format a raw rupee amount as Indian crores with lakh-style commas.

    Examples (rounded to nearest crore):
        198430200000000  → '₹1,98,43,02,000 Cr' (wrong: too many digits)
        19843020000000   → '₹19,84,302 Cr'  (RELIANCE-ish)
        500000000000     → '₹50,000 Cr'
        15200000         → '₹2 Cr'   (1.52 Cr rounds to 2)
        500000           → '₹0 Cr'   (sub-crore)

    The conversion is `raw_rupees / 1e7 → crores`, then formatted with
    Indian grouping (last 3 digits, then groups of 2).
    """
    if raw_rupees is None:
        return '—'
    crores = raw_rupees / 10_000_000.0
    n = int(round(crores))
    if n < 0:
        return f"-{_format_indian_crores(-raw_rupees)}"
    s = str(n)
    if len(s) <= 3:
        return f"₹{s} Cr"
    last3 = s[-3:]
    rest = s[:-3]
    chunks: list[str] = []
    while len(rest) > 2:
        chunks.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        chunks.insert(0, rest)
    return f"₹{','.join(chunks)},{last3} Cr"