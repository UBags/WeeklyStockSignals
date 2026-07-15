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
from ..domain.market_session import (
    market_tz_name,
)

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
    unavailable: int = 0
    """Entities the PROVIDER has nothing for — asked, and answered "no data".

    The third bucket, and the one that makes this audit usable as a gate.
    Without it, `covered < universe` is the only signal available and permanent
    upstream absence is indistinguishable from a stage that never ran. ~107 NSE
    microcaps have no sharesOutstanding on yfinance and never will; scoring them
    as a gap leaves quarterly_financials PARTIAL forever, so READY is red forever,
    and a gate that can never go green is not a gate — it cannot guard the wipe it
    exists to guard.

    Sourced from fundamentals_fetch_status, which records SharesFetchPermanent /
    FinancialsFetchPermanent verdicts. It is a record of what we learned, not a
    cache: the monthly job re-asks and clear() releases any ticker that gains
    coverage.
    """

    @property
    def coverage(self) -> float | None:
        if self.universe in (None, 0) or self.covered is None:
            return None
        return self.covered / self.universe

    @property
    def missing(self) -> int | None:
        """Entities with no data AND no provider verdict — i.e. genuinely
        unfetched. This is the number that should be zero."""
        if self.universe is None or self.covered is None:
            return None
        return max(0, self.universe - self.covered - self.unavailable)

    def render(self) -> str:
        cov = self.coverage
        cov_s = f"{cov * 100:5.1f}%" if cov is not None else "     —"
        counts = (
            f"{self.covered}/{self.universe}"
            if self.universe is not None else f"{self.rows}"
        )
        detail = self.detail
        if self.unavailable:
            note = f"{self.unavailable} unavailable upstream"
            detail = f"{note}; {detail}" if detail else note
        return (
            f"  {self.status:<8} {self.dataset:<24} {cov_s}  "
            f"{counts:<14} {detail}"
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


def _status_for_coverage(
    covered: int, universe: int, unavailable: int = 0,
) -> str:
    """OK means "as good as it CAN be", not "every cell is full".

    A dataset is OK when nothing is genuinely missing — every entity either has
    data, or the provider has been asked and has none. Scoring permanent upstream
    absence as a gap would leave quarterly_financials PARTIAL in perpetuity and
    make READY unreachable, which would render the whole audit decorative.

    The coverage thresholds still apply to what remains, so a stage that half-ran
    is still caught.
    """
    if universe == 0:
        return "MISSING"
    missing = max(0, universe - covered - unavailable)
    if covered == 0:
        return "MISSING"
    if missing == 0:
        return "OK"

    # There ARE genuinely unfetched entities, so this is not OK — regardless of
    # how good the percentage looks.
    #
    # The old rule was `coverage >= 0.95 -> OK`, and it had to be: without a
    # record of provider-side absence, "107 tickers with no data" and "107
    # tickers the provider has nothing for" were the same observation, so a
    # threshold was the only way to stop the audit screaming forever. It also
    # meant 107 genuinely unfetched tickers sailed through the gate at 95.4%.
    #
    # Now that absence is recorded, the fudge is unnecessary and the criterion
    # can be the honest one: every entity either HAS data or has been ASKED
    # ABOUT. Anything else is work not yet done.
    fetchable = max(1, universe - unavailable)
    ratio = covered / fetchable
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
                       min((o.bar_timestamp AT TIME ZONE :tz)::date) AS first_bar
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
            # 'UTC' was explicit, and session-independent, and WRONG: it asked for
            # the UTC date of an anchor that is local midnight, which for NSE is
            # the day BEFORE. first_bar is compared against listing_date — a local
            # calendar fact — so the truncation predicate was off by a day for
            # every ticker. Explicit is necessary; explicit and correct is the job.
            "tz": market_tz_name(exchange),
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

# L2-GATE-CANNOT-LIE: the table a verdict is ABOUT. Mapped, not interpolated
# freely — the name reaches SQL, and "it's only ever called with a constant" is
# not a property worth betting the query on.
_DATASET_TABLE = {
    "shares": "instrument_shares",
    "financials": "quarterly_financials",
    "industry": "instrument_industry",
}


def _unavailable_counts(conn, exchange: str) -> dict[str, int]:
    """{dataset: n} — entities the provider has told us it has nothing for AND
    that still have no data.

    The second half of that sentence is the entire point of this function.

    `missing = universe - covered - unavailable`, and OK is `missing == 0`. A
    ticker counted in BOTH `covered` and `unavailable` is therefore subtracted
    twice, and one genuinely-missing ticker silently drops out of the gate. A
    single stale verdict — one clear() that didn't fire, on one ticker — is
    enough to turn `READY: yes` on over a real gap. backfill() runs this audit;
    the gate guards the wipe.

    So the NOT EXISTS below is not defence-in-depth layered on top of the
    runners' clear() calls. It IS the invariant: a verdict only counts while the
    data it is a verdict about is still absent. The clear() calls are hygiene —
    they keep the table honest and the sweep cheap — but the gate no longer
    depends on them being perfect, which is the only reason it can be trusted to
    guard a wipe.
    """
    out: dict[str, int] = {}
    for dataset, table in _DATASET_TABLE.items():
        out[dataset] = _scalar(
            conn,
            f"""
            SELECT count(*)
              FROM fundamentals_fetch_status f
              JOIN tickers t ON t.ticker_id = f.ticker_id
             WHERE t.exchange = :ex
               AND t.is_active AND t.delisted_at IS NULL
               AND f.dataset = :ds
               AND f.status = 'UNAVAILABLE'
               AND NOT EXISTS (
                   SELECT 1 FROM {table} x
                    WHERE x.ticker_id = f.ticker_id
               )
            """,
            ex=exchange, ds=dataset,
        )
    return out


def _audit_per_ticker_table(
    conn, exchange: str, universe: int, *,
    dataset: str, table: str, detail: str = "", eq_only: bool = False,
    unavailable: int = 0,
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
    status = _status_for_coverage(covered, universe, unavailable)
    if status == "MISSING" and covered == 0:
        detail = detail or "no runner populates this for a universe"
    if eq_only:
        detail = (detail + "; " if detail else "") + "EQ only (ETFs excluded by design)"
    return DatasetFidelity(
        dataset=dataset, status=status, rows=rows,
        covered=covered, universe=universe, detail=detail,
        unavailable=unavailable,
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
        dark = _unavailable_counts(conn, exchange)
        datasets.append(_audit_per_ticker_table(
            conn, exchange, universe,
            dataset="instrument_industry", table="instrument_industry",
            # The third bucket, finally wired. Without it, tickers yfinance
            # cannot classify are scored as OUR gap and this dataset can never
            # reach OK — and a gate that cannot go green is not a gate.
            unavailable=dark.get("industry", 0),
        ))
        # EQ-scoped, the rest. See _audit_per_ticker_table.
        datasets.append(_audit_per_ticker_table(
            conn, exchange, eq_universe, eq_only=True,
            dataset="instrument_shares", table="instrument_shares",
            unavailable=dark.get("shares", 0),
        ))
        datasets.append(_audit_per_ticker_table(
            conn, exchange, eq_universe, eq_only=True,
            dataset="quarterly_financials", table="quarterly_financials",
            unavailable=dark.get("financials", 0),
        ))
        datasets.append(_audit_per_ticker_table(
            conn, exchange, eq_universe, eq_only=True,
            dataset="daily_market_cap", table="daily_market_cap",
            detail="derived from instrument_shares x ohlcv_bars",
            # Market cap is DERIVED. A ticker whose shares the provider has
            # nothing for can never have one — that absence is inherited, not a
            # gap of ours. What remains after subtracting it is real: tickers
            # that HAVE shares but no market cap, which means they have no BARS.
            # That is the throttle cohort, and it should keep this PARTIAL until
            # the OHLCV refetch — which is the truth.
            unavailable=dark.get("shares", 0),
        ))
        # Derived half of the chain (auto-wired). Lazy import breaks the module cycle:
        # data_fidelity_derived imports helpers from this module.
        from .data_fidelity_derived import audit_derived_chain
        datasets.extend(
            audit_derived_chain(conn, exchange, gap_days=gap_days)
        )
        datasets.extend(_audit_multi_exchange_safety(conn))

    return FidelityReport(
        exchange=exchange,
        generated_at=datetime.now(UTC),
        datasets=datasets,
    )
