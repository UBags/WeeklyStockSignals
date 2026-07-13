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

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..persistence.subscribers_repo import SubscriberRecord
from .benchmark_rerank import fetch_rank_scores
from ..domain.market_session import exchange_of_conn, market_tz

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
) -> EmailBody:
    """Render a pre-fetched, pre-filtered row list into an EmailBody.

    Shared renderer: compose_email (current cycle, all signals) and the
    per-subscriber signal-report notifier (filtered rows) both call
    this. Assumes `rows` is non-empty — callers handle the empty case.
    """
    counts = _count_signal_kinds(rows)
    subject = _make_subject(cycle_id, len(rows), counts)
    html = _render_html(rows, cycle_id, now)
    plain_text = _render_plain_text(rows, cycle_id, now)
    return EmailBody(
        subject=subject,
        html=html,
        plain_text=plain_text,
        signal_count=len(rows),
    )


def compose_email(
    conn: Connection,
    subscriber: SubscriberRecord,
    cycle_id: str,
    now: datetime,
) -> EmailBody | None:
    """Compose the V1 cycle email body.

    Returns None when current_signals is empty — no email to send.

    Args:
        conn: SQLAlchemy Connection. Caller controls the transaction.
        subscriber: target subscriber. Present for P2-A subscriber-
            specific filtering; NOT used in V1.
        cycle_id: identifier for this cycle (e.g. "20260626-1500").
            Appears in the subject + body for operator reference.
        now: instant used in the footer's "generated at" line.

    Returns:
        EmailBody | None.
    """
    # CHAIN-CLOCK-FIX: pass the cycle's instant down. The chain lookup must
    # use the same day the transitions were LOGGED under
    # (log_intraday_transitions writes signal_date from the cycle's as_of), not
    # the wall clock at render time — otherwise an EoD cycle that straddles
    # midnight silently loses every chain.
    rows = _fetch_enriched_signals(conn, now=now)
    if not rows:
        return None

    return build_email_body(rows, cycle_id, now)


# ====================================================================
# Internals
# ====================================================================

def _fetch_enriched_signals(
    conn: Connection,
    *,
    now: datetime,
) -> list[EnrichedSignalRow]:
    """SELECT current_signals JOIN tickers + instrument_industry,
    then attach rank_scores via fetch_rank_scores. Returns rows
    sorted by (rank_score DESC, ticker_id ASC)."""
    raw_rows = conn.execute(text("""
        SELECT cs.ticker_id,
               cs.timeframe,
               cs.bar_timestamp,
               cs.signal_type,
               cs.regime_before,
               cs.regime_after,
               cs.price_at_signal,
               cs.computed_at,
               t.exchange,
               t.symbol,
               ii.industry
          FROM current_signals cs
          JOIN tickers t ON t.ticker_id = cs.ticker_id
     LEFT JOIN instrument_industry ii ON ii.ticker_id = cs.ticker_id
         ORDER BY cs.ticker_id
    """)).all()

    if not raw_rows:
        return []

    ticker_ids = [int(r.ticker_id) for r in raw_rows]
    rank_scores = fetch_rank_scores(conn, ticker_ids)

    # INTRADAY-CHAIN: the within-day sequence for each emailed ticker.
    # current_signals holds only the ticker's LATEST signal; the chain shows
    # how it got there (e.g. BUY - EXIT_FROM_BUY - BUY). Keyed on today in the
    # session's own timezone: the bar_timestamp on current_signals is the week
    # anchor, so it cannot supply the day.
    from ..persistence.intraday_signal_log_repo import (
        IntradaySignalLogRepository,
    )
    # CHAIN-CLOCK-FIX: the day comes from the CYCLE's instant, never from
    # datetime.now(). log_intraday_transitions stamps signal_date from the
    # cycle's as_of; using the wall clock here meant two different clocks
    # decided the same day. An EoD cycle running at 23:5x writes
    # signal_date = D, and if the email rendered minutes later — past midnight
    # — the wall clock said D+1 and EVERY chain silently disappeared. It also
    # made the feature untestable: tests pass an explicit `now=`, which was
    # being ignored here.
    # THE SAME CLOCK that wrote signal_date. recompute_market stamps it with
    # market_tz(exchange); resolving the day here with a different zone is
    # precisely the two-clocks bug the comment above describes — and for any
    # exchange outside India the two WOULD disagree, and every chain would
    # silently disappear.
    _today = now.astimezone(market_tz(exchange_of_conn(conn))).date()
    chains = IntradaySignalLogRepository(conn).chains_for_date(
        signal_date=_today, ticker_ids=ticker_ids,
    )

    enriched = [
        EnrichedSignalRow(
            ticker_id=int(r.ticker_id),
            symbol=r.symbol,
            exchange=r.exchange,
            industry=r.industry,
            signal_type=r.signal_type,
            regime_before=int(r.regime_before),
            regime_after=int(r.regime_after),
            price_at_signal=float(r.price_at_signal),
            bar_timestamp=r.bar_timestamp,
            computed_at=r.computed_at,
            rank_score=rank_scores.get(int(r.ticker_id), 0.0),
            # INTRADAY-CHAIN: the ordered signals this ticker fired today.
            intraday_chain=chains.get(int(r.ticker_id), []),
        )
        for r in raw_rows
    ]

    # Sort by rank DESC, then ticker_id ASC for deterministic ties.
    enriched.sort(key=lambda s: (-s.rank_score, s.ticker_id))
    return enriched


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
            _render_industry_section(industry, by_industry[industry]),
        )

    parts.append(_render_html_footer(now))
    parts.append('</div>')
    return "\n".join(parts)


def _render_industry_section(
    industry: str, rows: list[EnrichedSignalRow],
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
