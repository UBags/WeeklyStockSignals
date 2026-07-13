"""Per-subscriber signal-report notifier — a single-send primitive.

Given ONE recipient plus that recipient's subscription parameters,
this fetches the `current_signals` produced by the current run, applies
the recipient's filters, renders via the shared `build_email_body`, and
sends via Brevo (`send_email`). It sends at most one email.

A future layer will iterate all subscribers + their subscription rows
and call `send_signal_report` once per subscriber with that
subscriber's filters. This module does NOT iterate subscribers, does
NOT dedup across cycles, and (for now) does NOT write notification_log
— that bookkeeping belongs to the future iteration layer, which owns
real subscriber_ids.

Parameter semantics (locked with the operator):
  - which_signals: SignalKind bitmask (BUY=1, SELL=2, EXIT_FROM_BUY=4,
    EXIT_FROM_SELL=8; ALL=15). Row kept if `kind & which_signals`.
  - which_exchanges / which_industries / which_stocks: each is ALL
    (no restriction), a list/sequence (restrict to those), or None.
    Combined via AND (intersection). None on ANY dimension => the
    subscriber is not subscribed to that dimension => no mail.
    Default ALL for now (operator will switch defaults to None later).
  - when: ALWAYS (send every run) or EOD_ONLY (send only on the EoD
    run — requires is_eod_run from the caller's run context).
  - from_date / to_date: the recipient's PAID subscription window.
    Mail is sent only if *today* (IST) falls within [from_date,
    to_date]. Defaults span 1970..2099 (effectively always active).
    These are NOT a signal-date filter.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, UTC
from enum import IntFlag
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..persistence.subscribers_repo import SubscriberRecord
from .benchmark_rerank import fetch_rank_scores
from .compose_email import EnrichedSignalRow, build_email_body
from .send_email import SendResult, send_email
from ..domain.market_session import exchange_of_engine, market_tz


# Sentinel for "no restriction on this dimension".
ALL = "ALL"

_DEFAULT_FROM = date(1970, 1, 1)
_DEFAULT_TO = date(2099, 12, 31)


class SignalKind(IntFlag):
    """OR-able signal-type flags. ALL = every kind."""
    BUY = 1
    SELL = 2
    EXIT_FROM_BUY = 4
    EXIT_FROM_SELL = 8
    ALL = BUY | SELL | EXIT_FROM_BUY | EXIT_FROM_SELL  # 15


class When:
    ALWAYS = "ALWAYS"
    EOD_ONLY = "EOD_ONLY"


_KIND_BY_NAME: dict[str, SignalKind] = {
    "BUY": SignalKind.BUY,
    "SELL": SignalKind.SELL,
    "EXIT_FROM_BUY": SignalKind.EXIT_FROM_BUY,
    "EXIT_FROM_SELL": SignalKind.EXIT_FROM_SELL,
}


def send_signal_report(
    db_engine: Engine,
    *,
    notification_mailid: str,
    brevo_api_key: str,
    brevo_from_address: str,
    brevo_from_name: str = "WeeklyStockSignals",
    which_signals: SignalKind = SignalKind.ALL,
    which_exchanges: str | Sequence[str] | None = ALL,
    which_industries: str | Sequence[str] | None = ALL,
    which_stocks: str | Sequence[int] | None = ALL,
    when: str = When.ALWAYS,
    from_date: date = _DEFAULT_FROM,
    to_date: date = _DEFAULT_TO,
    is_eod_run: bool = False,
    now: datetime | None = None,
    cycle_id: str | None = None,
    subscriber_id: int = 0,
) -> SendResult | None:
    """Send one filtered signal report to `notification_mailid`.

    Returns None when the send is gated out (outside the subscription
    window; EOD_ONLY on a non-EoD run; a None scope dimension; or no
    signals survive filtering). Otherwise returns send_email's
    SendResult.
    """
    now = now or datetime.now(UTC)
    # The MARKET's today. This gates the paid-subscription window below
    # (from_date <= today <= to_date) — on the wrong clock a subscriber is
    # entitled on the wrong day.
    today_market = now.astimezone(market_tz(exchange_of_engine(db_engine))).date()

    # --- Entitlement gate: today within the paid subscription window ---
    if not (from_date <= today_market <= to_date):
        return None

    # --- EOD gate ---
    if when == When.EOD_ONLY and not is_eod_run:
        return None

    # --- None on any scope dimension => not subscribed => no mail ---
    if which_exchanges is None or which_industries is None \
            or which_stocks is None:
        return None

    rows = _fetch_filtered_current_signals(
        db_engine,
        which_signals=which_signals,
        which_exchanges=which_exchanges,
        which_industries=which_industries,
        which_stocks=which_stocks,
        # CHAIN-CLOCK-FIX: today_market is already computed above for the
        # entitlement gate — reuse it rather than reaching for a second clock.
        as_of_date=today_market,
    )
    if not rows:
        return None

    cycle_id = cycle_id or now.strftime("%Y%m%d-%H%M%S")
    body = build_email_body(rows, cycle_id, now)

    subscriber = SubscriberRecord(
        subscriber_id=subscriber_id,
        transport="email",
        address=notification_mailid,
        notify_cadence_minutes=0,
        last_notified_at=None,
        is_active=True,
    )
    return send_email(
        subscriber, body,
        api_key=brevo_api_key,
        from_address=brevo_from_address,
        from_name=brevo_from_name,
        now=now,
    )


def _fetch_filtered_current_signals(
    db_engine: Engine,
    *,
    which_signals: SignalKind,
    which_exchanges: str | Sequence[str],
    which_industries: str | Sequence[str],
    which_stocks: str | Sequence[int],
    as_of_date: date,
) -> list[EnrichedSignalRow]:
    """current_signals JOIN tickers LEFT JOIN instrument_industry, with
    the recipient's scope + signal-type filters applied. Mirrors
    compose_email._fetch_enriched_signals (rank_scores + identical sort)
    but parameterized. Empty result => caller sends nothing.
    """
    # Signal-type mask -> explicit name list.
    kept_types = [
        name for name, kind in _KIND_BY_NAME.items()
        if which_signals & kind
    ]
    if not kept_types:
        return []

    clauses: list[str] = []
    params: dict = {}

    if which_exchanges != ALL:
        clauses.append("t.exchange = ANY(:exchanges)")
        params["exchanges"] = list(which_exchanges)
    if which_industries != ALL:
        clauses.append("ii.industry = ANY(:industries)")
        params["industries"] = list(which_industries)
    if which_stocks != ALL:
        clauses.append("cs.ticker_id = ANY(:stock_ids)")
        params["stock_ids"] = [int(s) for s in which_stocks]
    if which_signals != SignalKind.ALL:
        clauses.append("cs.signal_type = ANY(:types)")
        params["types"] = kept_types

    where = ("WHERE " + "\n           AND ".join(clauses)) if clauses else ""
    sql = f"""
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
        {where}
         ORDER BY cs.ticker_id
    """

    with db_engine.connect() as conn:
        raw_rows = conn.execute(text(sql), params).all()
        if not raw_rows:
            return []
        ticker_ids = [int(r.ticker_id) for r in raw_rows]
        rank_scores = fetch_rank_scores(conn, ticker_ids)

        # CHAIN-CLOCK-FIX: this fetch never happened, so intraday_chain
        # defaulted to [] on every row and the renderer — which suppresses an
        # empty chain by design — silently shipped every per-subscriber email
        # WITHOUT the within-day sequence. Indistinguishable from "no chain".
        #
        # Must run INSIDE this `with` block: the comprehension below sits
        # outside it, by which point `conn` is closed.
        from ..persistence.intraday_signal_log_repo import (
            IntradaySignalLogRepository,
        )
        _chains = IntradaySignalLogRepository(conn).chains_for_date(
            signal_date=as_of_date, ticker_ids=ticker_ids,
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
            intraday_chain=_chains.get(int(r.ticker_id), []),
        )
        for r in raw_rows
    ]
    enriched.sort(key=lambda s: (-s.rank_score, s.ticker_id))
    return enriched
