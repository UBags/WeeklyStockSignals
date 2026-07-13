"""Read-only fidelity audit: what is populated for an exchange, and is it trustworthy?

WHY THIS EXISTS

The pipeline has always answered "did the stage run?" and never "is the data any
good?". That gap is what let DABUR sit in ohlcv_bars with 5 bars from last week,
flagged as a clean ETL success, while the industry indices were quietly rebuilt
without their largest constituents. `_coverage_shortfall_days` in the ETL runner
was the first check that asked the second question. This module generalises it to
every dataset the signal path depends on.

Nothing here writes. It is safe to run against production at any time, and it is
the gate that must pass before a cold start is trusted — or before the database
is wiped and rebuilt.

WHAT IT CHECKS

The signal path is a chain, and a break anywhere upstream silently corrupts
everything downstream:

    tickers -> instrument_industry -> +-> daily_market_cap -> industry_index_value
            -> instrument_shares  ---+                      -> industry state
            -> ohlcv_bars ------------------------------------> signals -> email

So each link is audited for PRESENCE (is there anything?) and COVERAGE (is there
enough, for enough of the universe?), not merely for existence of the table.

STATUS VOCABULARY

    OK       present and complete enough to trust
    PARTIAL  present but covering less of the universe than required
    MISSING  nothing at all — the stage that fills this has never run
    BLOCKED  a structural defect makes this dataset unsafe regardless of content

BLOCKED is the important one. It is not about rows; it is about the schema being
incapable of holding the truth. See `_audit_multi_exchange_safety`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# A dataset covering less of the active universe than this is PARTIAL, not OK.
# Not 100%: some tickers legitimately have no industry (newly listed shells), no
# shares data (ETFs), or no financials (trusts). These are the thresholds below
# which the gap stops being the long tail and starts being a broken stage.
_COVERAGE_OK = 0.95
_COVERAGE_PARTIAL = 0.50

# Mirrors EtlConfig.max_coverage_gap_days. A first bar this far after
# max(backfill_start, listing_date) means the fetch was truncated, not that the
# stock is new.
_DEFAULT_GAP_DAYS = 30

# Indicator warm-up: a ticker with fewer bars than this cannot produce a signal,
# and stage_2 rejects it outright ("has only N daily bars (need at least 30)").
_MIN_BARS_FOR_SIGNAL = 30


@dataclass(frozen=True)
class DatasetFidelity:
    """One link in the chain."""

    dataset: str
    status: str                      # OK | PARTIAL | MISSING | BLOCKED
    rows: int
    covered: int | None = None       # entities with data
    universe: int | None = None      # entities that should have data
    detail: str = ""

    @property
    def coverage(self) -> float | None:
        if self.universe in (None, 0) or self.covered is None:
            return None
        return self.covered / self.universe

    def render(self) -> str:
        cov = self.coverage
        cov_s = f"{cov * 100:5.1f}%" if cov is not None else "     —"
        counts = (
            f"{self.covered}/{self.universe}"
            if self.universe is not None else f"{self.rows}"
        )
        return (
            f"  {self.status:<8} {self.dataset:<24} {cov_s}  "
            f"{counts:<14} {self.detail}"
        )


@dataclass(frozen=True)
class FidelityReport:
    exchange: str
    generated_at: datetime
    datasets: list[DatasetFidelity] = field(default_factory=list)

    @property
    def blockers(self) -> list[DatasetFidelity]:
        return [d for d in self.datasets if d.status == "BLOCKED"]

    @property
    def gaps(self) -> list[DatasetFidelity]:
        return [d for d in self.datasets if d.status in ("MISSING", "PARTIAL")]

    @property
    def ready(self) -> bool:
        """True only when every link is OK. Used as a gate, so it is strict."""
        return all(d.status == "OK" for d in self.datasets)

    def render(self) -> str:
        lines = [
            f"data fidelity — {self.exchange} "
            f"@ {self.generated_at.isoformat(timespec='seconds')}",
            "",
            f"  {'STATUS':<8} {'DATASET':<24} {'COVER':>6}  "
            f"{'COVERED/TOTAL':<14} DETAIL",
            f"  {'-' * 8} {'-' * 24} {'-' * 6}  {'-' * 14} {'-' * 40}",
        ]
        lines += [d.render() for d in self.datasets]
        lines.append("")
        if self.blockers:
            lines.append(
                f"  BLOCKED: {len(self.blockers)} structural defect(s) — "
                f"data cannot be trusted regardless of row counts."
            )
        if self.gaps:
            lines.append(
                f"  GAPS: {len(self.gaps)} dataset(s) missing or incomplete."
            )
        lines.append(
            f"  READY: {'yes' if self.ready else 'NO'}"
        )
        return "\n".join(lines)


def _status_for_coverage(covered: int, universe: int) -> str:
    if universe == 0 or covered == 0:
        return "MISSING"
    ratio = covered / universe
    if ratio >= _COVERAGE_OK:
        return "OK"
    return "PARTIAL" if ratio >= _COVERAGE_PARTIAL else "MISSING"


def _scalar(conn, sql: str, **params) -> int:
    return conn.execute(text(sql), params).scalar() or 0


# --------------------------------------------------------------------------
# The universe
# --------------------------------------------------------------------------

def _audit_tickers(conn, exchange: str) -> tuple[DatasetFidelity, int, int]:
    """Returns (fidelity, universe, eq_universe).

    The EQ universe is a separate number because ETFs are not equities and must
    not be audited as if they were. yfinance never reports sharesOutstanding for
    an ETF, ETFs have no quarterly financials, and run_market_cap_backfill
    explicitly filters to instrument_type == 'EQ'. Measuring those three datasets
    against the FULL universe reports 328 ETFs as a data gap when they are
    behaving exactly as designed — which is precisely the false alarm the first
    version of this module raised, and then presented as the biggest problem in
    the database.
    """
    n = _scalar(
        conn,
        """
        SELECT count(*) FROM tickers
         WHERE exchange = :ex AND is_active AND delisted_at IS NULL
        """,
        ex=exchange,
    )
    n_eq = _scalar(
        conn,
        """
        SELECT count(*) FROM tickers
         WHERE exchange = :ex AND is_active AND delisted_at IS NULL
           AND instrument_type = 'EQ'
        """,
        ex=exchange,
    )
    no_listing = _scalar(
        conn,
        """
        SELECT count(*) FROM tickers
         WHERE exchange = :ex AND is_active AND delisted_at IS NULL
           AND listing_date IS NULL
        """,
        ex=exchange,
    )
    if n == 0:
        return (
            DatasetFidelity(
                dataset="tickers",
                status="MISSING",
                rows=0,
                detail="no active tickers — run refresh-listings first",
            ),
            0,
            0,
        )
    detail = ""
    status = "OK"
    if no_listing:
        # listing_date is the ONLY thing that distinguishes "short history because
        # the stock is new" from "short history because Yahoo truncated us". Without
        # it the ohlcv truncation check below is blind for those tickers.
        status = "PARTIAL"
        detail = (
            f"{no_listing} ticker(s) have no listing_date — "
            f"truncation cannot be detected for them"
        )
    return (
        DatasetFidelity(
            dataset="tickers",
            status=status,
            rows=n,
            covered=n - no_listing,
            universe=n,
            detail=detail,
        ),
        n,
        n_eq,
    )


# --------------------------------------------------------------------------
# Price history — presence, warm-up depth, and truncation
# --------------------------------------------------------------------------

def _requested_start(conn, exchange: str) -> date | None:
    """The date_from of the most recent backfill for this exchange.

    This is not a nicety. The truncation test is

        expected_first_bar = max(REQUESTED_START, listing_date)

    and dropping the first term is catastrophic for the audit: RELIANCE listed
    1995-11-29, but a backfill run from 2006-04-01 can only ever produce a first
    bar of 2006-04-03. Compare that against listing_date alone and RELIANCE — and
    every other pre-2006 listing with flawless history — is reported as TRUNCATED.
    The first version of this module did exactly that and cried wolf on 738
    tickers.

    Returns None when no backfill has ever run, in which case truncation cannot be
    assessed and is not guessed at.
    """
    row = conn.execute(
        text("""
            SELECT date_from
              FROM backfill_runs
             WHERE exchange = :ex
             ORDER BY started_at DESC
             LIMIT 1
        """),
        {"ex": exchange},
    ).first()
    return row.date_from if row else None


def _audit_ohlcv(
    conn, exchange: str, universe: int, gap_days: int,
    requested_start: date | None,
) -> DatasetFidelity:
    """Three distinct populations, which the ETL used to conflate into 'success'.

    - enough bars to produce a signal (>= 30)
    - too few bars, but CORRECTLY so (genuinely new listing)
    - too few bars because the fetch was TRUNCATED (this is the bug)

    Truncation is `first_bar` landing more than `gap_days` after
    max(requested_start, listing_date) — the same predicate the ETL runner uses in
    _coverage_shortfall_days. BOTH terms are required:

      * listing_date alone false-flags every stock that listed before the backfill
        window opened (a 1995 listing cannot have a 2006 backfill produce bars
        from 1995);
      * requested_start alone false-flags every genuine new listing (ELCID took
        its NSE listing on 2026-04-20; its short history is CORRECT).
    """
    row = conn.execute(
        text("""
            WITH b AS (
                SELECT t.ticker_id,
                       t.listing_date,
                       count(o.ticker_id) AS n_bars,
                       min((o.bar_timestamp AT TIME ZONE 'UTC')::date) AS first_bar
                  FROM tickers t
                  LEFT JOIN ohlcv_bars o
                    ON o.ticker_id = t.ticker_id AND o.timeframe = '1D'
                 WHERE t.exchange = :ex AND t.is_active AND t.delisted_at IS NULL
                 GROUP BY t.ticker_id, t.listing_date
            )
            SELECT
                count(*) FILTER (WHERE n_bars >= :minbars)         AS signalable,
                count(*) FILTER (WHERE n_bars = 0)                 AS empty,
                count(*) FILTER (
                    WHERE n_bars > 0
                      AND listing_date IS NOT NULL
                      -- Use CAST(...), never the postgres double-colon cast
                      -- shorthand, on a NAMED PARAMETER. SQLAlchemy's text()
                      -- bind parser does not recognise a parameter immediately
                      -- followed by that shorthand: it leaves the token in the
                      -- SQL verbatim, never binds it, and Postgres then chokes
                      -- on the stray colon.
                      -- NB: text() also scans COMMENTS for parameters, so this
                      -- note deliberately spells none of that out literally —
                      -- writing the broken form here would conjure a phantom
                      -- bindparam out of a comment.
                      AND CAST(:start AS date) IS NOT NULL
                      -- GREATEST ignores NULLs in Postgres, so with no prior
                      -- backfill this would silently collapse to listing_date
                      -- alone — the exact bug being fixed. Hence the guard above.
                      AND first_bar >
                          GREATEST(listing_date, CAST(:start AS date))
                          + (:gap || ' days')::interval
                )                                                  AS truncated,
                coalesce(sum(n_bars), 0)                           AS total_bars
              FROM b
        """),
        {
            "ex": exchange,
            "minbars": _MIN_BARS_FOR_SIGNAL,
            "gap": gap_days,
            "start": requested_start,
        },
    ).one()

    status = _status_for_coverage(row.signalable, universe)
    bits = []
    if requested_start is None:
        bits.append("no prior backfill — truncation NOT assessed")
    if row.truncated:
        # This is never acceptable. A truncated large cap poisons daily_market_cap,
        # which poisons constituent selection, which poisons every industry index.
        status = "PARTIAL" if status == "OK" else status
        bits.append(f"{row.truncated} TRUNCATED (fetch returned short history)")
    if row.empty:
        bits.append(f"{row.empty} with zero bars")
    return DatasetFidelity(
        dataset="ohlcv_bars",
        status=status,
        rows=int(row.total_bars),
        covered=row.signalable,
        universe=universe,
        detail="; ".join(bits) or f">= {_MIN_BARS_FOR_SIGNAL} bars",
    )


# --------------------------------------------------------------------------
# Reference data — the layer the backfill assumes and never builds
# --------------------------------------------------------------------------

def _audit_per_ticker_table(
    conn, exchange: str, universe: int, *,
    dataset: str, table: str, detail: str = "", eq_only: bool = False,
) -> DatasetFidelity:
    """Coverage of a table keyed by ticker_id, scoped to an exchange via tickers.

    None of instrument_industry / instrument_shares / quarterly_financials carry
    an exchange column, so the scope has to come from the join.

    `eq_only` restricts BOTH the numerator and the denominator to
    instrument_type='EQ'. It is mandatory for shares, financials and market cap:
    yfinance never reports sharesOutstanding for an ETF, ETFs have no quarterly
    financials, and run_market_cap_backfill filters to EQ by design. Counting the
    328 NSE ETFs as a gap in those datasets reports correct behaviour as a defect.
    """
    eq = " AND t.instrument_type = 'EQ'" if eq_only else ""
    covered = _scalar(
        conn,
        f"""
        SELECT count(DISTINCT t.ticker_id)
          FROM tickers t
          JOIN {table} x ON x.ticker_id = t.ticker_id
         WHERE t.exchange = :ex AND t.is_active AND t.delisted_at IS NULL{eq}
        """,
        ex=exchange,
    )
    rows = _scalar(
        conn,
        f"""
        SELECT count(*)
          FROM tickers t JOIN {table} x ON x.ticker_id = t.ticker_id
         WHERE t.exchange = :ex AND t.is_active AND t.delisted_at IS NULL{eq}
        """,
        ex=exchange,
    )
    status = _status_for_coverage(covered, universe)
    if status == "MISSING" and covered == 0:
        detail = detail or "no runner populates this for a universe"
    if eq_only:
        detail = (detail + "; " if detail else "") + "EQ only (ETFs excluded by design)"
    return DatasetFidelity(
        dataset=dataset, status=status, rows=rows,
        covered=covered, universe=universe, detail=detail,
    )


# --------------------------------------------------------------------------
# Structural safety — the checks that are about the SCHEMA, not the rows
# --------------------------------------------------------------------------

def _audit_multi_exchange_safety(conn) -> list[DatasetFidelity]:
    """Can this database hold more than one exchange without corrupting itself?

    industry_index_value's primary key is (industry_name, trade_date). There is no
    exchange column. So NSE's "Software - Infrastructure" on 2026-07-10 and NYSE's
    "Software - Infrastructure" on 2026-07-10 are THE SAME ROW.

    That does not fail loudly. It upserts. Backfilling a second exchange silently
    overwrites the first exchange's industry index values, and every signal derived
    from them is then computed against a corrupted index. industry_indicator_state
    (PK industry_name, timeframe, date) has the same defect.

    This is reported as BLOCKED rather than PARTIAL because no amount of correct
    data fixes it — the table cannot represent the truth.
    """
    n_exchanges = _scalar(
        conn,
        """
        SELECT count(DISTINCT exchange) FROM tickers
         WHERE is_active AND delisted_at IS NULL
        """,
    )
    out: list[DatasetFidelity] = []

    iiv_rows = _scalar(conn, "SELECT count(*) FROM industry_index_value")
    if n_exchanges > 1:
        out.append(DatasetFidelity(
            dataset="industry_index_value",
            status="BLOCKED",
            rows=iiv_rows,
            detail=(
                f"PK (industry_name, trade_date) has NO exchange column, but "
                f"{n_exchanges} exchanges are active — industry indices from "
                f"different exchanges COLLIDE and silently overwrite each other"
            ),
        ))
    else:
        out.append(DatasetFidelity(
            dataset="industry_index_value",
            status="OK" if iiv_rows else "MISSING",
            rows=iiv_rows,
            detail=(
                "single exchange, so the missing exchange key in the PK is "
                "latent — adding a second exchange WILL corrupt this"
            ),
        ))

    sub_rows = _scalar(
        conn, "SELECT count(*) FROM subscribers WHERE is_active",
    )
    out.append(DatasetFidelity(
        dataset="subscribers",
        status="OK" if sub_rows else "MISSING",
        rows=sub_rows,
        detail=(
            "no exchange column — subscribers are global; a per-exchange "
            "default subscriber cannot be expressed"
        ),
    ))
    return out


# --------------------------------------------------------------------------

def audit_exchange(
    db_engine: Engine,
    exchange: str,
    *,
    gap_days: int = _DEFAULT_GAP_DAYS,
) -> FidelityReport:
    """Audit every link in the signal chain for `exchange`. Writes nothing."""
    datasets: list[DatasetFidelity] = []

    with db_engine.connect() as conn:
        tickers_fid, universe, eq_universe = _audit_tickers(conn, exchange)
        datasets.append(tickers_fid)

        if universe == 0:
            # Everything downstream is scoped to the universe. With no universe
            # there is nothing to say about it, and reporting 0/0 as "OK" would
            # be exactly the kind of vacuous pass this module exists to prevent.
            datasets.append(DatasetFidelity(
                dataset="(downstream)", status="MISSING", rows=0,
                detail="skipped — no ticker universe to audit against",
            ))
            return FidelityReport(
                exchange=exchange,
                generated_at=datetime.now(UTC),
                datasets=datasets,
            )

        req_start = _requested_start(conn, exchange)
        datasets.append(
            _audit_ohlcv(conn, exchange, universe, gap_days, req_start)
        )
        datasets.append(_audit_per_ticker_table(
            conn, exchange, universe,
            dataset="instrument_industry", table="instrument_industry",
        ))
        # EQ-scoped, all three. See _audit_per_ticker_table.
        datasets.append(_audit_per_ticker_table(
            conn, exchange, eq_universe, eq_only=True,
            dataset="instrument_shares", table="instrument_shares",
        ))
        datasets.append(_audit_per_ticker_table(
            conn, exchange, eq_universe, eq_only=True,
            dataset="quarterly_financials", table="quarterly_financials",
        ))
        datasets.append(_audit_per_ticker_table(
            conn, exchange, eq_universe, eq_only=True,
            dataset="daily_market_cap", table="daily_market_cap",
            detail="derived from instrument_shares x ohlcv_bars",
        ))
        datasets.extend(_audit_multi_exchange_safety(conn))

    return FidelityReport(
        exchange=exchange,
        generated_at=datetime.now(UTC),
        datasets=datasets,
    )
