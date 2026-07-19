"""Derived-chain fidelity audit — the second half of the signal chain.

data_fidelity.audit_exchange audits the INPUTS to the compute layer:

    tickers -> instrument_industry / instrument_shares / ohlcv_bars
            -> daily_market_cap

and stops. Its own diagram draws the chain all the way to `signals -> email`,
but `READY: yes` has only ever certified that the raw materials are present. A
stage on the DERIVED side could run, write zero rows for a whole cohort, and
still leave READY green — which is exactly the shape of the benchmark_stats bug
(both tables empty, every emailed signal ranked 0.0, nothing surfaced it).

This module closes that gap. It audits every derived link:

    stock_indicator_state / _enrichment            (EMAs, regime, per-bar features)
    industry_index_value                           (the index itself)
    industry_indicator_state / _enrichment         (industry EMAs/regime)
    historical_signals / current_signals           (the signals)
    signal_enrichment / _labels / _ensemble_features(per-signal X / y)
    stock_benchmark_stats / industry_benchmark_stats(rank_score, email ordering)

Design principle — WALL-CLOCK INDEPENDENCE. A legitimately historical backfill
(date_to = 2020) computes a frontier at 2020 and must pass. So staleness is
never measured against `today`; it is measured against the UPSTREAM frontier of
the same run. Each derived stage's frontier is checked against the frontier of
the thing it derives from:

    stock_indicator_state.date   vs  ohlcv frontier
    industry_index_value.trade_date vs stock_indicator_state frontier
    industry_indicator_state.date   vs industry_index_value frontier
    historical_signals.signal_date  vs stock_indicator_state frontier

Two obligation shapes:

  * STRICT (1:1): every entity that SHOULD have a derived row must have one.
    A miss is a real defect — a stage skipped a cohort. Used for state,
    enrichment, index values, signal_enrichment, ensemble.
  * LENIENT (non-empty): coverage < 100% is expected by construction, so only
    an EMPTY table is a defect. Used for benchmark_stats (NIFTYBEES/self-vs-self
    and sub-threshold industries are legitimately skipped) and signal_labels
    (the newest signals have not matured their forward horizon yet).

Writes nothing. Returns a list[DatasetFidelity] that audit_exchange extends onto
its own list, so the existing FidelityReport.render() and .ready pick these up
with no other change — every derived dataset that is not OK turns READY red and
the CLI exits non-zero.
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import text

# Import from the sibling module. audit_exchange imports THIS module lazily
# (function-local), so there is no import cycle at module-load time.
from .data_fidelity import (
    DatasetFidelity,
    _scalar,
    _status_for_coverage,
    _MIN_BARS_FOR_SIGNAL,
    _DEFAULT_GAP_DAYS,
)
from ..domain.market_session import market_tz_name

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _names(conn, sql: str, limit: int = 8, **params) -> list[str]:
    """First `limit` names from a diagnostic query, for the detail line."""
    rows = conn.execute(text(sql + f" LIMIT {int(limit)}"), params).all()
    return [str(r[0]) for r in rows]


def _name_detail(missing: int, names: list[str], noun: str) -> str:
    """'12 X without Y (ABC, DEF, GHI, …)' — actionable, bounded."""
    if missing <= 0:
        return ""
    shown = ", ".join(names)
    more = "" if missing <= len(names) else f", … (+{missing - len(names)} more)"
    return f"{missing} {noun} ({shown}{more})"


def _lenient(covered: int, universe: int) -> str:
    """Non-empty obligation: MISSING only when the table is empty for a
    universe that should have rows. A partial fill is fine (edge skips are
    expected for the datasets this is used on)."""
    if universe == 0:
        return "OK"          # nothing was owed
    if covered == 0:
        return "MISSING"     # owed, produced nothing — the bug we guard
    return "OK"


def _frontier_gap_days(upstream: date | None, derived: date | None) -> int | None:
    """How many days the derived frontier lags the upstream one. None if
    either side is empty (a separate, upstream, check owns that)."""
    if upstream is None or derived is None:
        return None
    return (upstream - derived).days


# --------------------------------------------------------------------------
# the audit
# --------------------------------------------------------------------------

def audit_derived_chain(
    conn,
    exchange: str,
    *,
    gap_days: int = _DEFAULT_GAP_DAYS,
) -> list[DatasetFidelity]:
    """Audit every derived link for `exchange`. Read-only.

    The industry layer has no exchange column (audit_exchange's multi-exchange
    safety check guards that), so on a single-exchange database every ACTIVE
    industry index belongs to `exchange` and the industry-side queries need no
    exchange predicate.
    """
    out: list[DatasetFidelity] = []
    tz = market_tz_name(exchange)

    # ---- frontiers: the run's own clocks, never wall-clock `today` ----
    ohlcv_frontier = conn.execute(
        text(
            "SELECT MAX((o.bar_timestamp AT TIME ZONE :tz)::date) "
            "  FROM ohlcv_bars o "
            "  JOIN tickers t ON t.ticker_id = o.ticker_id "
            " WHERE t.exchange = :ex AND o.timeframe = '1D'"
        ),
        {"tz": tz, "ex": exchange},
    ).scalar()
    state_frontier = conn.execute(
        text(
            "SELECT MAX(s.date) FROM stock_indicator_state s "
            "  JOIN tickers t ON t.ticker_id = s.ticker_id "
            " WHERE t.exchange = :ex"
        ),
        {"ex": exchange},
    ).scalar()
    iiv_frontier = conn.execute(
        text("SELECT MAX(trade_date) FROM industry_index_value")
    ).scalar()

    # =====================================================================
    # 1. stock_indicator_state — EMAs, ATR, bands, regime (STRICT)
    #    universe = tickers with enough bars to be computable at all.
    # =====================================================================
    sig_universe = _scalar(
        conn,
        "SELECT count(*) FROM ("
        "  SELECT o.ticker_id FROM ohlcv_bars o "
        "    JOIN tickers t ON t.ticker_id = o.ticker_id "
        "   WHERE t.exchange = :ex AND o.timeframe = '1D' "
        "   GROUP BY o.ticker_id HAVING count(*) >= :mb"
        ") q",
        ex=exchange, mb=_MIN_BARS_FOR_SIGNAL,
    )
    state_tickers = _scalar(
        conn,
        "SELECT count(DISTINCT s.ticker_id) FROM stock_indicator_state s "
        "  JOIN tickers t ON t.ticker_id = s.ticker_id "
        " WHERE t.exchange = :ex",
        ex=exchange,
    )
    n_state_rows = _scalar(
        conn,
        "SELECT count(*) FROM stock_indicator_state s "
        "  JOIN tickers t ON t.ticker_id = s.ticker_id WHERE t.exchange = :ex",
        ex=exchange,
    )
    missing = max(0, sig_universe - state_tickers)
    detail = ""
    if missing:
        names = _names(
            conn,
            "SELECT t.symbol FROM tickers t "
            "  JOIN ("
            "    SELECT o.ticker_id FROM ohlcv_bars o WHERE o.timeframe = '1D' "
            "     GROUP BY o.ticker_id HAVING count(*) >= :mb"
            "  ) q ON q.ticker_id = t.ticker_id "
            " WHERE t.exchange = :ex "
            "   AND NOT EXISTS (SELECT 1 FROM stock_indicator_state s "
            "                    WHERE s.ticker_id = t.ticker_id) "
            " ORDER BY t.symbol",
            ex=exchange, mb=_MIN_BARS_FOR_SIGNAL,
        )
        detail = _name_detail(missing, names, "signalable tickers w/o state")
    gap = _frontier_gap_days(ohlcv_frontier, state_frontier)
    status = _status_for_coverage(state_tickers, sig_universe)
    if status == "OK" and gap is not None and gap > gap_days:
        status = "PARTIAL"
        detail = (
            f"state frontier {state_frontier} lags ohlcv {ohlcv_frontier} "
            f"by {gap}d"
        )
    out.append(DatasetFidelity(
        dataset="stock_indicator_state", status=status, rows=n_state_rows,
        covered=state_tickers, universe=sig_universe, detail=detail,
    ))

    # =====================================================================
    # 2. stock_indicator_enrichment — FK subset of state (STRICT, per row).
    #    Every state row is supposed to have exactly one enrichment row.
    # =====================================================================
    n_enrich_rows = _scalar(
        conn,
        "SELECT count(*) FROM stock_indicator_enrichment e "
        "  JOIN tickers t ON t.ticker_id = e.ticker_id WHERE t.exchange = :ex",
        ex=exchange,
    )
    detail = ""
    if n_state_rows and n_enrich_rows < n_state_rows:
        detail = (
            f"{n_state_rows - n_enrich_rows} state row(s) without enrichment"
        )
    out.append(DatasetFidelity(
        dataset="stock_indicator_enrichment",
        status=_status_for_coverage(n_enrich_rows, n_state_rows),
        rows=n_enrich_rows, covered=n_enrich_rows, universe=n_state_rows,
        detail=detail,
    ))

    # =====================================================================
    # 3. industry_index_value — the index itself (STRICT).
    #    universe = ACTIVE indices that HAVE live constituents. Those, and
    #    only those, owe values. An index with no constituents is a
    #    sub-threshold industry and owes nothing.
    #    This is the exact "built constituents but no values" defect that
    #    fed the empty-rs_ema seam.
    # =====================================================================
    built = _scalar(
        conn,
        "SELECT count(*) FROM industry_index i "
        " WHERE i.status = 'ACTIVE' "
        "   AND EXISTS (SELECT 1 FROM industry_index_constituent c "
        "                WHERE c.industry_name = i.industry_name "
        "                  AND c.effective_to IS NULL)",
    )
    valued = _scalar(
        conn,
        "SELECT count(*) FROM industry_index i "
        " WHERE i.status = 'ACTIVE' "
        "   AND EXISTS (SELECT 1 FROM industry_index_constituent c "
        "                WHERE c.industry_name = i.industry_name "
        "                  AND c.effective_to IS NULL) "
        "   AND EXISTS (SELECT 1 FROM industry_index_value v "
        "                WHERE v.industry_name = i.industry_name)",
    )
    n_iiv_rows = _scalar(conn, "SELECT count(*) FROM industry_index_value")
    missing = max(0, built - valued)
    detail = ""
    if missing:
        names = _names(
            conn,
            "SELECT i.industry_name FROM industry_index i "
            " WHERE i.status = 'ACTIVE' "
            "   AND EXISTS (SELECT 1 FROM industry_index_constituent c "
            "                WHERE c.industry_name = i.industry_name "
            "                  AND c.effective_to IS NULL) "
            "   AND NOT EXISTS (SELECT 1 FROM industry_index_value v "
            "                    WHERE v.industry_name = i.industry_name) "
            " ORDER BY i.industry_name",
        )
        detail = _name_detail(missing, names, "indices built but unvalued")
    status = _status_for_coverage(valued, built)
    gap = _frontier_gap_days(state_frontier, iiv_frontier)
    if status == "OK" and gap is not None and gap > gap_days:
        status = "PARTIAL"
        detail = (
            f"iiv frontier {iiv_frontier} lags stock-state {state_frontier} "
            f"by {gap}d"
        )
    out.append(DatasetFidelity(
        dataset="industry_index_value", status=status, rows=n_iiv_rows,
        covered=valued, universe=built, detail=detail,
    ))

    # =====================================================================
    # 4. industry_indicator_state — industry EMAs/regime (STRICT).
    #    universe = industries that HAVE index values.
    # =====================================================================
    iiv_industries = _scalar(
        conn, "SELECT count(DISTINCT industry_name) FROM industry_index_value",
    )
    istate_industries = _scalar(
        conn,
        "SELECT count(DISTINCT v.industry_name) FROM industry_index_value v "
        " WHERE EXISTS (SELECT 1 FROM industry_indicator_state s "
        "                WHERE s.industry_name = v.industry_name)",
    )
    n_istate_rows = _scalar(
        conn, "SELECT count(*) FROM industry_indicator_state",
    )
    missing = max(0, iiv_industries - istate_industries)
    detail = ""
    if missing:
        names = _names(
            conn,
            "SELECT DISTINCT v.industry_name FROM industry_index_value v "
            " WHERE NOT EXISTS (SELECT 1 FROM industry_indicator_state s "
            "                    WHERE s.industry_name = v.industry_name) "
            " ORDER BY v.industry_name",
        )
        detail = _name_detail(missing, names, "valued industries w/o state")
    istate_frontier = conn.execute(
        text("SELECT MAX(date) FROM industry_indicator_state")
    ).scalar()
    status = _status_for_coverage(istate_industries, iiv_industries)
    gap = _frontier_gap_days(iiv_frontier, istate_frontier)
    if status == "OK" and gap is not None and gap > gap_days:
        status = "PARTIAL"
        detail = (
            f"industry-state frontier {istate_frontier} lags iiv "
            f"{iiv_frontier} by {gap}d"
        )
    out.append(DatasetFidelity(
        dataset="industry_indicator_state", status=status, rows=n_istate_rows,
        covered=istate_industries, universe=iiv_industries, detail=detail,
    ))

    # =====================================================================
    # 5. industry_indicator_enrichment — FK subset of industry state (STRICT).
    # =====================================================================
    n_ienrich_rows = _scalar(
        conn, "SELECT count(*) FROM industry_indicator_enrichment",
    )
    detail = ""
    if n_istate_rows and n_ienrich_rows < n_istate_rows:
        detail = (
            f"{n_istate_rows - n_ienrich_rows} industry-state row(s) "
            f"without enrichment"
        )
    out.append(DatasetFidelity(
        dataset="industry_indicator_enrichment",
        status=_status_for_coverage(n_ienrich_rows, n_istate_rows),
        rows=n_ienrich_rows, covered=n_ienrich_rows, universe=n_istate_rows,
        detail=detail,
    ))

    # =====================================================================
    # 6. historical_signals — the signals (existence + frontier).
    #    Coverage-vs-universe is meaningless (signals are sparse; not every
    #    ticker flips). The real failures are (a) zero signals despite a
    #    universe that could flip, and (b) the signal frontier stopping far
    #    short of the state frontier — the daywalk stopped early.
    # =====================================================================
    signalable = _scalar(
        conn,
        "SELECT count(*) FROM ("
        "  SELECT s.ticker_id FROM stock_indicator_state s "
        "    JOIN tickers t ON t.ticker_id = s.ticker_id "
        "   WHERE t.exchange = :ex GROUP BY s.ticker_id HAVING count(*) >= 2"
        ") q",
        ex=exchange,
    )
    n_signals = _scalar(
        conn,
        "SELECT count(*) FROM historical_signals h "
        "  JOIN tickers t ON t.ticker_id = h.ticker_id WHERE t.exchange = :ex",
        ex=exchange,
    )
    sig_frontier = conn.execute(
        text(
            "SELECT MAX(h.signal_date) FROM historical_signals h "
            "  JOIN tickers t ON t.ticker_id = h.ticker_id "
            " WHERE t.exchange = :ex"
        ),
        {"ex": exchange},
    ).scalar()
    if signalable == 0:
        status, detail = "MISSING", "no ticker has >= 2 state rows (upstream)"
    elif n_signals == 0:
        status = "MISSING"
        detail = (
            f"0 signals for {signalable} signalable tickers — daywalk "
            f"produced nothing"
        )
    else:
        status, detail = "OK", f"latest signal {sig_frontier}"
        gap = _frontier_gap_days(state_frontier, sig_frontier)
        if gap is not None and gap > gap_days:
            status = "PARTIAL"
            detail = (
                f"signal frontier {sig_frontier} lags state {state_frontier} "
                f"by {gap}d — daywalk may have stopped early"
            )
    out.append(DatasetFidelity(
        dataset="historical_signals", status=status, rows=n_signals,
        detail=detail,
    ))

    # =====================================================================
    # 7. current_signals — snapshot of live signal state (currency-gated).
    #    Only written when a run reaches the present. On a historical
    #    backfill its emptiness is correct, so gate on whether the COMPUTE is
    #    current (state frontier within gap_days of the ohlcv frontier), not
    #    on wall-clock. Catches the "cold-start on a weekend -> empty
    #    current_signals, no email, green run" class.
    # =====================================================================
    n_current = _scalar(
        conn,
        "SELECT count(*) FROM current_signals c "
        "  JOIN tickers t ON t.ticker_id = c.ticker_id WHERE t.exchange = :ex",
        ex=exchange,
    )
    compute_gap = _frontier_gap_days(ohlcv_frontier, state_frontier)
    compute_is_current = compute_gap is not None and compute_gap <= gap_days
    if compute_is_current and n_current == 0:
        status = "MISSING"
        detail = (
            "compute is current but current_signals is empty — today's "
            "process_signals_for_day never ran (weekend/holiday gate?)"
        )
    elif not compute_is_current:
        status = "OK"
        detail = "compute not current (historical range) — snapshot not owed"
    else:
        status, detail = "OK", ""
    out.append(DatasetFidelity(
        dataset="current_signals", status=status, rows=n_current, detail=detail,
    ))

    # =====================================================================
    # 8. signal_enrichment — one row per historical signal (STRICT).
    #    Every historical signal has exactly one enrichment row (uq on
    #    ticker,tf,signal_date,signal_type; historical is unique per
    #    ticker,tf,signal_date so the join is clean). A shortfall means the
    #    enrichment stage silently skipped a cohort — the single strongest
    #    signal-side check.
    # =====================================================================
    enr_covered = _scalar(
        conn,
        "SELECT count(*) FROM historical_signals h "
        "  JOIN tickers t ON t.ticker_id = h.ticker_id "
        " WHERE t.exchange = :ex "
        "   AND EXISTS (SELECT 1 FROM signal_enrichment e "
        "                WHERE e.ticker_id = h.ticker_id "
        "                  AND e.timeframe = h.timeframe "
        "                  AND e.signal_date = h.signal_date)",
        ex=exchange,
    )
    missing = max(0, n_signals - enr_covered)
    detail = ""
    if missing:
        names = _names(
            conn,
            "SELECT DISTINCT t.symbol FROM historical_signals h "
            "  JOIN tickers t ON t.ticker_id = h.ticker_id "
            " WHERE t.exchange = :ex "
            "   AND NOT EXISTS (SELECT 1 FROM signal_enrichment e "
            "                    WHERE e.ticker_id = h.ticker_id "
            "                      AND e.timeframe = h.timeframe "
            "                      AND e.signal_date = h.signal_date) "
            " ORDER BY t.symbol",
            ex=exchange,
        )
        detail = _name_detail(missing, names, "signals without enrichment")
    out.append(DatasetFidelity(
        dataset="signal_enrichment",
        status=_status_for_coverage(enr_covered, n_signals),
        rows=enr_covered, covered=enr_covered, universe=n_signals, detail=detail,
    ))

    # total enrichment rows for this exchange — universe for labels/ensemble.
    n_enrichment = _scalar(
        conn,
        "SELECT count(*) FROM signal_enrichment e "
        "  JOIN tickers t ON t.ticker_id = e.ticker_id WHERE t.exchange = :ex",
        ex=exchange,
    )

    # =====================================================================
    # 9. signal_labels — forward-return targets (LENIENT).
    #    The newest signals cannot be labelled (their horizon has not
    #    matured), so coverage < 100% is normal and MUST NOT gate. Only an
    #    empty table when enrichment is non-empty is a defect.
    # =====================================================================
    n_labels = _scalar(
        conn,
        "SELECT count(*) FROM signal_labels l "
        "  JOIN signal_enrichment e ON e.signal_enrichment_id = "
        "       l.signal_enrichment_id "
        "  JOIN tickers t ON t.ticker_id = e.ticker_id WHERE t.exchange = :ex",
        ex=exchange,
    )
    n_labels_done = _scalar(
        conn,
        "SELECT count(*) FROM signal_labels l "
        "  JOIN signal_enrichment e ON e.signal_enrichment_id = "
        "       l.signal_enrichment_id "
        "  JOIN tickers t ON t.ticker_id = e.ticker_id "
        " WHERE t.exchange = :ex AND l.horizon_complete = TRUE",
        ex=exchange,
    )
    out.append(DatasetFidelity(
        dataset="signal_labels", status=_lenient(n_labels, n_enrichment),
        rows=n_labels, covered=n_labels, universe=n_enrichment,
        detail=(
            f"{n_labels_done} horizon-complete; remainder awaiting forward "
            f"horizon (normal)" if n_labels else "no labels yet"
        ),
    ))

    # =====================================================================
    # 10. signal_ensemble_features — cross-industry confirmation (STRICT).
    #     Backfill sweeps ALL signals (ensemble_scope=None), so every
    #     enrichment row should have >= 1 ensemble row (PK is
    #     (signal_enrichment_id, variant)).
    # =====================================================================
    #     ETFs are NOT in that universe. backfill_signal_ensemble_features
    #     skips any ticker whose industry has no ensemble members, and the
    #     'ETF' bucket never has any -- an ETF has no sector to lead or lag.
    #     Counting them as missing made run 69 report a 6.5% gap of which
    #     two thirds was correct behaviour, which is how a real gap hides.
    _ETF_EXCLUDED = (
        "  JOIN instrument_industry ii ON ii.ticker_id = e.ticker_id "
        " WHERE t.exchange = :ex AND COALESCE(ii.industry, '') <> 'ETF' "
    )
    n_ens_universe = _scalar(
        conn,
        "SELECT count(*) FROM signal_enrichment e "
        "  JOIN tickers t ON t.ticker_id = e.ticker_id "
        + _ETF_EXCLUDED,
        ex=exchange,
    )
    ens_covered = _scalar(
        conn,
        "SELECT count(*) FROM signal_enrichment e "
        "  JOIN tickers t ON t.ticker_id = e.ticker_id "
        + _ETF_EXCLUDED +
        "   AND EXISTS (SELECT 1 FROM signal_ensemble_features f "
        "                WHERE f.signal_enrichment_id = e.signal_enrichment_id)",
        ex=exchange,
    )
    n_etf_excluded = max(0, n_enrichment - n_ens_universe)
    detail = ""
    if n_ens_universe and ens_covered < n_ens_universe:
        detail = (
            f"{n_ens_universe - ens_covered} non-ETF enrichment row(s) without "
            f"ensemble features (industry below the index threshold)"
        )
    if n_etf_excluded:
        detail = (
            (detail + "; " if detail else "")
            + f"{n_etf_excluded} ETF row(s) EXCLUDED, not missing "
            f"(no sector to lead or lag)"
        )
    out.append(DatasetFidelity(
        dataset="signal_ensemble_features",
        status=_status_for_coverage(ens_covered, n_ens_universe),
        rows=ens_covered, covered=ens_covered, universe=n_ens_universe,
        detail=detail,
    ))

    # =====================================================================
    # 11. stock_benchmark_stats — feeds rank_score (LENIENT).
    #     Empty => fetch_rank_scores returns nothing => every emailed signal
    #     scores 0.0 and the email's ORDER BY rank_score silently degrades to
    #     ticker_id order. universe = tickers that HAVE a signal (only those
    #     get ranked). A handful legitimately have no stats (the NIFTY proxy's
    #     self-vs-self leg), so this is non-empty-gated, not 1:1.
    # =====================================================================
    bench_universe = _scalar(
        conn,
        "SELECT count(DISTINCT h.ticker_id) FROM historical_signals h "
        "  JOIN tickers t ON t.ticker_id = h.ticker_id WHERE t.exchange = :ex",
        ex=exchange,
    )
    bench_covered = _scalar(
        conn,
        "SELECT count(*) FROM ("
        "  SELECT DISTINCT h.ticker_id FROM historical_signals h "
        "    JOIN tickers t ON t.ticker_id = h.ticker_id "
        "   WHERE t.exchange = :ex "
        "     AND EXISTS (SELECT 1 FROM stock_benchmark_stats b "
        "                  WHERE b.ticker_id = h.ticker_id)"
        ") q",
        ex=exchange,
    )
    detail = f"{bench_covered}/{bench_universe} signalled tickers have stats"
    if bench_universe and bench_covered == 0:
        detail = "EMPTY — every emailed signal would rank 0.0"
    out.append(DatasetFidelity(
        dataset="stock_benchmark_stats",
        status=_lenient(bench_covered, bench_universe),
        rows=bench_covered, covered=bench_covered, universe=bench_universe,
        detail=detail,
    ))

    # =====================================================================
    # 12. industry_benchmark_stats — industry-vs-NIFTY leg of rank_score
    #     (LENIENT). universe = industries that have index values.
    # =====================================================================
    ibench_covered = _scalar(
        conn,
        "SELECT count(*) FROM ("
        "  SELECT DISTINCT v.industry_name FROM industry_index_value v "
        "   WHERE EXISTS (SELECT 1 FROM industry_benchmark_stats b "
        "                  WHERE b.industry_name = v.industry_name)"
        ") q",
    )
    detail = f"{ibench_covered}/{iiv_industries} valued industries have stats"
    if iiv_industries and ibench_covered == 0:
        detail = "EMPTY — industry rank leg would score 0.0"
    out.append(DatasetFidelity(
        dataset="industry_benchmark_stats",
        status=_lenient(ibench_covered, iiv_industries),
        rows=ibench_covered, covered=ibench_covered, universe=iiv_industries,
        detail=detail,
    ))

    return out
