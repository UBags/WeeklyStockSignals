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
