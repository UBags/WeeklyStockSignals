"""Loader: nselib feed -> parsed atomic actions -> nse_corporate_actions.

`build_records` is pure (feed rows + isin map -> records) and carries the
whole transform: date parse, subject parse (one row -> N atomic actions),
isin -> ticker_id resolution. `refresh_with_repo` orchestrates over an injected
repo + downloader (testable without a DB or nselib); `run_nse_actions_refresh`
is the production wrapper that opens the transaction.

Stores ALL action types (a complete audit); the corrector filters to
factor/censor rows by type at read time.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy.engine import Engine

from ..persistence import transaction
from .nse_actions_parser import parse_subject
from .nse_actions_source import NseActionsDownloader, nselib_download
from .nse_corporate_actions_repo import (
    NseActionRecord,
    NseCorporateActionsRepository,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RefreshSummary:
    rows_fetched: int
    rows_skipped_bad_date: int
    actions_built: int
    actions_inserted: int
    actions_resolved: int      # actions whose isin mapped to a ticker_id
    actions_unresolved: int


def _parse_ex_date(value: Any) -> date | None:
    """nselib ex-dates are 'DD-Mon-YYYY' (e.g. '06-Jan-2006'). Defensive:
    return None on anything else so the loader can skip and count it."""
    if isinstance(value, date):
        return value
    s = str(value or "").strip()
    if not s:
        return None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _clean_isin(value: Any) -> str:
    s = str(value or "").strip()
    return s


def build_records(
    rows: list[dict[str, Any]],
    isin_map: dict[str, int],
    *,
    parse: Callable[[str | None], list] = parse_subject,
) -> tuple[list[NseActionRecord], dict[str, int]]:
    """Feed rows -> atomic NseActionRecords + a stats dict.

    One feed row's `subject` may yield several atomic actions (e.g. a compound
    split+bonus); each becomes its own record. Rows whose ex-date can't be
    parsed are skipped (counted) — ex_date is the correction anchor and is
    NOT NULL in the table.
    """
    records: list[NseActionRecord] = []
    stats = {
        "rows_fetched": len(rows),
        "rows_skipped_bad_date": 0,
        "actions_built": 0,
        "actions_resolved": 0,
        "actions_unresolved": 0,
    }

    for row in rows:
        ex_date = _parse_ex_date(row.get("exDate"))
        if ex_date is None:
            stats["rows_skipped_bad_date"] += 1
            continue

        isin = _clean_isin(row.get("isin"))
        symbol = str(row.get("symbol") or "").strip()
        subject = str(row.get("subject") or "")
        ticker_id = isin_map.get(isin)

        for act in parse(subject):
            records.append(
                NseActionRecord(
                    isin=isin,
                    symbol=symbol,
                    ex_date=ex_date,
                    action_type=act.action_type,
                    subject=act.raw_subject or subject,
                    ticker_id=ticker_id,
                    detail=act.detail or None,
                    ratio_numerator=act.ratio_numerator,
                    ratio_denominator=act.ratio_denominator,
                    price_factor=act.price_factor,
                    censor=act.censor,
                    raw_payload=dict(row),
                )
            )
            stats["actions_built"] += 1
            if ticker_id is not None:
                stats["actions_resolved"] += 1
            else:
                stats["actions_unresolved"] += 1

    return records, stats


def refresh_with_repo(
    repo: NseCorporateActionsRepository,
    download: NseActionsDownloader,
    from_date: str,
    to_date: str,
) -> RefreshSummary:
    """Orchestration over an injected repo + downloader — the unit-testable
    core (no engine, no nselib)."""
    isin_map = repo.isin_to_ticker_id()
    rows = download(from_date, to_date)
    records, stats = build_records(rows, isin_map)
    inserted = repo.upsert_many(records)
    summary = RefreshSummary(
        rows_fetched=stats["rows_fetched"],
        rows_skipped_bad_date=stats["rows_skipped_bad_date"],
        actions_built=stats["actions_built"],
        actions_inserted=inserted,
        actions_resolved=stats["actions_resolved"],
        actions_unresolved=stats["actions_unresolved"],
    )
    logger.info(
        "nse corporate-actions refresh: fetched=%d skipped_bad_date=%d "
        "built=%d inserted=%d resolved=%d unresolved=%d",
        summary.rows_fetched, summary.rows_skipped_bad_date,
        summary.actions_built, summary.actions_inserted,
        summary.actions_resolved, summary.actions_unresolved,
    )
    return summary


def run_nse_actions_refresh(
    engine: Engine,
    *,
    from_date: str,
    to_date: str,
    download: NseActionsDownloader = nselib_download,
) -> RefreshSummary:
    """Production entry point: fetch the nselib feed for the window and upsert
    the parsed atomic actions. Idempotent — safe to re-run (monthly overlap)."""
    with transaction(engine) as conn:
        repo = NseCorporateActionsRepository(conn)
        return refresh_with_repo(repo, download, from_date, to_date)
