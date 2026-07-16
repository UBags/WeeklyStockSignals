"""V1 email composer: read `current_signals`, enrich, render.

Inputs: a `SubscriberRecord`, a `cycle_id` string, and a `now`
timestamp. Output: an `EmailBody` with subject, HTML, plain text,
and the signal count — or None if `current_signals` is empty.

V1 sends the SAME body to every subscriber. The `subscriber`
parameter is in the signature so P2-A can hook subscriber-specific
filtering here without touching the call sites in
`notify_subscribers` (Step 9).

Rendering uses inline f-strings rather than Jinja2 — Jinja2 isn't a
runtime dependency in this codebase, and the existing
`notifications/batcher.py` uses the same inline style.

Pipeline:
  1. SELECT current_signals JOIN tickers JOIN instrument_industry
  2. fetch_rank_scores(ticker_ids) for ranking
     (0.5 * stock_rs_26w + 0.3 * industry_rs_26w + 0.2 * stock_r2_26w;
      NULLs treated as 0.0; reused from `benchmark_rerank.py`)
  3. Sort by (rank_score DESC, ticker_id ASC) for deterministic order
  4. Group by industry alphabetically (Unknown last)
  5. Render HTML (table per industry) + plain text
  6. Subject: "[trip_ema] {cycle_id} — {N} signal(s) ({n_buys} BUY,
     {n_sells} SELL, {n_exits} EXIT)" where EXIT combines both
     EXIT_FROM_BUY and EXIT_FROM_SELL.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


from .leadlag_render import render_leadlag_html, render_leadlag_text

# INTRADAY-CHAIN: the chain is keyed on the trading day in exchange-local
# time. current_signals.bar_timestamp is the week anchor, so it cannot
# supply the day.


@dataclass(frozen=True, slots=True)
class EnrichedSignalRow:
    """One current_signals row + join columns for rendering."""
    ticker_id: int
    symbol: str
    exchange: str
    industry: str | None
    signal_type: str
    regime_before: int
    regime_after: int
    price_at_signal: float
    bar_timestamp: datetime
    computed_at: datetime
    rank_score: float
    # INTRADAY-CHAIN: ordered signal_types this ticker fired TODAY, e.g.
    # ["BUY", "EXIT_FROM_BUY", "BUY"]. Defaulted: other constructors of this
    # row (tests, the per-subscriber report) do not supply it.
    intraday_chain: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EmailBody:
    """Rendered email body. `signal_count` lets callers report
    cycle-level metrics without re-parsing the body."""
    subject: str
    html: str
    plain_text: str
    signal_count: int


# ====================================================================
# Style constants (consistent with notifications/batcher.py)
# ====================================================================

_COLOR_TEXT = "#222"
_COLOR_MUTED = "#888"
_COLOR_BUY = "#0a6e2c"      # green
_COLOR_SELL = "#a01919"     # red
_COLOR_NEUTRAL = "#666"
_COLOR_HEADER = "#1f1f1f"

# Signal type → display label
_TYPE_LABELS = {
    "BUY": "BUY",
    "SELL": "SELL",
    "EXIT_FROM_BUY": "EXIT (Buy)",
    "EXIT_FROM_SELL": "EXIT (Sell)",
}


# ====================================================================
# Public entry point
# ====================================================================

def build_email_body(
    rows: list[EnrichedSignalRow],
    cycle_id: str,
    now: datetime,
    *,
    leadlag_fn=None,
) -> EmailBody:
    """Render a pre-fetched, pre-filtered row list into an EmailBody.

    Renderer for the per-subscriber signal-report notifier (filtered
    rows). Assumes `rows` is non-empty — callers handle the empty case.

    leadlag_fn: optional (EnrichedSignalRow -> LeadLagContext | None). When
    supplied, each signal gets a lead/lag clause appended under it; when None
    (the default) the body renders exactly as before. Injected as a callable so
    build_email_body stays DB-free and unit-testable.
    """
    counts = _count_signal_kinds(rows)
    subject = _make_subject(cycle_id, len(rows), counts)
    clauses = _leadlag_clauses(rows, leadlag_fn)
    html = _render_html(rows, cycle_id, now, clauses)
    plain_text = _render_plain_text(rows, cycle_id, now, clauses)
    return EmailBody(
        subject=subject,
        html=html,
        plain_text=plain_text,
        signal_count=len(rows),
    )


def _leadlag_clauses(rows, leadlag_fn):
    """{ticker_id: (html_clause, text_clause)} per row, or {} when no reader is
    injected. The reader is called once per row; a missing/thin context yields
    empty strings (no clause), never a fabricated one."""
    if leadlag_fn is None:
        return {}
    out: dict = {}
    for r in rows:
        ctx = leadlag_fn(r)
        out[r.ticker_id] = (
            render_leadlag_html(r, ctx),
            render_leadlag_text(r, ctx),
        )
    return out


# ====================================================================
# Internals
# ====================================================================

def _count_signal_kinds(
    rows: list[EnrichedSignalRow],
) -> dict[str, int]:
    """Counts BUY, SELL, EXIT (combined from EXIT_FROM_BUY +
    EXIT_FROM_SELL)."""
    counts = {"BUY": 0, "SELL": 0, "EXIT": 0}
    for r in rows:
        if r.signal_type == "BUY":
            counts["BUY"] += 1
        elif r.signal_type == "SELL":
            counts["SELL"] += 1
        elif r.signal_type in ("EXIT_FROM_BUY", "EXIT_FROM_SELL"):
            counts["EXIT"] += 1
    return counts


def _make_subject(
    cycle_id: str, total: int, counts: dict[str, int],
) -> str:
    plural = "s" if total != 1 else ""
    return (
        f"[trip_ema] {cycle_id} — {total} signal{plural} "
        f"({counts['BUY']} BUY, {counts['SELL']} SELL, "
        f"{counts['EXIT']} EXIT)"
    )


# ---------------- HTML rendering ----------------

def _render_html(
    rows: list[EnrichedSignalRow],
    cycle_id: str,
    now: datetime,
    clauses: dict | None = None,
) -> str:
    by_industry = _group_by_industry(rows)
    industry_order = _industry_render_order(by_industry)

    parts: list[str] = [
        f'<div style="font-family: Arial, Helvetica, sans-serif; '
        f'color: {_COLOR_TEXT}; max-width: 1100px;">',
        f'<p style="font-size: 14px; margin: 0 0 16px 0;">'
        f'Cycle <strong>{_html_escape(cycle_id)}</strong> — '
        f'{len(rows)} signal{"s" if len(rows) != 1 else ""} '
        f'across {len(by_industry)} '
        f'industr{"ies" if len(by_industry) != 1 else "y"}.'
        f'</p>',
    ]

    for industry in industry_order:
        parts.append(
            _render_industry_section(
                industry, by_industry[industry], clauses,
            ),
        )

    parts.append(_render_html_footer(now))
    parts.append('</div>')
    return "\n".join(parts)


def _render_industry_section(
    industry: str,
    rows: list[EnrichedSignalRow],
    clauses: dict | None = None,
) -> str:
    out = [
        f'<h3 style="margin: 18px 0 6px 0; color: {_COLOR_HEADER}; '
        f'font-size: 14px;">{_html_escape(industry)} '
        f'<span style="font-weight: normal; color: {_COLOR_MUTED};">'
        f'({len(rows)})</span></h3>',
        '<table style="border-collapse: collapse; width: 100%; '
        'font-size: 13px;">',
        '<thead><tr style="background: #f0f0f0;">'
        '<th style="text-align: left;  padding: 4px 8px;">Signal</th>'
        '<th style="text-align: left;  padding: 4px 8px;">Ticker</th>'
        '<th style="text-align: right; padding: 4px 8px;">Price</th>'
        '<th style="text-align: right; padding: 4px 8px;">Rank</th>'
        '</tr></thead>',
        '<tbody>',
    ]
    for r in rows:
        out.append(_render_row_html(r))
        clause = (clauses or {}).get(r.ticker_id, ("", ""))[0]
        if clause:
            out.append(
                f'<tr><td colspan="4" '
                f'style="padding: 0 8px 10px 8px;">{clause}</td></tr>'
            )
    out.append('</tbody></table>')
    return "\n".join(out)


def _render_chain_html(r: EnrichedSignalRow) -> str:
    """INTRADAY-CHAIN: " (BUY - EXIT_FROM_BUY - BUY)".

    Empty when the ticker fired only once today (chain length <= 1) — the row's
    own signal_type already says it, so repeating it adds nothing.
    """
    chain = getattr(r, "intraday_chain", None) or []
    if len(chain) <= 1:
        return ""
    seq = " - ".join(chain)
    return (
        f' <span style="color: {_COLOR_MUTED};">'
        f'({_html_escape(seq)})</span>'
    )


def _render_chain_text(r: EnrichedSignalRow) -> str:
    """INTRADAY-CHAIN, plain-text form."""
    chain = getattr(r, "intraday_chain", None) or []
    if len(chain) <= 1:
        return ""
    return " (" + " - ".join(chain) + ")"


def _render_row_html(r: EnrichedSignalRow) -> str:
    color = (
        _COLOR_BUY if r.signal_type == "BUY"
        else _COLOR_SELL if r.signal_type == "SELL"
        else _COLOR_NEUTRAL
    )
    label = _TYPE_LABELS.get(r.signal_type, r.signal_type)
    return (
        f'<tr style="border-bottom: 1px solid #eee;">'
        f'<td style="padding: 4px 8px; color: {color}; '
        f'font-weight: bold;">{_html_escape(label)}</td>'
        f'<td style="padding: 4px 8px;">'
        f'{_html_escape(r.symbol)} '
        f'<span style="color: {_COLOR_MUTED};">'
        f'({_html_escape(r.exchange)})</span>'
        f'{_render_chain_html(r)}</td>'
        f'<td style="padding: 4px 8px; text-align: right;">'
        f'{r.price_at_signal:.2f}</td>'
        f'<td style="padding: 4px 8px; text-align: right;">'
        f'{r.rank_score:.2f}</td>'
        f'</tr>'
    )


def _render_html_footer(now: datetime) -> str:
    stamp = now.strftime("%Y-%m-%d %H:%M %Z").strip()
    return (
        f'<p style="font-size: 11px; color: {_COLOR_MUTED}; '
        f'margin: 24px 0 0 0;">'
        f'Generated at {_html_escape(stamp)}. '
        f'Rank = 0.5·stock_RS_26w + 0.3·industry_RS_26w + 0.2·R².'
        f'</p>'
    )


# ---------------- Plain-text rendering ----------------

def _render_plain_text(
    rows: list[EnrichedSignalRow],
    cycle_id: str,
    now: datetime,
    clauses: dict | None = None,
) -> str:
    by_industry = _group_by_industry(rows)
    industry_order = _industry_render_order(by_industry)

    lines: list[str] = [
        f"Cycle {cycle_id} — {len(rows)} signal"
        f"{'s' if len(rows) != 1 else ''} across "
        f"{len(by_industry)} industr"
        f"{'ies' if len(by_industry) != 1 else 'y'}.",
        "",
    ]
    for industry in industry_order:
        ind_rows = by_industry[industry]
        lines.append(f"=== {industry} ({len(ind_rows)}) ===")
        for r in ind_rows:
            label = _TYPE_LABELS.get(r.signal_type, r.signal_type)
            lines.append(
                f"  [{label:>11}] {r.symbol} ({r.exchange})"
                f"{_render_chain_text(r)}  "
                f"price={r.price_at_signal:.2f}  "
                f"rank={r.rank_score:.2f}"
            )
            clause = (clauses or {}).get(r.ticker_id, ("", ""))[1]
            if clause:
                lines.append(clause)
        lines.append("")

    stamp = now.strftime("%Y-%m-%d %H:%M %Z").strip()
    lines.append(
        f"Generated at {stamp}. "
        f"Rank = 0.5*stock_RS + 0.3*industry_RS + 0.2*R^2."
    )
    return "\n".join(lines)


# ---------------- Shared helpers ----------------

def _group_by_industry(
    rows: list[EnrichedSignalRow],
) -> dict[str, list[EnrichedSignalRow]]:
    """Group preserving rank order within each industry. None / empty
    industries are bucketed under '(Unknown)'."""
    out: dict[str, list[EnrichedSignalRow]] = {}
    for r in rows:
        key = r.industry if r.industry else "(Unknown)"
        out.setdefault(key, []).append(r)
    return out


def _industry_render_order(
    by_industry: dict[str, list[EnrichedSignalRow]],
) -> list[str]:
    """Alphabetical, with '(Unknown)' bucket last."""
    return sorted(
        by_industry.keys(),
        key=lambda s: (s == "(Unknown)", s),
    )


def _html_escape(s: str) -> str:
    """Minimal HTML escape — tickers/industry names are mostly ASCII
    but defensive against any user-controlled content."""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )
