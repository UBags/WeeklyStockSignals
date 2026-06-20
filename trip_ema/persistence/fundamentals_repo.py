"""Repositories for the fundamentals tables (Phase 1).

Three repos in one file because they're tightly coupled in the
runner's workflow: the runner reads overrides to decide which
tickers to skip, writes classification rows, and brackets the
whole thing with a fundamentals_run row.

Convention matches the rest of `trip_ema/persistence/`:
  - Connection in constructor.
  - No transaction management here; caller wraps with
    `with transaction(engine) as conn:`.
  - Returns domain dataclasses, not sqlalchemy Row objects.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..fundamentals import (
    CLASSIFICATION_SOURCES,
    RUN_PHASES,
    RUN_STATUSES,
    FundamentalsRun,
    IndustryClassification,
    IndustryOverride,
)

logger = logging.getLogger(__name__)


# ---------- IndustryClassification ---------- #


class InstrumentIndustryRepository:
    """Reads and writes against `instrument_industry`.

    The `upsert` method NEVER overwrites a row whose ticker has an
    `industry_override` entry. The caller is expected to read the
    override table first (or use `upsert_unless_overridden` which
    does the SELECT inline)."""

    def __init__(self, conn: Connection):
        self._conn = conn

    # ----- reads ----- #

    def get(self, ticker_id: int) -> Optional[IndustryClassification]:
        row = self._conn.execute(
            text("""
                SELECT ticker_id, sector, industry,
                       classification_source, fetched_at, updated_at,
                       etf_long_name
                FROM instrument_industry
                WHERE ticker_id = :tid
            """),
            {"tid": ticker_id},
        ).fetchone()
        if row is None:
            return None
        return _classification_from_row(row)

    def list_for_exchange(
        self, exchange: str, *, only_classified: bool = False,
    ) -> list[IndustryClassification]:
        """Join against `tickers` to scope by exchange. Returns one row
        per ticker with a fundamentals entry (or all tickers if not
        only_classified)."""
        sql = """
            SELECT ii.ticker_id, ii.sector, ii.industry,
                   ii.classification_source, ii.fetched_at, ii.updated_at,
                   ii.etf_long_name
            FROM tickers t
            JOIN instrument_industry ii ON ii.ticker_id = t.ticker_id
            WHERE t.exchange = :ex
            ORDER BY ii.industry NULLS LAST, t.symbol
        """
        return [
            _classification_from_row(r)
            for r in self._conn.execute(text(sql), {"ex": exchange})
        ]

    def upsert_etf(
        self,
        ticker_id: int,
        *,
        etf_long_name: Optional[str],
    ) -> bool:
        """Classify a ticker as an ETF.

        Writes sector='ETF', industry='ETF', classification_source=
        'INSTRUMENT_TYPE', and stores the human-readable long name
        (the one useful piece of metadata yfinance provides for
        Indian ETFs) in etf_long_name. Honors operator overrides
        the same way upsert_unless_overridden does — returns False
        if an override exists.

        Why a dedicated method instead of overloading upsert(): the
        ETF classification is structurally different (always 'ETF'/
        'ETF', source pinned to 'INSTRUMENT_TYPE'). A single method
        with explicit semantics is clearer than a four-kwarg
        overload that has to validate combinations.
        """
        result = self._conn.execute(
            text("""
                INSERT INTO instrument_industry
                    (ticker_id, sector, industry, classification_source,
                     etf_long_name, fetched_at, updated_at)
                SELECT :tid, 'ETF', 'ETF', 'INSTRUMENT_TYPE',
                       :name, NOW(), NOW()
                WHERE NOT EXISTS (
                    SELECT 1 FROM industry_override WHERE ticker_id = :tid
                )
                ON CONFLICT (ticker_id) DO UPDATE SET
                    sector = 'ETF',
                    industry = 'ETF',
                    classification_source = 'INSTRUMENT_TYPE',
                    etf_long_name = EXCLUDED.etf_long_name,
                    updated_at = NOW()
                WHERE NOT EXISTS (
                    SELECT 1 FROM industry_override WHERE ticker_id = :tid
                )
            """),
            {"tid": ticker_id, "name": etf_long_name},
        )
        return result.rowcount > 0

    def coverage_counts_by_type(self) -> list[tuple[str, str, int, int]]:
        """Per (exchange, instrument_type) coverage breakdown.

        Returns tuples of (exchange, instrument_type, total, classified).
        `classified` here means "has a non-NULL industry OR is a bucketed
        ETF" — i.e., the row exists with usable data.

        This is the right query for the post-Phase-1 view, because
        equity coverage and ETF coverage have different shapes:
        equities aim for ~85%+ via yfinance, while ETFs aim for 100%
        once a refresh has run (they're bucketed deterministically).
        """
        rows = self._conn.execute(text("""
            SELECT
                t.exchange,
                t.instrument_type,
                COUNT(*) AS total,
                COUNT(ii.ticker_id) FILTER (
                    WHERE ii.industry IS NOT NULL
                ) AS classified
            FROM tickers t
            LEFT JOIN instrument_industry ii ON ii.ticker_id = t.ticker_id
            WHERE t.is_active
            GROUP BY t.exchange, t.instrument_type
            ORDER BY t.exchange, t.instrument_type
        """))
        return [
            (r[0], r[1], int(r[2]), int(r[3]))
            for r in rows
        ]

    def coverage_counts(self) -> list[tuple[str, int, int]]:
        """Per-exchange (exchange, total_active, classified) tuples.

        Coarser version of coverage_counts_by_type() that ignores
        instrument_type. Kept for backward compatibility with the
        existing CLI / HTTP / tests written before 0007 landed.

        `classified` counts rows where industry OR sector is non-NULL
        (so a row with industry='ETF', sector='ETF' counts as
        classified, same as a row with industry='IT Services').
        """
        rows = self._conn.execute(text("""
            SELECT
                t.exchange,
                COUNT(*) AS total,
                COUNT(ii.ticker_id) FILTER (
                    WHERE ii.industry IS NOT NULL OR ii.sector IS NOT NULL
                ) AS classified
            FROM tickers t
            LEFT JOIN instrument_industry ii ON ii.ticker_id = t.ticker_id
            WHERE t.is_active
            GROUP BY t.exchange
            ORDER BY t.exchange
        """))
        return [(r[0], int(r[1]), int(r[2])) for r in rows]

    # ----- writes ----- #

    def upsert(
        self,
        ticker_id: int,
        *,
        sector: Optional[str],
        industry: Optional[str],
        classification_source: str = "YFINANCE",
    ) -> None:
        """Unconditional upsert. Use `upsert_unless_overridden` in
        the runner; this method is for the operator-override CLI path
        where the override IS the source."""
        if classification_source not in CLASSIFICATION_SOURCES:
            raise ValueError(
                f"classification_source must be one of {CLASSIFICATION_SOURCES}, "
                f"got {classification_source!r}"
            )
        self._conn.execute(
            text("""
                INSERT INTO instrument_industry
                    (ticker_id, sector, industry, classification_source,
                     fetched_at, updated_at)
                VALUES (:tid, :sec, :ind, :src, NOW(), NOW())
                ON CONFLICT (ticker_id) DO UPDATE SET
                    sector = EXCLUDED.sector,
                    industry = EXCLUDED.industry,
                    classification_source = EXCLUDED.classification_source,
                    updated_at = NOW()
            """),
            {"tid": ticker_id, "sec": sector, "ind": industry,
             "src": classification_source},
        )

    def upsert_unless_overridden(
        self,
        ticker_id: int,
        *,
        sector: Optional[str],
        industry: Optional[str],
        classification_source: str = "YFINANCE",
    ) -> bool:
        """Upsert with an override-table guard. Returns True if the
        write happened, False if the ticker has an override and the
        write was skipped.

        The guard is a single SQL statement (WHERE NOT EXISTS in the
        UPSERT) to avoid a TOCTOU race against the override CLI
        running concurrently."""
        if classification_source not in CLASSIFICATION_SOURCES:
            raise ValueError(
                f"classification_source must be one of {CLASSIFICATION_SOURCES}, "
                f"got {classification_source!r}"
            )
        result = self._conn.execute(
            text("""
                INSERT INTO instrument_industry
                    (ticker_id, sector, industry, classification_source,
                     fetched_at, updated_at)
                SELECT :tid, :sec, :ind, :src, NOW(), NOW()
                WHERE NOT EXISTS (
                    SELECT 1 FROM industry_override WHERE ticker_id = :tid
                )
                ON CONFLICT (ticker_id) DO UPDATE SET
                    sector = EXCLUDED.sector,
                    industry = EXCLUDED.industry,
                    classification_source = EXCLUDED.classification_source,
                    updated_at = NOW()
                WHERE NOT EXISTS (
                    SELECT 1 FROM industry_override WHERE ticker_id = :tid
                )
            """),
            {"tid": ticker_id, "sec": sector, "ind": industry,
             "src": classification_source},
        )
        return result.rowcount > 0

    def delete(self, ticker_id: int) -> bool:
        result = self._conn.execute(
            text("DELETE FROM instrument_industry WHERE ticker_id = :tid"),
            {"tid": ticker_id},
        )
        return result.rowcount > 0


def _classification_from_row(row) -> IndustryClassification:
    # row tuple is (ticker_id, sector, industry, classification_source,
    # fetched_at, updated_at, etf_long_name). The last column is NULL
    # for equity rows.
    return IndustryClassification(
        ticker_id=row[0],
        sector=row[1],
        industry=row[2],
        classification_source=row[3],
        fetched_at=row[4],
        updated_at=row[5],
        etf_long_name=row[6] if len(row) > 6 else None,
    )


# ---------- IndustryOverride ---------- #


class IndustryOverrideRepository:
    def __init__(self, conn: Connection):
        self._conn = conn

    def get(self, ticker_id: int) -> Optional[IndustryOverride]:
        row = self._conn.execute(
            text("""
                SELECT ticker_id, sector, industry, note, set_by, set_at
                FROM industry_override
                WHERE ticker_id = :tid
            """),
            {"tid": ticker_id},
        ).fetchone()
        if row is None:
            return None
        return IndustryOverride(*row)

    def list_all(self) -> list[IndustryOverride]:
        rows = self._conn.execute(text("""
            SELECT ticker_id, sector, industry, note, set_by, set_at
            FROM industry_override
            ORDER BY ticker_id
        """))
        return [IndustryOverride(*r) for r in rows]

    def upsert(
        self,
        ticker_id: int,
        *,
        sector: str,
        industry: str,
        note: Optional[str] = None,
        set_by: Optional[str] = None,
    ) -> None:
        """Set the operator override AND immediately apply it to
        instrument_industry (so the next read sees the new value
        without waiting for the next refresh)."""
        self._conn.execute(
            text("""
                INSERT INTO industry_override
                    (ticker_id, sector, industry, note, set_by, set_at)
                VALUES (:tid, :sec, :ind, :note, :by, NOW())
                ON CONFLICT (ticker_id) DO UPDATE SET
                    sector = EXCLUDED.sector,
                    industry = EXCLUDED.industry,
                    note = EXCLUDED.note,
                    set_by = EXCLUDED.set_by,
                    set_at = NOW()
            """),
            {"tid": ticker_id, "sec": sector, "ind": industry,
             "note": note, "by": set_by},
        )
        # Apply to instrument_industry too. We use a separate UPSERT
        # rather than upsert_unless_overridden because the override
        # IS the source here.
        self._conn.execute(
            text("""
                INSERT INTO instrument_industry
                    (ticker_id, sector, industry, classification_source,
                     fetched_at, updated_at)
                VALUES (:tid, :sec, :ind, 'MANUAL_OVERRIDE', NOW(), NOW())
                ON CONFLICT (ticker_id) DO UPDATE SET
                    sector = EXCLUDED.sector,
                    industry = EXCLUDED.industry,
                    classification_source = 'MANUAL_OVERRIDE',
                    updated_at = NOW()
            """),
            {"tid": ticker_id, "sec": sector, "ind": industry},
        )

    def delete(self, ticker_id: int) -> bool:
        result = self._conn.execute(
            text("DELETE FROM industry_override WHERE ticker_id = :tid"),
            {"tid": ticker_id},
        )
        return result.rowcount > 0


# ---------- FundamentalsRun ---------- #


class FundamentalsRunRepository:
    """Tracks one row per refresh invocation across all phases.

    The runner uses this for resumability: a worker that died mid-run
    can be restarted with `--resume RUN_ID`, which reads back the
    `progress_json` cursor and picks up.
    """

    def __init__(self, conn: Connection):
        self._conn = conn

    # ----- reads ----- #

    def get(self, run_id: int) -> Optional[FundamentalsRun]:
        row = self._conn.execute(
            text("""
                SELECT run_id, phase, exchange, started_at, completed_at,
                       status, succeeded, failed, skipped, progress_json, notes
                FROM fundamentals_run
                WHERE run_id = :rid
            """),
            {"rid": run_id},
        ).fetchone()
        if row is None:
            return None
        return _run_from_row(row)

    def list_recent(
        self,
        *,
        phase: Optional[str] = None,
        exchange: Optional[str] = None,
        limit: int = 20,
    ) -> list[FundamentalsRun]:
        clauses = []
        params: dict[str, object] = {"lim": limit}
        if phase is not None:
            clauses.append("phase = :phase")
            params["phase"] = phase
        if exchange is not None:
            clauses.append("exchange = :ex")
            params["ex"] = exchange
        where = " AND ".join(clauses) if clauses else "TRUE"
        rows = self._conn.execute(
            text(f"""
                SELECT run_id, phase, exchange, started_at, completed_at,
                       status, succeeded, failed, skipped, progress_json, notes
                FROM fundamentals_run
                WHERE {where}
                ORDER BY started_at DESC
                LIMIT :lim
            """),
            params,
        )
        return [_run_from_row(r) for r in rows]

    def last_success(
        self, *, phase: str, exchange: Optional[str],
    ) -> Optional[FundamentalsRun]:
        """The most recent SUCCESS row for (phase, exchange).

        Used by the quarterly scheduler to decide whether to skip
        a refresh that's already been done this quarter.
        """
        # NULL-safe exchange match (a 'phase' that's exchange-agnostic
        # uses NULL exchange; we need to find that with exchange=None).
        if exchange is None:
            sql = """
                SELECT run_id, phase, exchange, started_at, completed_at,
                       status, succeeded, failed, skipped, progress_json, notes
                FROM fundamentals_run
                WHERE phase = :phase AND exchange IS NULL
                  AND status = 'SUCCESS'
                ORDER BY completed_at DESC NULLS LAST
                LIMIT 1
            """
            params = {"phase": phase}
        else:
            sql = """
                SELECT run_id, phase, exchange, started_at, completed_at,
                       status, succeeded, failed, skipped, progress_json, notes
                FROM fundamentals_run
                WHERE phase = :phase AND exchange = :ex
                  AND status = 'SUCCESS'
                ORDER BY completed_at DESC NULLS LAST
                LIMIT 1
            """
            params = {"phase": phase, "ex": exchange}
        row = self._conn.execute(text(sql), params).fetchone()
        return _run_from_row(row) if row else None

    def last_completed(
        self, *, phase: str, exchange: Optional[str],
    ) -> Optional[FundamentalsRun]:
        """The most recent row for (phase, exchange) that finished
        with a non-failure status — SUCCESS or PARTIAL.

        Used by skip-if-recent guards in Phase 1-3 runners. The
        original `last_success` only matches SUCCESS, but phases
        that work over a heterogeneous universe (e.g. shares
        refresh over a mix of EQ tickers and ETFs) routinely end
        in PARTIAL because ETFs lack the data — that's expected,
        not a failure to retry. Treating PARTIAL as "completed"
        for the purposes of skip-if-recent prevents endless
        re-runs of expected-partial phases.
        """
        if exchange is None:
            sql = """
                SELECT run_id, phase, exchange, started_at, completed_at,
                       status, succeeded, failed, skipped, progress_json, notes
                FROM fundamentals_run
                WHERE phase = :phase AND exchange IS NULL
                  AND status IN ('SUCCESS', 'PARTIAL')
                ORDER BY completed_at DESC NULLS LAST
                LIMIT 1
            """
            params = {"phase": phase}
        else:
            sql = """
                SELECT run_id, phase, exchange, started_at, completed_at,
                       status, succeeded, failed, skipped, progress_json, notes
                FROM fundamentals_run
                WHERE phase = :phase AND exchange = :ex
                  AND status IN ('SUCCESS', 'PARTIAL')
                ORDER BY completed_at DESC NULLS LAST
                LIMIT 1
            """
            params = {"phase": phase, "ex": exchange}
        row = self._conn.execute(text(sql), params).fetchone()
        return _run_from_row(row) if row else None

    # ----- writes ----- #

    def open(
        self, *, phase: str, exchange: Optional[str] = None, notes: Optional[str] = None,
    ) -> int:
        """Insert a new RUNNING row. Returns run_id."""
        if phase not in RUN_PHASES:
            raise ValueError(
                f"phase must be one of {RUN_PHASES}, got {phase!r}"
            )
        row = self._conn.execute(
            text("""
                INSERT INTO fundamentals_run (phase, exchange, started_at, status, notes)
                VALUES (:phase, :ex, NOW(), 'RUNNING', :notes)
                RETURNING run_id
            """),
            {"phase": phase, "ex": exchange, "notes": notes},
        ).fetchone()
        return int(row[0])

    def update_progress(
        self,
        run_id: int,
        *,
        succeeded: Optional[int] = None,
        failed: Optional[int] = None,
        skipped: Optional[int] = None,
        progress_json: Optional[dict] = None,
    ) -> None:
        """Partial update — None means "don't touch". Used by the
        runner to checkpoint every 50 tickers."""
        sets = []
        params: dict[str, object] = {"rid": run_id}
        if succeeded is not None:
            sets.append("succeeded = :s"); params["s"] = succeeded
        if failed is not None:
            sets.append("failed = :f"); params["f"] = failed
        if skipped is not None:
            sets.append("skipped = :sk"); params["sk"] = skipped
        if progress_json is not None:
            sets.append("progress_json = :pj"); params["pj"] = json.dumps(progress_json)
        if not sets:
            return
        self._conn.execute(
            text(f"UPDATE fundamentals_run SET {', '.join(sets)} WHERE run_id = :rid"),
            params,
        )

    def close(
        self,
        run_id: int,
        *,
        status: str,
        succeeded: int,
        failed: int,
        skipped: int,
        notes: Optional[str] = None,
    ) -> None:
        if status not in RUN_STATUSES:
            raise ValueError(f"status must be one of {RUN_STATUSES}, got {status!r}")
        if status == "RUNNING":
            raise ValueError("close() must set a terminal status, not RUNNING")
        # notes is COALESCE-merged so the caller can pass None to keep
        # the existing notes (set at open() time).
        self._conn.execute(
            text("""
                UPDATE fundamentals_run
                SET status = :st,
                    succeeded = :s,
                    failed = :f,
                    skipped = :sk,
                    completed_at = NOW(),
                    notes = COALESCE(:notes, notes)
                WHERE run_id = :rid
            """),
            {"rid": run_id, "st": status, "s": succeeded,
             "f": failed, "sk": skipped, "notes": notes},
        )


def _run_from_row(row) -> FundamentalsRun:
    pj = row[9]
    # psycopg2 returns JSONB as already-parsed dict.
    if isinstance(pj, str):
        try:
            pj = json.loads(pj)
        except json.JSONDecodeError:
            pj = None
    return FundamentalsRun(
        run_id=row[0],
        phase=row[1],
        exchange=row[2],
        started_at=row[3],
        completed_at=row[4],
        status=row[5],
        succeeded=int(row[6]),
        failed=int(row[7]),
        skipped=int(row[8]),
        progress_json=pj,
        notes=row[10],
    )


# ---------- InstrumentSharesOutstanding (Phase 2) ---------- #


class InstrumentSharesRepository:
    """Time-series shares-outstanding per ticker.

    Composite PK is (ticker_id, as_of_date) — every refresh inserts
    a new snapshot. Reads default to "latest"; the time-series shape
    is used by Phase 4 for historical market cap and the (deferred)
    XBRL sidecar for filling in historical filing rows.

    Pattern matches InstrumentIndustryRepository: connection in
    constructor, no transaction management, returns domain dataclasses.
    """

    def __init__(self, conn: Connection):
        self._conn = conn

    # ----- reads ----- #

    def get_latest(self, ticker_id: int):
        """The most-recent snapshot for this ticker. None if no rows."""
        from ..fundamentals import SharesOutstanding  # local to avoid cycle
        row = self._conn.execute(
            text("""
                SELECT ticker_id, as_of_date, shares_outstanding,
                       free_float_factor, shares_source, fetched_at, xbrl_url
                FROM instrument_shares
                WHERE ticker_id = :tid
                ORDER BY as_of_date DESC
                LIMIT 1
            """),
            {"tid": ticker_id},
        ).fetchone()
        if row is None:
            return None
        return _shares_from_row(row)

    def get_as_of(self, ticker_id: int, as_of: "date"):
        """The latest snapshot at-or-before `as_of`. Used by Phase 4
        to find shares-outstanding for a historical trade date."""
        row = self._conn.execute(
            text("""
                SELECT ticker_id, as_of_date, shares_outstanding,
                       free_float_factor, shares_source, fetched_at, xbrl_url
                FROM instrument_shares
                WHERE ticker_id = :tid AND as_of_date <= :asof
                ORDER BY as_of_date DESC
                LIMIT 1
            """),
            {"tid": ticker_id, "asof": as_of},
        ).fetchone()
        return _shares_from_row(row) if row else None

    def get_at(self, ticker_id: int, as_of: "date"):
        """The exact snapshot at `as_of` if one exists, else None.
        Used by the runner to test "have we already written today's
        row?" before re-fetching."""
        row = self._conn.execute(
            text("""
                SELECT ticker_id, as_of_date, shares_outstanding,
                       free_float_factor, shares_source, fetched_at, xbrl_url
                FROM instrument_shares
                WHERE ticker_id = :tid AND as_of_date = :asof
            """),
            {"tid": ticker_id, "asof": as_of},
        ).fetchone()
        return _shares_from_row(row) if row else None

    def list_history(self, ticker_id: int, *, limit: int = 100) -> list:
        """All snapshots for this ticker, newest first."""
        rows = self._conn.execute(
            text("""
                SELECT ticker_id, as_of_date, shares_outstanding,
                       free_float_factor, shares_source, fetched_at, xbrl_url
                FROM instrument_shares
                WHERE ticker_id = :tid
                ORDER BY as_of_date DESC
                LIMIT :lim
            """),
            {"tid": ticker_id, "lim": limit},
        )
        return [_shares_from_row(r) for r in rows]

    def list_latest_for_exchange(self, exchange: str) -> list:
        """Latest snapshot per ticker for the exchange. Uses
        DISTINCT ON which is more efficient than the windowed
        equivalent in Postgres.

        Joined against `tickers` to scope by exchange and to filter
        to active tickers (delisted tickers' shares are still in the
        table for historical lookups, but the "current view"
        excludes them)."""
        rows = self._conn.execute(text("""
            SELECT DISTINCT ON (s.ticker_id)
                   s.ticker_id, s.as_of_date, s.shares_outstanding,
                   s.free_float_factor, s.shares_source, s.fetched_at,
                   s.xbrl_url
            FROM instrument_shares s
            JOIN tickers t ON t.ticker_id = s.ticker_id
            WHERE t.exchange = :ex AND t.is_active = TRUE
            ORDER BY s.ticker_id, s.as_of_date DESC
        """), {"ex": exchange})
        return [_shares_from_row(r) for r in rows]

    def coverage_counts_by_type(self) -> list[tuple[str, str, int, int]]:
        """Per-(exchange, instrument_type) (total, with_shares).

        `total` counts active tickers; `with_shares` counts those
        that have AT LEAST ONE non-NULL shares_outstanding row.
        Same shape as InstrumentIndustryRepository.coverage_counts_by_type
        so fundamentals-status can render both side by side.

        The COUNT(DISTINCT t.ticker_id) on `total` is important — a
        LEFT JOIN against the time-series instrument_shares table
        produces one joined row per (ticker, snapshot), so a naive
        COUNT(*) over-counts tickers that have many snapshots.
        """
        rows = self._conn.execute(text("""
            SELECT
                t.exchange,
                t.instrument_type,
                COUNT(DISTINCT t.ticker_id) AS total,
                COUNT(DISTINCT s.ticker_id) FILTER (
                    WHERE s.shares_outstanding IS NOT NULL
                ) AS with_shares
            FROM tickers t
            LEFT JOIN instrument_shares s ON s.ticker_id = t.ticker_id
            WHERE t.is_active
            GROUP BY t.exchange, t.instrument_type
            ORDER BY t.exchange, t.instrument_type
        """))
        return [
            (r[0], r[1], int(r[2]), int(r[3]))
            for r in rows
        ]

    # ----- writes ----- #

    def insert_snapshot(
        self,
        ticker_id: int,
        *,
        as_of_date,
        shares_outstanding,
        shares_source: str,
        free_float_factor=None,
        xbrl_url=None,
    ) -> None:
        """Insert a snapshot row. Idempotent on (ticker_id, as_of_date):
        if a row already exists for that date, this is an UPDATE.

        The idempotency is intentional — a same-day re-run shouldn't
        fail with a PK violation; it should overwrite, since the
        re-run presumably has fresher data.

        shares_outstanding may be NULL (record-of-attempt) but
        shares_source is always required so we know the provenance
        of the attempt.
        """
        from ..fundamentals import SHARES_SOURCES
        if shares_source not in SHARES_SOURCES:
            raise ValueError(
                f"shares_source must be one of {SHARES_SOURCES}, "
                f"got {shares_source!r}"
            )
        self._conn.execute(
            text("""
                INSERT INTO instrument_shares
                    (ticker_id, as_of_date, shares_outstanding,
                     free_float_factor, shares_source, fetched_at, xbrl_url)
                VALUES (:tid, :asof, :so, :ff, :src, NOW(), :url)
                ON CONFLICT (ticker_id, as_of_date) DO UPDATE SET
                    shares_outstanding = EXCLUDED.shares_outstanding,
                    free_float_factor = EXCLUDED.free_float_factor,
                    shares_source = EXCLUDED.shares_source,
                    fetched_at = NOW(),
                    xbrl_url = EXCLUDED.xbrl_url
            """),
            {"tid": ticker_id, "asof": as_of_date,
             "so": shares_outstanding, "ff": free_float_factor,
             "src": shares_source, "url": xbrl_url},
        )

    def delete_snapshot(self, ticker_id: int, as_of_date) -> bool:
        """Remove one snapshot. Used by tests; not exposed in the CLI.
        Operator overrides are NOT done via delete — they use
        shares_source='MANUAL_OVERRIDE' on a fresh insert."""
        result = self._conn.execute(
            text("""
                DELETE FROM instrument_shares
                WHERE ticker_id = :tid AND as_of_date = :asof
            """),
            {"tid": ticker_id, "asof": as_of_date},
        )
        return result.rowcount > 0


def _shares_from_row(row):
    from ..fundamentals import SharesOutstanding
    return SharesOutstanding(
        ticker_id=row[0],
        as_of_date=row[1],
        shares_outstanding=int(row[2]) if row[2] is not None else None,
        free_float_factor=float(row[3]) if row[3] is not None else None,
        shares_source=row[4],
        fetched_at=row[5],
        xbrl_url=row[6],
    )


# ---------- QuarterlyFinancial (Phase 3) ---------- #


class QuarterlyFinancialsRepository:
    """Time-series quarterly revenue + EBITDA per ticker.

    Composite PK is (ticker_id, fiscal_quarter, fetched_at). Reads
    default to the latest fetched_at per (ticker, quarter) via
    DISTINCT ON; the history of a single (ticker, quarter) pair
    is exposed via get_history for restatement detection.

    Pattern matches InstrumentSharesRepository: connection in
    constructor, no transaction management, returns domain
    dataclasses.
    """

    def __init__(self, conn: Connection):
        self._conn = conn

    # ----- reads ----- #

    def get_latest(self, ticker_id: int, fiscal_quarter: "date"):
        """The most-recent snapshot for (ticker, quarter). None if
        the runner never recorded anything for this pair."""
        row = self._conn.execute(
            text("""
                SELECT ticker_id, fiscal_quarter, fetched_at,
                       revenue, ebitda, currency, source
                FROM quarterly_financials
                WHERE ticker_id = :tid AND fiscal_quarter = :fq
                ORDER BY fetched_at DESC
                LIMIT 1
            """),
            {"tid": ticker_id, "fq": fiscal_quarter},
        ).fetchone()
        return _qfinancial_from_row(row) if row else None

    def get_latest_n_quarters(self, ticker_id: int, n: int = 4) -> list:
        """Latest snapshots for the N most-recent quarters this
        ticker has been refreshed for. Newest quarter first.

        This is the Phase 7 "show me Q-4 .. Q-1 revenue and EBITDA"
        query. It assumes every (ticker, quarter) pair has at least
        one row — gaps in the time series surface as missing
        quarters.
        """
        rows = self._conn.execute(
            text("""
                SELECT DISTINCT ON (fiscal_quarter)
                       ticker_id, fiscal_quarter, fetched_at,
                       revenue, ebitda, currency, source
                FROM quarterly_financials
                WHERE ticker_id = :tid
                ORDER BY fiscal_quarter DESC, fetched_at DESC
                LIMIT :n
            """),
            {"tid": ticker_id, "n": n},
        )
        return [_qfinancial_from_row(r) for r in rows]

    def get_history(self, ticker_id: int, fiscal_quarter: "date") -> list:
        """All snapshots for one (ticker, quarter) pair, newest first.

        Used for restatement detection: if length > 1, the number
        has been reported differently at different times. Phase 3
        ships with this query available but no analytics built on
        top — it's reserved for the (future) data-quality tooling.
        """
        rows = self._conn.execute(
            text("""
                SELECT ticker_id, fiscal_quarter, fetched_at,
                       revenue, ebitda, currency, source
                FROM quarterly_financials
                WHERE ticker_id = :tid AND fiscal_quarter = :fq
                ORDER BY fetched_at DESC
            """),
            {"tid": ticker_id, "fq": fiscal_quarter},
        )
        return [_qfinancial_from_row(r) for r in rows]

    def list_latest_for_exchange(
        self, exchange: str, *, fiscal_quarter: Optional["date"] = None,
    ) -> list:
        """Latest snapshot per (ticker, quarter) for active tickers
        on `exchange`. If `fiscal_quarter` is given, scopes to that
        quarter; otherwise returns the latest quarter per ticker.

        Phase 7 will call this with the most recent quarter date to
        show a universe-wide snapshot.
        """
        if fiscal_quarter is None:
            # Latest known quarter per ticker, with latest fetched_at
            # within that. DISTINCT ON is the cleanest way to express
            # the per-ticker max in Postgres.
            sql = """
                SELECT DISTINCT ON (qf.ticker_id)
                       qf.ticker_id, qf.fiscal_quarter, qf.fetched_at,
                       qf.revenue, qf.ebitda, qf.currency, qf.source
                FROM quarterly_financials qf
                JOIN tickers t ON t.ticker_id = qf.ticker_id
                WHERE t.exchange = :ex AND t.is_active = TRUE
                ORDER BY qf.ticker_id, qf.fiscal_quarter DESC, qf.fetched_at DESC
            """
            params = {"ex": exchange}
        else:
            sql = """
                SELECT DISTINCT ON (qf.ticker_id)
                       qf.ticker_id, qf.fiscal_quarter, qf.fetched_at,
                       qf.revenue, qf.ebitda, qf.currency, qf.source
                FROM quarterly_financials qf
                JOIN tickers t ON t.ticker_id = qf.ticker_id
                WHERE t.exchange = :ex AND t.is_active = TRUE
                  AND qf.fiscal_quarter = :fq
                ORDER BY qf.ticker_id, qf.fetched_at DESC
            """
            params = {"ex": exchange, "fq": fiscal_quarter}
        rows = self._conn.execute(text(sql), params)
        return [_qfinancial_from_row(r) for r in rows]

    def coverage_counts_by_type(self) -> list[tuple[str, str, int, int, int]]:
        """Per-(exchange, instrument_type) (total, with_revenue,
        with_ebitda) tuples.

        Used by fundamentals-status. The COUNT(DISTINCT ...) on each
        column matters: a LEFT JOIN against the time-series table
        produces one row per (ticker, quarter, fetched_at) and naive
        COUNT(*) would multiply by history depth.
        """
        rows = self._conn.execute(text("""
            SELECT
                t.exchange,
                t.instrument_type,
                COUNT(DISTINCT t.ticker_id) AS total,
                COUNT(DISTINCT qf.ticker_id) FILTER (
                    WHERE qf.revenue IS NOT NULL
                ) AS with_revenue,
                COUNT(DISTINCT qf.ticker_id) FILTER (
                    WHERE qf.ebitda IS NOT NULL
                ) AS with_ebitda
            FROM tickers t
            LEFT JOIN quarterly_financials qf ON qf.ticker_id = t.ticker_id
            WHERE t.is_active
            GROUP BY t.exchange, t.instrument_type
            ORDER BY t.exchange, t.instrument_type
        """))
        return [
            (r[0], r[1], int(r[2]), int(r[3]), int(r[4]))
            for r in rows
        ]

    # ----- writes ----- #

    def insert_snapshot(
        self,
        ticker_id: int,
        *,
        fiscal_quarter: "date",
        revenue: Optional[float],
        ebitda: Optional[float],
        currency: str = "INR",
        source: str = "YFINANCE",
    ) -> None:
        """Insert a snapshot row. The PK is (ticker_id, fiscal_quarter,
        fetched_at), and fetched_at is NOW() — so same-millisecond
        inserts of the same (ticker, quarter) would collide, but in
        practice the runner sleeps 2s between tickers so this never
        happens.

        Both revenue and ebitda may be NULL — record-of-attempt
        rather than skipping the write entirely. The next refresh
        sees "we tried and got nothing" and can decide policy.
        """
        from ..fundamentals import FINANCIAL_SOURCES
        if source not in FINANCIAL_SOURCES:
            raise ValueError(
                f"source must be one of {FINANCIAL_SOURCES}, got {source!r}"
            )
        self._conn.execute(
            text("""
                INSERT INTO quarterly_financials
                    (ticker_id, fiscal_quarter, fetched_at,
                     revenue, ebitda, currency, source)
                VALUES (:tid, :fq, NOW(), :rev, :ebt, :cur, :src)
            """),
            {"tid": ticker_id, "fq": fiscal_quarter,
             "rev": revenue, "ebt": ebitda,
             "cur": currency, "src": source},
        )

    def delete_for_ticker(self, ticker_id: int) -> int:
        """Remove all rows for a ticker. Used by tests; not exposed
        in the CLI. Returns count deleted."""
        result = self._conn.execute(
            text("DELETE FROM quarterly_financials WHERE ticker_id = :tid"),
            {"tid": ticker_id},
        )
        return result.rowcount


def _qfinancial_from_row(row):
    from ..fundamentals import QuarterlyFinancial
    return QuarterlyFinancial(
        ticker_id=row[0],
        fiscal_quarter=row[1],
        fetched_at=row[2],
        revenue=float(row[3]) if row[3] is not None else None,
        ebitda=float(row[4]) if row[4] is not None else None,
        currency=row[5],
        source=row[6],
    )


# ---------- DailyMarketCap (Phase 4A) ---------- #


class DailyMarketCapRepository:
    """Daily per-ticker market cap.

    PK is (ticker_id, trade_date) — same shape as ohlcv_bars but
    aggregated to daily-only resolution and combined with shares
    data. Reads default to "latest" per ticker; the
    list_for_date(date, exchange) query is the Phase 4B hot path
    (top-N by mcap on a specific date).

    Pattern matches InstrumentSharesRepository: connection in
    constructor, no transaction management, returns domain
    dataclasses.

    A few non-obvious semantics:
      - upsert_snapshot is idempotent on (ticker_id, trade_date).
        Re-running the compute for a date overwrites whatever was
        previously stored. This is intentional — if the operator
        re-runs because shares-outstanding was corrected, the
        newer COMPUTED mcap should replace the older one.
      - shares_used is required to be NULL when source is
        YFINANCE_HIST_MCAP and required to be NOT NULL otherwise.
        We don't enforce that at the DB level (would need a complex
        CHECK constraint); the runner enforces it via type
        signatures.
    """

    def __init__(self, conn: Connection):
        self._conn = conn

    # ----- reads ----- #

    def get_latest(self, ticker_id: int):
        """The most recent trade_date row for this ticker. None if
        no rows. Used by analytics for "what's the current mcap"."""
        row = self._conn.execute(
            text("""
                SELECT ticker_id, trade_date, close_price, shares_used,
                       market_cap, source, computed_at
                FROM daily_market_cap
                WHERE ticker_id = :tid
                ORDER BY trade_date DESC
                LIMIT 1
            """),
            {"tid": ticker_id},
        ).fetchone()
        return _mcap_from_row(row) if row else None

    def get_at(self, ticker_id: int, trade_date: "date"):
        """The row for an exact (ticker, date) pair, or None.

        Used by the runner to skip-if-already-computed (re-running
        a date is expensive if it triggers yfinance calls). Also
        used by tests."""
        row = self._conn.execute(
            text("""
                SELECT ticker_id, trade_date, close_price, shares_used,
                       market_cap, source, computed_at
                FROM daily_market_cap
                WHERE ticker_id = :tid AND trade_date = :td
            """),
            {"tid": ticker_id, "td": trade_date},
        ).fetchone()
        return _mcap_from_row(row) if row else None

    def list_for_date(
        self, trade_date: "date", *, exchange: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list:
        """All market cap rows for a specific trade_date, ordered
        by market_cap DESC. Optionally scoped to a single exchange
        and/or top-N.

        This is the Phase 4B hot path for "top 20 stocks by mcap
        in industry X on rebalance date Y" — Phase 4B will further
        filter by industry; the repo just provides the date-axis
        scan.

        The (trade_date, market_cap DESC) index covers this; no
        sort needed at the DB level."""
        if exchange is None:
            sql = """
                SELECT mc.ticker_id, mc.trade_date, mc.close_price,
                       mc.shares_used, mc.market_cap, mc.source, mc.computed_at
                FROM daily_market_cap mc
                WHERE mc.trade_date = :td
                ORDER BY mc.market_cap DESC
            """
            params = {"td": trade_date}
        else:
            sql = """
                SELECT mc.ticker_id, mc.trade_date, mc.close_price,
                       mc.shares_used, mc.market_cap, mc.source, mc.computed_at
                FROM daily_market_cap mc
                JOIN tickers t ON t.ticker_id = mc.ticker_id
                WHERE mc.trade_date = :td AND t.exchange = :ex
                ORDER BY mc.market_cap DESC
            """
            params = {"td": trade_date, "ex": exchange}
        if limit is not None:
            sql += " LIMIT :lim"
            params["lim"] = limit
        rows = self._conn.execute(text(sql), params)
        return [_mcap_from_row(r) for r in rows]

    def list_history(self, ticker_id: int, *, limit: int = 365) -> list:
        """All daily mcap rows for one ticker, newest first.

        Default limit is 365 days — covers a year of backtest.
        Used for analytics that want to inspect mcap drift for a
        specific name. Phase 4B's index computation does not use
        this — it walks dates with list_for_date."""
        rows = self._conn.execute(
            text("""
                SELECT ticker_id, trade_date, close_price, shares_used,
                       market_cap, source, computed_at
                FROM daily_market_cap
                WHERE ticker_id = :tid
                ORDER BY trade_date DESC
                LIMIT :lim
            """),
            {"tid": ticker_id, "lim": limit},
        )
        return [_mcap_from_row(r) for r in rows]

    def coverage_counts_by_date(
        self, *, exchange: Optional[str] = None,
    ) -> list[tuple["date", int, int, int]]:
        """For each trade_date that has mcap rows, return
        (date, total, computed, hist_mcap) — where:
          total       = total mcap rows on that date
          computed    = rows with source=COMPUTED
          hist_mcap   = rows with source=YFINANCE_HIST_MCAP

        Used by `fundamentals-status` to show backfill coverage.
        Newest dates first; caller typically limits to the latest
        ~30 dates."""
        if exchange is None:
            sql = """
                SELECT
                    mc.trade_date,
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE mc.source = 'COMPUTED') AS computed,
                    COUNT(*) FILTER (WHERE mc.source = 'YFINANCE_HIST_MCAP') AS hist
                FROM daily_market_cap mc
                GROUP BY mc.trade_date
                ORDER BY mc.trade_date DESC
            """
            params = {}
        else:
            sql = """
                SELECT
                    mc.trade_date,
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE mc.source = 'COMPUTED') AS computed,
                    COUNT(*) FILTER (WHERE mc.source = 'YFINANCE_HIST_MCAP') AS hist
                FROM daily_market_cap mc
                JOIN tickers t ON t.ticker_id = mc.ticker_id
                WHERE t.exchange = :ex
                GROUP BY mc.trade_date
                ORDER BY mc.trade_date DESC
            """
            params = {"ex": exchange}
        rows = self._conn.execute(text(sql), params)
        return [(r[0], int(r[1]), int(r[2]), int(r[3])) for r in rows]

    # ----- writes ----- #

    def upsert_snapshot(
        self,
        ticker_id: int,
        *,
        trade_date: "date",
        close_price: float,
        shares_used: Optional[int],
        market_cap: float,
        source: str,
    ) -> None:
        """Insert or update a mcap row. Idempotent on (ticker_id,
        trade_date) via ON CONFLICT DO UPDATE.

        Validates that:
          - source is in MARKET_CAP_SOURCES.
          - market_cap >= 0.
          - For COMPUTED / APPROX_TODAYS_SHARES / MANUAL_OVERRIDE,
            shares_used must NOT be None (we use it for the
            derivation).
          - For YFINANCE_HIST_MCAP, shares_used should be None
            (yfinance doesn't expose it). We accept non-None and
            store as-is; this is a soft contract, not enforced."""
        from ..fundamentals import MARKET_CAP_SOURCES
        if source not in MARKET_CAP_SOURCES:
            raise ValueError(
                f"source must be one of {MARKET_CAP_SOURCES}, "
                f"got {source!r}"
            )
        if market_cap < 0:
            raise ValueError(
                f"market_cap must be >= 0, got {market_cap!r}"
            )
        if source in ("COMPUTED", "APPROX_TODAYS_SHARES",
                      "MANUAL_OVERRIDE") and shares_used is None:
            raise ValueError(
                f"shares_used is required when source={source!r}; "
                f"it is the multiplicand used to derive market_cap"
            )

        self._conn.execute(
            text("""
                INSERT INTO daily_market_cap
                    (ticker_id, trade_date, close_price, shares_used,
                     market_cap, source, computed_at)
                VALUES (:tid, :td, :cp, :su, :mc, :src, NOW())
                ON CONFLICT (ticker_id, trade_date) DO UPDATE SET
                    close_price = EXCLUDED.close_price,
                    shares_used = EXCLUDED.shares_used,
                    market_cap = EXCLUDED.market_cap,
                    source = EXCLUDED.source,
                    computed_at = NOW()
            """),
            {"tid": ticker_id, "td": trade_date, "cp": close_price,
             "su": shares_used, "mc": market_cap, "src": source},
        )

    def delete_for_date(self, trade_date: "date") -> int:
        """Remove all mcap rows for one trade_date. Used by tests
        and by the runner's "wipe and rebuild this date" admin
        path. Returns count deleted."""
        result = self._conn.execute(
            text("DELETE FROM daily_market_cap WHERE trade_date = :td"),
            {"td": trade_date},
        )
        return result.rowcount

    def delete_for_ticker(self, ticker_id: int) -> int:
        """Remove all mcap rows for one ticker. Used by tests."""
        result = self._conn.execute(
            text(
                "DELETE FROM daily_market_cap WHERE ticker_id = :tid"
            ),
            {"tid": ticker_id},
        )
        return result.rowcount


def _mcap_from_row(row):
    from ..fundamentals import DailyMarketCap
    return DailyMarketCap(
        ticker_id=row[0],
        trade_date=row[1],
        close_price=float(row[2]),
        shares_used=int(row[3]) if row[3] is not None else None,
        market_cap=float(row[4]),
        source=row[5],
        computed_at=row[6],
    )


# ---------- IndustryIndex (Phase 4B-1) ---------- #


class IndustryIndexRepository:
    """Index definitions. PK is industry_name (operator's choice
    per §20 design decision).

    Same connection-in-constructor pattern as the other Phase 1-4
    repositories.
    """

    def __init__(self, conn: Connection):
        self._conn = conn

    # ----- reads ----- #

    def get(self, industry_name: str):
        row = self._conn.execute(
            text("""
                SELECT industry_name, sector_name, methodology,
                       base_value, base_date, rebalance_schedule,
                       min_constituents, status, notes
                FROM industry_index
                WHERE industry_name = :n
            """),
            {"n": industry_name},
        ).fetchone()
        return _index_from_row(row) if row else None

    def list_active(self) -> list:
        rows = self._conn.execute(text("""
            SELECT industry_name, sector_name, methodology,
                   base_value, base_date, rebalance_schedule,
                   min_constituents, status, notes
            FROM industry_index
            WHERE status = 'ACTIVE'
            ORDER BY industry_name
        """))
        return [_index_from_row(r) for r in rows]

    def list_all(self) -> list:
        """Includes RETIRED indices. Used by audit / status views
        rather than analytics."""
        rows = self._conn.execute(text("""
            SELECT industry_name, sector_name, methodology,
                   base_value, base_date, rebalance_schedule,
                   min_constituents, status, notes
            FROM industry_index
            ORDER BY status, industry_name
        """))
        return [_index_from_row(r) for r in rows]

    # ----- writes ----- #

    def upsert(
        self, industry_name: str, *,
        sector_name: str,
        base_date: "date",
        base_value: float = 1000.0,
        methodology: str = "MODIFIED_LASPEYRES",
        rebalance_schedule: str = "SEMIANNUAL_APR_OCT",
        min_constituents: int = 10,
        notes: Optional[str] = None,
    ) -> None:
        """Insert or update an index. Idempotent on industry_name.

        The auto-discovery runner (Phase 4B-2) uses INSERT ...
        ON CONFLICT DO NOTHING semantics by checking get() first
        — once an index is created, its base_date and base_value
        are immutable. This method allows overwriting for
        administrative scenarios (e.g. operator changes the
        threshold), and validation happens via the DB CHECK
        constraints + this method's pre-checks.
        """
        from ..fundamentals import (
            INDEX_METHODOLOGIES, REBALANCE_SCHEDULES,
        )
        if methodology not in INDEX_METHODOLOGIES:
            raise ValueError(
                f"methodology must be one of {INDEX_METHODOLOGIES}, "
                f"got {methodology!r}"
            )
        if rebalance_schedule not in REBALANCE_SCHEDULES:
            raise ValueError(
                f"rebalance_schedule must be one of {REBALANCE_SCHEDULES}, "
                f"got {rebalance_schedule!r}"
            )
        if base_value <= 0:
            raise ValueError("base_value must be > 0")
        if min_constituents < 1:
            raise ValueError("min_constituents must be >= 1")
        self._conn.execute(
            text("""
                INSERT INTO industry_index
                    (industry_name, sector_name, methodology,
                     base_value, base_date, rebalance_schedule,
                     min_constituents, status, notes)
                VALUES
                    (:n, :s, :m, :bv, :bd, :rs, :mc, 'ACTIVE', :nt)
                ON CONFLICT (industry_name) DO UPDATE SET
                    sector_name = EXCLUDED.sector_name,
                    methodology = EXCLUDED.methodology,
                    base_value = EXCLUDED.base_value,
                    base_date = EXCLUDED.base_date,
                    rebalance_schedule = EXCLUDED.rebalance_schedule,
                    min_constituents = EXCLUDED.min_constituents,
                    notes = EXCLUDED.notes
            """),
            {"n": industry_name, "s": sector_name, "m": methodology,
             "bv": base_value, "bd": base_date, "rs": rebalance_schedule,
             "mc": min_constituents, "nt": notes},
        )

    def set_status(self, industry_name: str, status: str) -> bool:
        """Change ACTIVE / RETIRED. Returns False if the index
        doesn't exist."""
        from ..fundamentals import INDEX_STATUSES
        if status not in INDEX_STATUSES:
            raise ValueError(
                f"status must be one of {INDEX_STATUSES}, got {status!r}"
            )
        result = self._conn.execute(
            text("""
                UPDATE industry_index
                SET status = :s
                WHERE industry_name = :n
            """),
            {"n": industry_name, "s": status},
        )
        return result.rowcount > 0

    def delete(self, industry_name: str) -> bool:
        """Remove an index and ALL its constituents and values via
        ON DELETE CASCADE. Used by tests and by an operator
        admin path; normal lifecycle uses set_status('RETIRED')."""
        result = self._conn.execute(
            text("DELETE FROM industry_index WHERE industry_name = :n"),
            {"n": industry_name},
        )
        return result.rowcount > 0


def _index_from_row(row):
    from ..fundamentals import IndustryIndex
    return IndustryIndex(
        industry_name=row[0],
        sector_name=row[1],
        methodology=row[2],
        base_value=float(row[3]),
        base_date=row[4],
        rebalance_schedule=row[5],
        min_constituents=int(row[6]),
        status=row[7],
        notes=row[8],
    )


# ---------- IndustryIndexConstituent (Phase 4B-1) ---------- #


class IndustryIndexConstituentRepository:
    """Membership-with-history for an industry index.

    The hot read paths are:
      - list_active_for_index(industry_name) — current
        constituents (effective_to IS NULL).
      - list_as_of(industry_name, trade_date) — constituents
        active on a specific historical date.

    Writes happen in two contexts:
      - At each rebalance: insert_constituent() for each picked
        ticker.
      - At each non-first rebalance (and on mid-cycle delisting):
        close_period() on the prior constituent rows.
    """

    def __init__(self, conn: Connection):
        self._conn = conn

    # ----- reads ----- #

    def list_active_for_index(self, industry_name: str) -> list:
        """Currently-active constituents. effective_to IS NULL."""
        rows = self._conn.execute(text("""
            SELECT industry_name, ticker_id, rebalance_date,
                   effective_from, effective_to,
                   weight_at_rebalance, market_cap_at_rebalance,
                   close_at_rebalance, removed_reason
            FROM industry_index_constituent
            WHERE industry_name = :n AND effective_to IS NULL
            ORDER BY weight_at_rebalance DESC
        """), {"n": industry_name})
        return [_constituent_from_row(r) for r in rows]

    def list_as_of(self, industry_name: str, trade_date: "date") -> list:
        """Constituents whose effective period contains trade_date.
        Used by the compute layer to figure out who was in the
        index on a given day.
        """
        rows = self._conn.execute(text("""
            SELECT industry_name, ticker_id, rebalance_date,
                   effective_from, effective_to,
                   weight_at_rebalance, market_cap_at_rebalance,
                   close_at_rebalance, removed_reason
            FROM industry_index_constituent
            WHERE industry_name = :n
              AND effective_from <= :td
              AND (effective_to IS NULL OR effective_to >= :td)
            ORDER BY weight_at_rebalance DESC
        """), {"n": industry_name, "td": trade_date})
        return [_constituent_from_row(r) for r in rows]

    def list_history_for_ticker(
        self, industry_name: str, ticker_id: int,
    ) -> list:
        """Every effective period this ticker has been a
        constituent of this index. Used for audit / debugging
        when "why isn't TCS in the IT index now" comes up."""
        rows = self._conn.execute(text("""
            SELECT industry_name, ticker_id, rebalance_date,
                   effective_from, effective_to,
                   weight_at_rebalance, market_cap_at_rebalance,
                   close_at_rebalance, removed_reason
            FROM industry_index_constituent
            WHERE industry_name = :n AND ticker_id = :tid
            ORDER BY effective_from DESC
        """), {"n": industry_name, "tid": ticker_id})
        return [_constituent_from_row(r) for r in rows]

    def list_rebalances(self, industry_name: str) -> list["date"]:
        """All distinct rebalance dates for this index, newest
        first. Used for audit and for the value-computation logic
        that needs to know which rebalance applies to date D."""
        rows = self._conn.execute(text("""
            SELECT DISTINCT rebalance_date
            FROM industry_index_constituent
            WHERE industry_name = :n
            ORDER BY rebalance_date DESC
        """), {"n": industry_name})
        return [r[0] for r in rows]

    # ----- writes ----- #

    def insert_constituent(
        self, industry_name: str, *,
        ticker_id: int,
        rebalance_date: "date",
        weight_at_rebalance: float,
        market_cap_at_rebalance: float,
        close_at_rebalance: float,
    ) -> None:
        """Open a new effective period for this ticker as a
        constituent of this index. effective_from = rebalance_date;
        effective_to = NULL.

        The runner ensures there's no overlapping prior period for
        this (industry, ticker) by calling close_period first
        when needed; we don't enforce that here at the DB level."""
        if not (0 <= weight_at_rebalance <= 1):
            raise ValueError(
                f"weight_at_rebalance must be in [0, 1], "
                f"got {weight_at_rebalance!r}"
            )
        if market_cap_at_rebalance <= 0:
            raise ValueError("market_cap_at_rebalance must be > 0")
        if close_at_rebalance <= 0:
            raise ValueError("close_at_rebalance must be > 0")
        self._conn.execute(text("""
            INSERT INTO industry_index_constituent
                (industry_name, ticker_id, rebalance_date,
                 effective_from, effective_to,
                 weight_at_rebalance, market_cap_at_rebalance,
                 close_at_rebalance, removed_reason)
            VALUES (:n, :tid, :rd, :ef, NULL,
                    :w, :mc, :cl, NULL)
        """), {"n": industry_name, "tid": ticker_id,
               "rd": rebalance_date, "ef": rebalance_date,
               "w": weight_at_rebalance,
               "mc": market_cap_at_rebalance,
               "cl": close_at_rebalance})

    def close_period(
        self, industry_name: str, ticker_id: int,
        effective_to: "date", removed_reason: str,
    ) -> bool:
        """Close the currently-open effective period (the row with
        effective_to IS NULL) for this (industry, ticker). Returns
        False if there was no open period.

        Used at rebalance time (REBALANCE_EXIT) and on mid-cycle
        delistings (DELISTED). Operator admin path uses
        OPERATOR_REMOVED."""
        from ..fundamentals import CONSTITUENT_REMOVED_REASONS
        if removed_reason not in CONSTITUENT_REMOVED_REASONS:
            raise ValueError(
                f"removed_reason must be one of "
                f"{CONSTITUENT_REMOVED_REASONS}, got {removed_reason!r}"
            )
        result = self._conn.execute(text("""
            UPDATE industry_index_constituent
            SET effective_to = :et, removed_reason = :rr
            WHERE industry_name = :n
              AND ticker_id = :tid
              AND effective_to IS NULL
        """), {"n": industry_name, "tid": ticker_id,
               "et": effective_to, "rr": removed_reason})
        return result.rowcount > 0


def _constituent_from_row(row):
    from ..fundamentals import IndustryIndexConstituent
    return IndustryIndexConstituent(
        industry_name=row[0],
        ticker_id=int(row[1]),
        rebalance_date=row[2],
        effective_from=row[3],
        effective_to=row[4],
        weight_at_rebalance=float(row[5]),
        market_cap_at_rebalance=float(row[6]),
        close_at_rebalance=float(row[7]),
        removed_reason=row[8],
    )


# ---------- IndustryIndexValue (Phase 4B-1) ---------- #


class IndustryIndexValueRepository:
    """Daily index values output by the compute layer.

    PK is (industry_name, trade_date). Two read hot paths:
      - get_history(industry_name) for time-series analytics.
      - list_for_date(trade_date) for cross-industry queries.
    """

    def __init__(self, conn: Connection):
        self._conn = conn

    # ----- reads ----- #

    def get_at(self, industry_name: str, trade_date: "date"):
        row = self._conn.execute(text("""
            SELECT industry_name, trade_date, value,
                   constituents_used, constituents_missing,
                   renormalized, effective_rebalance_date, computed_at
            FROM industry_index_value
            WHERE industry_name = :n AND trade_date = :td
        """), {"n": industry_name, "td": trade_date}).fetchone()
        return _value_from_row(row) if row else None

    def get_latest(self, industry_name: str):
        row = self._conn.execute(text("""
            SELECT industry_name, trade_date, value,
                   constituents_used, constituents_missing,
                   renormalized, effective_rebalance_date, computed_at
            FROM industry_index_value
            WHERE industry_name = :n
            ORDER BY trade_date DESC
            LIMIT 1
        """), {"n": industry_name}).fetchone()
        return _value_from_row(row) if row else None

    def get_history(
        self, industry_name: str, *,
        from_date: Optional["date"] = None,
        to_date: Optional["date"] = None,
        limit: Optional[int] = None,
    ) -> list:
        """Time-series history for one index. Optional date-range
        filter; defaults to all history newest-first.

        Phase 5+ indicator stack will use this with a date-range
        filter to pull just the lookback window it needs.
        """
        conditions = ["industry_name = :n"]
        params: dict = {"n": industry_name}
        if from_date is not None:
            conditions.append("trade_date >= :fd")
            params["fd"] = from_date
        if to_date is not None:
            conditions.append("trade_date <= :td")
            params["td"] = to_date
        sql = f"""
            SELECT industry_name, trade_date, value,
                   constituents_used, constituents_missing,
                   renormalized, effective_rebalance_date, computed_at
            FROM industry_index_value
            WHERE {' AND '.join(conditions)}
            ORDER BY trade_date DESC
        """
        if limit is not None:
            sql += " LIMIT :lim"
            params["lim"] = limit
        rows = self._conn.execute(text(sql), params)
        return [_value_from_row(r) for r in rows]

    def list_for_date(self, trade_date: "date") -> list:
        """All industries' index values on a specific date.
        Sorted alphabetically — at this layer we don't impose a
        ranking. Cross-industry "show me today's sector movers"
        analytics happen above this in Phase 5+.
        """
        rows = self._conn.execute(text("""
            SELECT industry_name, trade_date, value,
                   constituents_used, constituents_missing,
                   renormalized, effective_rebalance_date, computed_at
            FROM industry_index_value
            WHERE trade_date = :td
            ORDER BY industry_name
        """), {"td": trade_date})
        return [_value_from_row(r) for r in rows]

    def coverage_summary(self) -> list[tuple[str, int, "date", "date"]]:
        """Per-index (industry_name, num_value_rows, earliest_date,
        latest_date). Used by fundamentals-status to show coverage
        at a glance after the value computation runs."""
        rows = self._conn.execute(text("""
            SELECT
                industry_name,
                COUNT(*) AS rows,
                MIN(trade_date) AS earliest,
                MAX(trade_date) AS latest
            FROM industry_index_value
            GROUP BY industry_name
            ORDER BY industry_name
        """))
        return [
            (r[0], int(r[1]), r[2], r[3])
            for r in rows
        ]

    # ----- writes ----- #

    def upsert_value(
        self, industry_name: str, *,
        trade_date: "date",
        value: float,
        constituents_used: int,
        constituents_missing: int,
        renormalized: bool,
        effective_rebalance_date: "date",
    ) -> None:
        """Idempotent on (industry_name, trade_date). Re-running
        the compute for a date overwrites — useful when constituents
        change retroactively (e.g. operator backfills a missing
        mcap row and the compute needs to be re-run)."""
        if value <= 0:
            raise ValueError("value must be > 0")
        if constituents_used < 0 or constituents_missing < 0:
            raise ValueError("constituent counts must be >= 0")
        self._conn.execute(text("""
            INSERT INTO industry_index_value
                (industry_name, trade_date, value,
                 constituents_used, constituents_missing,
                 renormalized, effective_rebalance_date, computed_at)
            VALUES (:n, :td, :v, :cu, :cm, :rn, :erd, NOW())
            ON CONFLICT (industry_name, trade_date) DO UPDATE SET
                value = EXCLUDED.value,
                constituents_used = EXCLUDED.constituents_used,
                constituents_missing = EXCLUDED.constituents_missing,
                renormalized = EXCLUDED.renormalized,
                effective_rebalance_date = EXCLUDED.effective_rebalance_date,
                computed_at = NOW()
        """), {"n": industry_name, "td": trade_date, "v": value,
               "cu": constituents_used, "cm": constituents_missing,
               "rn": renormalized, "erd": effective_rebalance_date})

    def delete_for_index(self, industry_name: str) -> int:
        """Wipe all value rows for one index. Used by tests and
        by the admin "recompute from scratch" path."""
        result = self._conn.execute(text("""
            DELETE FROM industry_index_value
            WHERE industry_name = :n
        """), {"n": industry_name})
        return result.rowcount


def _value_from_row(row):
    from ..fundamentals import IndustryIndexValue
    return IndustryIndexValue(
        industry_name=row[0],
        trade_date=row[1],
        value=float(row[2]),
        constituents_used=int(row[3]),
        constituents_missing=int(row[4]),
        renormalized=row[5],
        effective_rebalance_date=row[6],
        computed_at=row[7],
    )


# ---------- IndicatorState (Phase 5-1) ---------- #


class IndicatorStateRepository:
    """Per-bar Triple-EMA indicator state.

    Polymorphic series identity: each row is either ticker-keyed
    (ticker_id + timeframe) or industry-keyed (industry_name).
    The repo's read methods take a `series_key` parameter
    encoded as either a (ticker_id, timeframe) tuple OR a string
    (treated as industry_name). The write methods take explicit
    keyword args because typos like get vs upsert are common.

    Two hot read paths:
      - get_latest_for_series / get_history_for_series — the
        Phase 6+ strategy engine reads "yesterday's state" per
        series.
      - list_for_date — cross-series snapshot, "all series with
        display_regime=+1 on date D".
    """

    def __init__(self, conn: Connection):
        self._conn = conn

    # ----- reads ----- #

    def get_at_for_ticker(
        self, ticker_id: int, timeframe: str, trade_date: "date",
    ):
        row = self._conn.execute(text("""
            SELECT * FROM indicator_state
            WHERE ticker_id = :tid
              AND timeframe = :tf
              AND trade_date = :td
        """), {"tid": ticker_id, "tf": timeframe, "td": trade_date}).fetchone()
        return _indicator_from_row(row) if row else None

    def get_at_for_industry(
        self, industry_name: str, trade_date: "date",
    ):
        row = self._conn.execute(text("""
            SELECT * FROM indicator_state
            WHERE industry_name = :n
              AND trade_date = :td
        """), {"n": industry_name, "td": trade_date}).fetchone()
        return _indicator_from_row(row) if row else None

    def get_latest_for_ticker(self, ticker_id: int, timeframe: str):
        row = self._conn.execute(text("""
            SELECT * FROM indicator_state
            WHERE ticker_id = :tid AND timeframe = :tf
            ORDER BY trade_date DESC
            LIMIT 1
        """), {"tid": ticker_id, "tf": timeframe}).fetchone()
        return _indicator_from_row(row) if row else None

    def get_latest_for_industry(self, industry_name: str):
        row = self._conn.execute(text("""
            SELECT * FROM indicator_state
            WHERE industry_name = :n
            ORDER BY trade_date DESC
            LIMIT 1
        """), {"n": industry_name}).fetchone()
        return _indicator_from_row(row) if row else None

    def get_history_for_ticker(
        self, ticker_id: int, timeframe: str, *,
        from_date: Optional["date"] = None,
        to_date: Optional["date"] = None,
        limit: Optional[int] = None,
    ) -> list:
        """Time series for one ticker, newest first."""
        conditions = ["ticker_id = :tid", "timeframe = :tf"]
        params: dict = {"tid": ticker_id, "tf": timeframe}
        if from_date is not None:
            conditions.append("trade_date >= :fd")
            params["fd"] = from_date
        if to_date is not None:
            conditions.append("trade_date <= :td")
            params["td"] = to_date
        sql = f"""
            SELECT * FROM indicator_state
            WHERE {' AND '.join(conditions)}
            ORDER BY trade_date DESC
        """
        if limit is not None:
            sql += " LIMIT :lim"
            params["lim"] = limit
        rows = self._conn.execute(text(sql), params)
        return [_indicator_from_row(r) for r in rows]

    def get_history_for_industry(
        self, industry_name: str, *,
        from_date: Optional["date"] = None,
        to_date: Optional["date"] = None,
        limit: Optional[int] = None,
    ) -> list:
        """Time series for one industry index, newest first."""
        conditions = ["industry_name = :n"]
        params: dict = {"n": industry_name}
        if from_date is not None:
            conditions.append("trade_date >= :fd")
            params["fd"] = from_date
        if to_date is not None:
            conditions.append("trade_date <= :td")
            params["td"] = to_date
        sql = f"""
            SELECT * FROM indicator_state
            WHERE {' AND '.join(conditions)}
            ORDER BY trade_date DESC
        """
        if limit is not None:
            sql += " LIMIT :lim"
            params["lim"] = limit
        rows = self._conn.execute(text(sql), params)
        return [_indicator_from_row(r) for r in rows]

    def list_for_date(
        self, trade_date: "date", *,
        series_kind: Optional[str] = None,  # 'TICKER' | 'INDUSTRY' | None
        display_regime: Optional[int] = None,
        core_regime: Optional[int] = None,
    ) -> list:
        """Cross-series snapshot on a single date, optionally filtered
        by series kind and/or regime. Used by Phase 6+ "what fired
        today" queries.

        series_kind:
          - 'TICKER' → only ticker rows (industry_name IS NULL)
          - 'INDUSTRY' → only industry rows (ticker_id IS NULL)
          - None → both

        Ordering: industries alphabetically, then tickers by id.
        Stable enough for snapshot reports."""
        conditions = ["trade_date = :td"]
        params: dict = {"td": trade_date}
        if series_kind == "TICKER":
            conditions.append("ticker_id IS NOT NULL")
        elif series_kind == "INDUSTRY":
            conditions.append("industry_name IS NOT NULL")
        elif series_kind is not None:
            raise ValueError(
                f"series_kind must be 'TICKER', 'INDUSTRY', or None; "
                f"got {series_kind!r}"
            )
        if display_regime is not None:
            conditions.append("display_regime = :dr")
            params["dr"] = display_regime
        if core_regime is not None:
            conditions.append("core_regime = :cr")
            params["cr"] = core_regime
        sql = f"""
            SELECT * FROM indicator_state
            WHERE {' AND '.join(conditions)}
            ORDER BY industry_name NULLS LAST, ticker_id NULLS LAST
        """
        rows = self._conn.execute(text(sql), params)
        return [_indicator_from_row(r) for r in rows]

    def list_recent_turns(
        self, *,
        kind: str = "buy",  # 'buy' or 'sell'
        ema: str = "med",   # 'med' / 'long' / 'longest'
        days: int = 7,
        limit: int = 100,
    ) -> list:
        """List bars where the specified EMA printed a turn in the
        last N days. Used by Phase 6+ scanner "show me recent
        medium-EMA buy signals across all tickers".

        Returns newest first."""
        if kind not in ("buy", "sell"):
            raise ValueError(f"kind must be 'buy' or 'sell'; got {kind!r}")
        if ema not in ("med", "long", "longest"):
            raise ValueError(
                f"ema must be 'med', 'long', or 'longest'; got {ema!r}"
            )
        column = f"{ema}_turn_{kind}"
        sql = f"""
            SELECT * FROM indicator_state
            WHERE {column} = TRUE
              AND trade_date >= CURRENT_DATE - INTERVAL '{int(days)} days'
            ORDER BY trade_date DESC, industry_name NULLS LAST, ticker_id NULLS LAST
            LIMIT :lim
        """
        rows = self._conn.execute(text(sql), {"lim": limit})
        return [_indicator_from_row(r) for r in rows]

    def coverage_summary_for_industries(
        self,
    ) -> list[tuple[str, int, "date", "date"]]:
        """Per-industry (name, row_count, earliest, latest). Used
        by fundamentals-status (extended in Phase 5-4)."""
        rows = self._conn.execute(text("""
            SELECT industry_name, COUNT(*),
                   MIN(trade_date), MAX(trade_date)
            FROM indicator_state
            WHERE industry_name IS NOT NULL
            GROUP BY industry_name
            ORDER BY industry_name
        """))
        return [
            (r[0], int(r[1]), r[2], r[3])
            for r in rows
        ]

    def coverage_summary_for_tickers(
        self, *, timeframe: Optional[str] = None,
    ) -> list[tuple[int, str, int, "date", "date"]]:
        """Per-ticker (ticker_id, timeframe, row_count, earliest,
        latest). Filterable by timeframe."""
        if timeframe is None:
            sql = """
                SELECT ticker_id, timeframe, COUNT(*),
                       MIN(trade_date), MAX(trade_date)
                FROM indicator_state
                WHERE ticker_id IS NOT NULL
                GROUP BY ticker_id, timeframe
                ORDER BY ticker_id, timeframe
            """
            params = {}
        else:
            sql = """
                SELECT ticker_id, timeframe, COUNT(*),
                       MIN(trade_date), MAX(trade_date)
                FROM indicator_state
                WHERE ticker_id IS NOT NULL AND timeframe = :tf
                GROUP BY ticker_id, timeframe
                ORDER BY ticker_id
            """
            params = {"tf": timeframe}
        rows = self._conn.execute(text(sql), params)
        return [
            (int(r[0]), r[1], int(r[2]), r[3], r[4])
            for r in rows
        ]

    # ----- writes ----- #

    def upsert_for_ticker(self, state) -> None:
        """Upsert one indicator state row keyed by ticker.

        Idempotent on (ticker_id, timeframe, trade_date). The
        callsite passes an IndicatorState dataclass whose ticker_id
        and timeframe are populated, industry_name=None.

        Validates:
          - regime values ∈ {-1, 0, +1}.
          - pattern labels ∈ PATTERN_LABELS.
          - ATR >= 0; EMA values > 0.
          - For ticker rows: industry_name must be None.
        """
        self._validate_state_for_write(state, expect_ticker=True)
        self._conn.execute(text("""
            INSERT INTO indicator_state (
                ticker_id, timeframe, industry_name, trade_date,
                med_ema, long_ema, longest_ema,
                atr, upper_inner_band, lower_inner_band,
                upper_mid_band, lower_mid_band,
                upper_outer_band, lower_outer_band,
                core_regime, display_regime,
                pierces_upper_mid, pierces_lower_mid,
                dist_to_med_in_hl_units, dist_to_long_in_hl_units,
                dist_to_longest_in_hl_units,
                mean_dist_to_med_in_hl_units, mean_dist_to_long_in_hl_units,
                mean_dist_to_longest_in_hl_units,
                bars_since_med_turn, bars_since_long_turn,
                bars_since_longest_turn,
                pct_change_since_med_turn, pct_change_since_long_turn,
                pct_change_since_longest_turn,
                med_pattern_label, long_pattern_label, longest_pattern_label,
                med_turn_buy, med_turn_sell,
                long_turn_buy, long_turn_sell,
                longest_turn_buy, longest_turn_sell,
                computed_at
            ) VALUES (
                :tid, :tf, NULL, :td,
                :med, :long, :longest,
                :atr, :uib, :lib, :umb, :lmb, :uob, :lob,
                :cr, :dr,
                :pum, :plm,
                :dmh, :dlh, :dxh, :mdmh, :mdlh, :mdxh,
                :bmt, :blt, :bxt,
                :pcmt, :pclt, :pcxt,
                :mpl, :lpl, :xpl,
                :mtb, :mts, :ltb, :lts, :xtb, :xts,
                NOW()
            )
            ON CONFLICT (ticker_id, timeframe, trade_date)
            WHERE ticker_id IS NOT NULL
            DO UPDATE SET
                med_ema = EXCLUDED.med_ema,
                long_ema = EXCLUDED.long_ema,
                longest_ema = EXCLUDED.longest_ema,
                atr = EXCLUDED.atr,
                upper_inner_band = EXCLUDED.upper_inner_band,
                lower_inner_band = EXCLUDED.lower_inner_band,
                upper_mid_band = EXCLUDED.upper_mid_band,
                lower_mid_band = EXCLUDED.lower_mid_band,
                upper_outer_band = EXCLUDED.upper_outer_band,
                lower_outer_band = EXCLUDED.lower_outer_band,
                core_regime = EXCLUDED.core_regime,
                display_regime = EXCLUDED.display_regime,
                pierces_upper_mid = EXCLUDED.pierces_upper_mid,
                pierces_lower_mid = EXCLUDED.pierces_lower_mid,
                dist_to_med_in_hl_units = EXCLUDED.dist_to_med_in_hl_units,
                dist_to_long_in_hl_units = EXCLUDED.dist_to_long_in_hl_units,
                dist_to_longest_in_hl_units = EXCLUDED.dist_to_longest_in_hl_units,
                mean_dist_to_med_in_hl_units = EXCLUDED.mean_dist_to_med_in_hl_units,
                mean_dist_to_long_in_hl_units = EXCLUDED.mean_dist_to_long_in_hl_units,
                mean_dist_to_longest_in_hl_units = EXCLUDED.mean_dist_to_longest_in_hl_units,
                bars_since_med_turn = EXCLUDED.bars_since_med_turn,
                bars_since_long_turn = EXCLUDED.bars_since_long_turn,
                bars_since_longest_turn = EXCLUDED.bars_since_longest_turn,
                pct_change_since_med_turn = EXCLUDED.pct_change_since_med_turn,
                pct_change_since_long_turn = EXCLUDED.pct_change_since_long_turn,
                pct_change_since_longest_turn = EXCLUDED.pct_change_since_longest_turn,
                med_pattern_label = EXCLUDED.med_pattern_label,
                long_pattern_label = EXCLUDED.long_pattern_label,
                longest_pattern_label = EXCLUDED.longest_pattern_label,
                med_turn_buy = EXCLUDED.med_turn_buy,
                med_turn_sell = EXCLUDED.med_turn_sell,
                long_turn_buy = EXCLUDED.long_turn_buy,
                long_turn_sell = EXCLUDED.long_turn_sell,
                longest_turn_buy = EXCLUDED.longest_turn_buy,
                longest_turn_sell = EXCLUDED.longest_turn_sell,
                computed_at = NOW()
        """), self._params_for_state(state))

    def upsert_for_industry(self, state) -> None:
        """Upsert one indicator state row keyed by industry."""
        self._validate_state_for_write(state, expect_ticker=False)
        self._conn.execute(text("""
            INSERT INTO indicator_state (
                ticker_id, timeframe, industry_name, trade_date,
                med_ema, long_ema, longest_ema,
                atr, upper_inner_band, lower_inner_band,
                upper_mid_band, lower_mid_band,
                upper_outer_band, lower_outer_band,
                core_regime, display_regime,
                pierces_upper_mid, pierces_lower_mid,
                dist_to_med_in_hl_units, dist_to_long_in_hl_units,
                dist_to_longest_in_hl_units,
                mean_dist_to_med_in_hl_units, mean_dist_to_long_in_hl_units,
                mean_dist_to_longest_in_hl_units,
                bars_since_med_turn, bars_since_long_turn,
                bars_since_longest_turn,
                pct_change_since_med_turn, pct_change_since_long_turn,
                pct_change_since_longest_turn,
                med_pattern_label, long_pattern_label, longest_pattern_label,
                med_turn_buy, med_turn_sell,
                long_turn_buy, long_turn_sell,
                longest_turn_buy, longest_turn_sell,
                computed_at
            ) VALUES (
                NULL, NULL, :ind, :td,
                :med, :long, :longest,
                :atr, :uib, :lib, :umb, :lmb, :uob, :lob,
                :cr, :dr,
                :pum, :plm,
                :dmh, :dlh, :dxh, :mdmh, :mdlh, :mdxh,
                :bmt, :blt, :bxt,
                :pcmt, :pclt, :pcxt,
                :mpl, :lpl, :xpl,
                :mtb, :mts, :ltb, :lts, :xtb, :xts,
                NOW()
            )
            ON CONFLICT (industry_name, trade_date)
            WHERE industry_name IS NOT NULL
            DO UPDATE SET
                med_ema = EXCLUDED.med_ema,
                long_ema = EXCLUDED.long_ema,
                longest_ema = EXCLUDED.longest_ema,
                atr = EXCLUDED.atr,
                upper_inner_band = EXCLUDED.upper_inner_band,
                lower_inner_band = EXCLUDED.lower_inner_band,
                upper_mid_band = EXCLUDED.upper_mid_band,
                lower_mid_band = EXCLUDED.lower_mid_band,
                upper_outer_band = EXCLUDED.upper_outer_band,
                lower_outer_band = EXCLUDED.lower_outer_band,
                core_regime = EXCLUDED.core_regime,
                display_regime = EXCLUDED.display_regime,
                pierces_upper_mid = EXCLUDED.pierces_upper_mid,
                pierces_lower_mid = EXCLUDED.pierces_lower_mid,
                dist_to_med_in_hl_units = EXCLUDED.dist_to_med_in_hl_units,
                dist_to_long_in_hl_units = EXCLUDED.dist_to_long_in_hl_units,
                dist_to_longest_in_hl_units = EXCLUDED.dist_to_longest_in_hl_units,
                mean_dist_to_med_in_hl_units = EXCLUDED.mean_dist_to_med_in_hl_units,
                mean_dist_to_long_in_hl_units = EXCLUDED.mean_dist_to_long_in_hl_units,
                mean_dist_to_longest_in_hl_units = EXCLUDED.mean_dist_to_longest_in_hl_units,
                bars_since_med_turn = EXCLUDED.bars_since_med_turn,
                bars_since_long_turn = EXCLUDED.bars_since_long_turn,
                bars_since_longest_turn = EXCLUDED.bars_since_longest_turn,
                pct_change_since_med_turn = EXCLUDED.pct_change_since_med_turn,
                pct_change_since_long_turn = EXCLUDED.pct_change_since_long_turn,
                pct_change_since_longest_turn = EXCLUDED.pct_change_since_longest_turn,
                med_pattern_label = EXCLUDED.med_pattern_label,
                long_pattern_label = EXCLUDED.long_pattern_label,
                longest_pattern_label = EXCLUDED.longest_pattern_label,
                med_turn_buy = EXCLUDED.med_turn_buy,
                med_turn_sell = EXCLUDED.med_turn_sell,
                long_turn_buy = EXCLUDED.long_turn_buy,
                long_turn_sell = EXCLUDED.long_turn_sell,
                longest_turn_buy = EXCLUDED.longest_turn_buy,
                longest_turn_sell = EXCLUDED.longest_turn_sell,
                computed_at = NOW()
        """), self._params_for_state(state))

    def delete_for_ticker(
        self, ticker_id: int, timeframe: str,
    ) -> int:
        """Wipe all indicator state for one (ticker, timeframe).
        Used by the runner's full-recompute admin path."""
        result = self._conn.execute(text("""
            DELETE FROM indicator_state
            WHERE ticker_id = :tid AND timeframe = :tf
        """), {"tid": ticker_id, "tf": timeframe})
        return result.rowcount

    def delete_for_industry(self, industry_name: str) -> int:
        """Wipe all indicator state for one industry."""
        result = self._conn.execute(text("""
            DELETE FROM indicator_state
            WHERE industry_name = :n
        """), {"n": industry_name})
        return result.rowcount

    # ----- validation + params helpers ----- #

    def _validate_state_for_write(self, state, *, expect_ticker: bool) -> None:
        from ..fundamentals import PATTERN_LABELS
        if expect_ticker:
            if state.ticker_id is None or state.timeframe is None:
                raise ValueError(
                    "upsert_for_ticker requires ticker_id and timeframe"
                )
            if state.industry_name is not None:
                raise ValueError(
                    "upsert_for_ticker requires industry_name=None"
                )
        else:
            if state.industry_name is None:
                raise ValueError(
                    "upsert_for_industry requires industry_name"
                )
            if state.ticker_id is not None or state.timeframe is not None:
                raise ValueError(
                    "upsert_for_industry requires ticker_id=None and "
                    "timeframe=None"
                )
        if state.core_regime not in (-1, 0, 1):
            raise ValueError(
                f"core_regime must be in {{-1, 0, 1}}; "
                f"got {state.core_regime!r}"
            )
        if state.display_regime not in (-1, 0, 1):
            raise ValueError(
                f"display_regime must be in {{-1, 0, 1}}; "
                f"got {state.display_regime!r}"
            )
        if state.atr < 0:
            raise ValueError(f"atr must be >= 0; got {state.atr!r}")
        if state.med_ema <= 0 or state.long_ema <= 0 or state.longest_ema <= 0:
            raise ValueError("EMA values must be > 0")
        for label_name, label_val in (
            ("med_pattern_label", state.med_pattern_label),
            ("long_pattern_label", state.long_pattern_label),
            ("longest_pattern_label", state.longest_pattern_label),
        ):
            if label_val not in PATTERN_LABELS:
                raise ValueError(
                    f"{label_name} must be in {PATTERN_LABELS}; "
                    f"got {label_val!r}"
                )

    def _params_for_state(self, state) -> dict:
        """Pack an IndicatorState into the dict of SQL bind params."""
        return {
            "tid": state.ticker_id,
            "tf": state.timeframe,
            "ind": state.industry_name,
            "td": state.trade_date,
            "med": state.med_ema,
            "long": state.long_ema,
            "longest": state.longest_ema,
            "atr": state.atr,
            "uib": state.upper_inner_band,
            "lib": state.lower_inner_band,
            "umb": state.upper_mid_band,
            "lmb": state.lower_mid_band,
            "uob": state.upper_outer_band,
            "lob": state.lower_outer_band,
            "cr": state.core_regime,
            "dr": state.display_regime,
            "pum": state.pierces_upper_mid,
            "plm": state.pierces_lower_mid,
            "dmh": state.dist_to_med_in_hl_units,
            "dlh": state.dist_to_long_in_hl_units,
            "dxh": state.dist_to_longest_in_hl_units,
            "mdmh": state.mean_dist_to_med_in_hl_units,
            "mdlh": state.mean_dist_to_long_in_hl_units,
            "mdxh": state.mean_dist_to_longest_in_hl_units,
            "bmt": state.bars_since_med_turn,
            "blt": state.bars_since_long_turn,
            "bxt": state.bars_since_longest_turn,
            "pcmt": state.pct_change_since_med_turn,
            "pclt": state.pct_change_since_long_turn,
            "pcxt": state.pct_change_since_longest_turn,
            "mpl": state.med_pattern_label,
            "lpl": state.long_pattern_label,
            "xpl": state.longest_pattern_label,
            "mtb": state.med_turn_buy,
            "mts": state.med_turn_sell,
            "ltb": state.long_turn_buy,
            "lts": state.long_turn_sell,
            "xtb": state.longest_turn_buy,
            "xts": state.longest_turn_sell,
        }


def _indicator_from_row(row):
    """Unpack a row of indicator_state SELECT * into IndicatorState.

    Column order matters and must match the CREATE TABLE exactly.
    If a future migration adds a column, the column count here
    must be updated.
    """
    from ..fundamentals import IndicatorState
    return IndicatorState(
        ticker_id=int(row[0]) if row[0] is not None else None,
        timeframe=row[1],
        industry_name=row[2],
        trade_date=row[3],
        med_ema=float(row[4]),
        long_ema=float(row[5]),
        longest_ema=float(row[6]),
        atr=float(row[7]),
        upper_inner_band=float(row[8]),
        lower_inner_band=float(row[9]),
        upper_mid_band=float(row[10]),
        lower_mid_band=float(row[11]),
        upper_outer_band=float(row[12]),
        lower_outer_band=float(row[13]),
        core_regime=int(row[14]),
        display_regime=int(row[15]),
        pierces_upper_mid=bool(row[16]),
        pierces_lower_mid=bool(row[17]),
        dist_to_med_in_hl_units=(float(row[18]) if row[18] is not None else None),
        dist_to_long_in_hl_units=(float(row[19]) if row[19] is not None else None),
        dist_to_longest_in_hl_units=(float(row[20]) if row[20] is not None else None),
        mean_dist_to_med_in_hl_units=(float(row[21]) if row[21] is not None else None),
        mean_dist_to_long_in_hl_units=(float(row[22]) if row[22] is not None else None),
        mean_dist_to_longest_in_hl_units=(float(row[23]) if row[23] is not None else None),
        bars_since_med_turn=(int(row[24]) if row[24] is not None else None),
        bars_since_long_turn=(int(row[25]) if row[25] is not None else None),
        bars_since_longest_turn=(int(row[26]) if row[26] is not None else None),
        pct_change_since_med_turn=(float(row[27]) if row[27] is not None else None),
        pct_change_since_long_turn=(float(row[28]) if row[28] is not None else None),
        pct_change_since_longest_turn=(float(row[29]) if row[29] is not None else None),
        med_pattern_label=row[30],
        long_pattern_label=row[31],
        longest_pattern_label=row[32],
        med_turn_buy=bool(row[33]),
        med_turn_sell=bool(row[34]),
        long_turn_buy=bool(row[35]),
        long_turn_sell=bool(row[36]),
        longest_turn_buy=bool(row[37]),
        longest_turn_sell=bool(row[38]),
        computed_at=row[39],
    )


# ---------- SignalEventRepository (Phase 6-2: unified table) ---------- #

# Migration 0016 unified the previously separate signal_events (live
# per-ticker) and signal_event (Phase 6-1 per-industry / backfill) into
# one table: signal_events. The dataclass interface (SignalEvent in
# fundamentals/__init__.py) is unchanged — fields are still trade_date,
# event_kind, close_at_event — but the underlying columns are
# bar_timestamp (TIMESTAMPTZ), signal_type (VARCHAR), price_at_signal
# (DOUBLE). All SQL below maps between the two by aliasing on SELECT
# and explicit naming on INSERT/WHERE.

_SIGNAL_EVENT_SELECT = (
    "ticker_id, timeframe, industry_name, "
    "bar_timestamp::date AS trade_date, "
    "signal_type AS event_kind, "
    "regime_before, regime_after, "
    "price_at_signal AS close_at_event, "
    "computed_at"
)


class SignalEventRepository:
    """Sparse signal events derived from indicator_state regime
    transitions, persisted to the unified signal_events table.

    Reads cover the common Phase 6+ access patterns:
      - get_for_industry(name): full history for one industry.
      - get_for_ticker(ticker_id, timeframe): full history for one
        ticker series.
      - list_for_date_range(from, to, kind): all events of a kind
        across all series in a window.
      - list_recent(kind, days, limit): scanner-style "what fired
        recently".
      - count_by_kind_for_industry(): summary for status displays.

    Writes:
      - upsert (one-row): idempotent on series-identity uniqueness
        constraints added in migration 0016.
      - batch_upsert: many events in one transaction, used by the
        runner.
      - delete_for_industry / delete_for_ticker: full wipe for
        re-compute.
    """

    def __init__(self, conn: Connection):
        self._conn = conn

    # ----- reads ----- #

    def get_for_industry(
        self, industry_name: str, *,
        kinds: Optional[list[str]] = None,
        from_date: Optional["date"] = None,
        to_date: Optional["date"] = None,
        limit: Optional[int] = None,
    ) -> list:
        """All events for one industry, newest-first. Optional
        filters on event kinds and date range."""
        conditions = ["industry_name = :n"]
        params: dict = {"n": industry_name}
        if kinds:
            from ..fundamentals import SIGNAL_EVENT_KINDS
            for k in kinds:
                if k not in SIGNAL_EVENT_KINDS:
                    raise ValueError(
                        f"unknown event_kind {k!r}; "
                        f"must be in {SIGNAL_EVENT_KINDS}"
                    )
            placeholders = ", ".join(f":k{i}" for i in range(len(kinds)))
            conditions.append(f"signal_type IN ({placeholders})")
            for i, k in enumerate(kinds):
                params[f"k{i}"] = k
        if from_date is not None:
            conditions.append("bar_timestamp >= :fd")
            params["fd"] = from_date
        if to_date is not None:
            conditions.append("bar_timestamp::date <= :td")
            params["td"] = to_date
        sql = f"""
            SELECT {_SIGNAL_EVENT_SELECT}
            FROM signal_events
            WHERE {' AND '.join(conditions)}
            ORDER BY bar_timestamp DESC, signal_type
        """
        if limit is not None:
            sql += " LIMIT :lim"
            params["lim"] = limit
        rows = self._conn.execute(text(sql), params)
        return [_signal_event_from_row(r) for r in rows]

    def get_for_ticker(
        self, ticker_id: int, timeframe: str, *,
        kinds: Optional[list[str]] = None,
        from_date: Optional["date"] = None,
        to_date: Optional["date"] = None,
        limit: Optional[int] = None,
    ) -> list:
        """All events for one ticker series. Same shape as
        get_for_industry."""
        conditions = ["ticker_id = :tid", "timeframe = :tf"]
        params: dict = {"tid": ticker_id, "tf": timeframe}
        if kinds:
            from ..fundamentals import SIGNAL_EVENT_KINDS
            for k in kinds:
                if k not in SIGNAL_EVENT_KINDS:
                    raise ValueError(
                        f"unknown event_kind {k!r}; "
                        f"must be in {SIGNAL_EVENT_KINDS}"
                    )
            placeholders = ", ".join(f":k{i}" for i in range(len(kinds)))
            conditions.append(f"signal_type IN ({placeholders})")
            for i, k in enumerate(kinds):
                params[f"k{i}"] = k
        if from_date is not None:
            conditions.append("bar_timestamp >= :fd")
            params["fd"] = from_date
        if to_date is not None:
            conditions.append("bar_timestamp::date <= :td")
            params["td"] = to_date
        sql = f"""
            SELECT {_SIGNAL_EVENT_SELECT}
            FROM signal_events
            WHERE {' AND '.join(conditions)}
            ORDER BY bar_timestamp DESC, signal_type
        """
        if limit is not None:
            sql += " LIMIT :lim"
            params["lim"] = limit
        rows = self._conn.execute(text(sql), params)
        return [_signal_event_from_row(r) for r in rows]

    def list_for_date_range(
        self, from_date: "date", to_date: "date", *,
        kind: Optional[str] = None,
        series_kind: Optional[str] = None,  # 'TICKER' | 'INDUSTRY' | None
        limit: Optional[int] = None,
    ) -> list:
        """Cross-series scanner. 'show me all BUY events between
        date X and Y' across all industries (or tickers, or both).
        """
        from ..fundamentals import SIGNAL_EVENT_KINDS
        conditions = ["bar_timestamp::date BETWEEN :fd AND :td"]
        params: dict = {"fd": from_date, "td": to_date}
        if kind is not None:
            if kind not in SIGNAL_EVENT_KINDS:
                raise ValueError(
                    f"unknown event_kind {kind!r}; "
                    f"must be in {SIGNAL_EVENT_KINDS}"
                )
            conditions.append("signal_type = :k")
            params["k"] = kind
        if series_kind == "TICKER":
            conditions.append("ticker_id IS NOT NULL")
        elif series_kind == "INDUSTRY":
            conditions.append("industry_name IS NOT NULL")
        elif series_kind is not None:
            raise ValueError(
                f"series_kind must be 'TICKER', 'INDUSTRY', or None; "
                f"got {series_kind!r}"
            )
        sql = f"""
            SELECT {_SIGNAL_EVENT_SELECT}
            FROM signal_events
            WHERE {' AND '.join(conditions)}
            ORDER BY bar_timestamp DESC, industry_name NULLS LAST,
                     ticker_id NULLS LAST, signal_type
        """
        if limit is not None:
            sql += " LIMIT :lim"
            params["lim"] = limit
        rows = self._conn.execute(text(sql), params)
        return [_signal_event_from_row(r) for r in rows]

    def list_recent(
        self, *,
        kind: Optional[str] = None,
        days: int = 7,
        limit: int = 100,
    ) -> list:
        """Scanner-style: events of a kind in the last N days,
        newest-first. None for kind means all kinds."""
        from ..fundamentals import SIGNAL_EVENT_KINDS
        conditions = [
            f"bar_timestamp >= (CURRENT_DATE - INTERVAL '{int(days)} days')",
        ]
        params: dict = {"lim": limit}
        if kind is not None:
            if kind not in SIGNAL_EVENT_KINDS:
                raise ValueError(
                    f"unknown event_kind {kind!r}; "
                    f"must be in {SIGNAL_EVENT_KINDS}"
                )
            conditions.append("signal_type = :k")
            params["k"] = kind
        sql = f"""
            SELECT {_SIGNAL_EVENT_SELECT}
            FROM signal_events
            WHERE {' AND '.join(conditions)}
            ORDER BY bar_timestamp DESC, industry_name NULLS LAST,
                     ticker_id NULLS LAST, signal_type
            LIMIT :lim
        """
        rows = self._conn.execute(text(sql), params)
        return [_signal_event_from_row(r) for r in rows]

    def count_by_kind_for_industry(
        self, industry_name: str,
    ) -> dict:
        """Per-kind event count for one industry. Used by status
        displays."""
        rows = self._conn.execute(text("""
            SELECT signal_type, COUNT(*)
            FROM signal_events
            WHERE industry_name = :n
            GROUP BY signal_type
        """), {"n": industry_name})
        counts = {k: 0 for k in (
            "BUY", "SELL", "EXIT_FROM_BUY", "EXIT_FROM_SELL",
        )}
        for kind, count in rows:
            counts[kind] = int(count)
        return counts

    def coverage_summary_for_industries(
        self,
    ) -> list[tuple[str, int, "date", "date"]]:
        """Per-industry (name, total_event_count, earliest, latest).
        Used by fundamentals-status."""
        rows = self._conn.execute(text("""
            SELECT industry_name, COUNT(*),
                   MIN(bar_timestamp::date), MAX(bar_timestamp::date)
            FROM signal_events
            WHERE industry_name IS NOT NULL
            GROUP BY industry_name
            ORDER BY industry_name
        """))
        return [(r[0], int(r[1]), r[2], r[3]) for r in rows]

    # ----- writes ----- #

    def upsert(self, event) -> None:
        """Upsert one signal event. Validates the event_kind and
        regime values before hitting the DB (in addition to the DB
        CHECK constraints).

        Idempotent on the partial unique indexes added by migration
        0016: (ticker_id, timeframe, bar_timestamp, signal_type)
        for ticker rows; (industry_name, bar_timestamp, signal_type)
        for industry rows.

        The dataclass uses trade_date (date); we cast it to
        bar_timestamp (TIMESTAMPTZ at 00:00 UTC) for storage.
        """
        from ..fundamentals import SIGNAL_EVENT_KINDS
        # Application-level validation.
        if event.event_kind not in SIGNAL_EVENT_KINDS:
            raise ValueError(
                f"event_kind must be in {SIGNAL_EVENT_KINDS}; "
                f"got {event.event_kind!r}"
            )
        if event.regime_before not in (-1, 0, 1):
            raise ValueError(
                f"regime_before must be in {{-1, 0, 1}}; "
                f"got {event.regime_before!r}"
            )
        if event.regime_after not in (-1, 0, 1):
            raise ValueError(
                f"regime_after must be in {{-1, 0, 1}}; "
                f"got {event.regime_after!r}"
            )
        # Series-shape validation. Either ticker_id+timeframe or
        # industry_name; not both, not neither.
        ticker_set = event.ticker_id is not None and event.timeframe is not None
        industry_set = event.industry_name is not None
        if ticker_set and industry_set:
            raise ValueError(
                "exactly one of (ticker_id, timeframe) or "
                "industry_name must be set; got both"
            )
        if not ticker_set and not industry_set:
            raise ValueError(
                "exactly one of (ticker_id, timeframe) or "
                "industry_name must be set; got neither"
            )

        # trade_date (date) → TIMESTAMPTZ at 00:00 UTC. Use Postgres
        # cast so the conversion happens server-side and is consistent
        # with the migration's data-translation cast.
        bar_ts_sql = "((:td)::timestamp AT TIME ZONE 'UTC')"

        if industry_set:
            self._conn.execute(text(f"""
                INSERT INTO signal_events (
                    ticker_id, timeframe, industry_name,
                    bar_timestamp, signal_type,
                    regime_before, regime_after,
                    price_at_signal, computed_at
                ) VALUES (
                    NULL, NULL, :ind,
                    {bar_ts_sql}, :kind,
                    :rb, :ra, :close, NOW()
                )
                ON CONFLICT (industry_name, bar_timestamp, signal_type)
                WHERE industry_name IS NOT NULL
                DO UPDATE SET
                    regime_before = EXCLUDED.regime_before,
                    regime_after = EXCLUDED.regime_after,
                    price_at_signal = EXCLUDED.price_at_signal,
                    computed_at = NOW()
            """), {
                "ind": event.industry_name, "td": event.trade_date,
                "kind": event.event_kind,
                "rb": event.regime_before, "ra": event.regime_after,
                "close": float(event.close_at_event),
            })
        else:
            self._conn.execute(text(f"""
                INSERT INTO signal_events (
                    ticker_id, timeframe, industry_name,
                    bar_timestamp, signal_type,
                    regime_before, regime_after,
                    price_at_signal, computed_at
                ) VALUES (
                    :tid, :tf, NULL,
                    {bar_ts_sql}, :kind,
                    :rb, :ra, :close, NOW()
                )
                ON CONFLICT (ticker_id, timeframe, bar_timestamp, signal_type)
                WHERE ticker_id IS NOT NULL
                DO UPDATE SET
                    regime_before = EXCLUDED.regime_before,
                    regime_after = EXCLUDED.regime_after,
                    price_at_signal = EXCLUDED.price_at_signal,
                    computed_at = NOW()
            """), {
                "tid": event.ticker_id, "tf": event.timeframe,
                "td": event.trade_date,
                "kind": event.event_kind,
                "rb": event.regime_before, "ra": event.regime_after,
                "close": float(event.close_at_event),
            })

    def batch_upsert(self, events: list) -> int:
        """Upsert a list of events in the current transaction.
        Returns the number processed."""
        for e in events:
            self.upsert(e)
        return len(events)

    # ----- deletes ----- #

    def delete_for_industry(self, industry_name: str) -> int:
        """Wipe all signal events for one industry. Used by the
        runner's full-recompute path."""
        result = self._conn.execute(text("""
            DELETE FROM signal_events
            WHERE industry_name = :n
        """), {"n": industry_name})
        return result.rowcount

    def delete_for_ticker(self, ticker_id: int, timeframe: str) -> int:
        result = self._conn.execute(text("""
            DELETE FROM signal_events
            WHERE ticker_id = :tid AND timeframe = :tf
        """), {"tid": ticker_id, "tf": timeframe})
        return result.rowcount


def _signal_event_from_row(row):
    """Unpack a row from `SELECT _SIGNAL_EVENT_SELECT FROM
    signal_events` into a SignalEvent dataclass.

    Column order matches _SIGNAL_EVENT_SELECT — if that constant
    changes, this must change with it.
    """
    from ..fundamentals import SignalEvent
    return SignalEvent(
        ticker_id=int(row[0]) if row[0] is not None else None,
        timeframe=row[1],
        industry_name=row[2],
        trade_date=row[3],
        event_kind=row[4],
        regime_before=int(row[5]) if row[5] is not None else 0,
        regime_after=int(row[6]) if row[6] is not None else 0,
        close_at_event=float(row[7]),
        computed_at=row[8],
    )

# ---------- Phase 6-3: benchmark stats repositories ---------- #


class StockBenchmarkStatsRepository:
    """Per-(stock, benchmark, window) regression stats. PK
    (ticker_id, benchmark_kind, window_label, as_of_date); weekly
    recompute appends a new row per series."""

    def __init__(self, conn: Connection):
        self._conn = conn

    def upsert_batch(self, rows: list) -> int:
        """Upsert a list of StockBenchmarkStats. Idempotent on PK."""
        for r in rows:
            self._conn.execute(text("""
                INSERT INTO stock_benchmark_stats (
                    ticker_id, benchmark_kind, benchmark_id, window_label,
                    as_of_date, n_weeks,
                    beta, alpha_annual, r_squared, sigma_annual,
                    rs_pct, info_ratio, computed_at
                ) VALUES (
                    :tid, :bk, :bid, :wl,
                    :asof, :nw,
                    :beta, :alpha, :r2, :sigma,
                    :rs, :ir, NOW()
                )
                ON CONFLICT (ticker_id, benchmark_kind, window_label, as_of_date)
                DO UPDATE SET
                    benchmark_id = EXCLUDED.benchmark_id,
                    n_weeks = EXCLUDED.n_weeks,
                    beta = EXCLUDED.beta,
                    alpha_annual = EXCLUDED.alpha_annual,
                    r_squared = EXCLUDED.r_squared,
                    sigma_annual = EXCLUDED.sigma_annual,
                    rs_pct = EXCLUDED.rs_pct,
                    info_ratio = EXCLUDED.info_ratio,
                    computed_at = NOW()
            """), {
                "tid": r.ticker_id, "bk": r.benchmark_kind,
                "bid": r.benchmark_id, "wl": r.window_label,
                "asof": r.as_of_date, "nw": r.n_weeks,
                "beta": r.beta, "alpha": r.alpha_annual,
                "r2": r.r_squared, "sigma": r.sigma_annual,
                "rs": r.rs_pct, "ir": r.info_ratio,
            })
        return len(rows)

    def get_latest_for_ticker(self, ticker_id: int) -> list:
        """Latest as_of_date row per (benchmark_kind, window_label)
        for one ticker. Returns up to 6 rows."""
        rows = self._conn.execute(text("""
            SELECT DISTINCT ON (benchmark_kind, window_label)
                ticker_id, benchmark_kind, benchmark_id, window_label,
                as_of_date, n_weeks, beta, alpha_annual, r_squared,
                sigma_annual, rs_pct, info_ratio, computed_at
            FROM stock_benchmark_stats
            WHERE ticker_id = :tid
            ORDER BY benchmark_kind, window_label, as_of_date DESC
        """), {"tid": ticker_id}).all()
        return [_stock_bench_from_row(r) for r in rows]


class IndustryBenchmarkStatsRepository:
    """Per-(industry, NIFTY, window) regression stats."""

    def __init__(self, conn: Connection):
        self._conn = conn

    def upsert_batch(self, rows: list) -> int:
        for r in rows:
            self._conn.execute(text("""
                INSERT INTO industry_benchmark_stats (
                    industry_name, benchmark_id, window_label,
                    as_of_date, n_weeks,
                    beta, alpha_annual, r_squared, sigma_annual,
                    rs_pct, info_ratio, computed_at
                ) VALUES (
                    :ind, :bid, :wl,
                    :asof, :nw,
                    :beta, :alpha, :r2, :sigma,
                    :rs, :ir, NOW()
                )
                ON CONFLICT (industry_name, benchmark_id, window_label, as_of_date)
                DO UPDATE SET
                    n_weeks = EXCLUDED.n_weeks,
                    beta = EXCLUDED.beta,
                    alpha_annual = EXCLUDED.alpha_annual,
                    r_squared = EXCLUDED.r_squared,
                    sigma_annual = EXCLUDED.sigma_annual,
                    rs_pct = EXCLUDED.rs_pct,
                    info_ratio = EXCLUDED.info_ratio,
                    computed_at = NOW()
            """), {
                "ind": r.industry_name, "bid": r.benchmark_id,
                "wl": r.window_label, "asof": r.as_of_date,
                "nw": r.n_weeks, "beta": r.beta,
                "alpha": r.alpha_annual, "r2": r.r_squared,
                "sigma": r.sigma_annual, "rs": r.rs_pct,
                "ir": r.info_ratio,
            })
        return len(rows)

    def get_latest_for_industry(self, industry_name: str) -> list:
        rows = self._conn.execute(text("""
            SELECT DISTINCT ON (window_label)
                industry_name, benchmark_id, window_label,
                as_of_date, n_weeks, beta, alpha_annual, r_squared,
                sigma_annual, rs_pct, info_ratio, computed_at
            FROM industry_benchmark_stats
            WHERE industry_name = :n
            ORDER BY window_label, as_of_date DESC
        """), {"n": industry_name}).all()
        return [_industry_bench_from_row(r) for r in rows]


def _stock_bench_from_row(row):
    from ..fundamentals import StockBenchmarkStats
    return StockBenchmarkStats(
        ticker_id=int(row[0]), benchmark_kind=row[1],
        benchmark_id=row[2], window_label=row[3],
        as_of_date=row[4], n_weeks=int(row[5]),
        beta=float(row[6]) if row[6] is not None else None,
        alpha_annual=float(row[7]) if row[7] is not None else None,
        r_squared=float(row[8]) if row[8] is not None else None,
        sigma_annual=float(row[9]) if row[9] is not None else None,
        rs_pct=float(row[10]) if row[10] is not None else None,
        info_ratio=float(row[11]) if row[11] is not None else None,
        computed_at=row[12],
    )


def _industry_bench_from_row(row):
    from ..fundamentals import IndustryBenchmarkStats
    return IndustryBenchmarkStats(
        industry_name=row[0], benchmark_id=row[1],
        window_label=row[2], as_of_date=row[3],
        n_weeks=int(row[4]),
        beta=float(row[5]) if row[5] is not None else None,
        alpha_annual=float(row[6]) if row[6] is not None else None,
        r_squared=float(row[7]) if row[7] is not None else None,
        sigma_annual=float(row[8]) if row[8] is not None else None,
        rs_pct=float(row[9]) if row[9] is not None else None,
        info_ratio=float(row[10]) if row[10] is not None else None,
        computed_at=row[11],
    )

