"""Archived: the backfill-era per-industry / all-industries iiv runners.

run_index_value_backfill and run_index_value_backfill_all were the
index-value backfill fork. Backfill (commit 2), the compute-index-values
CLI command (3a), and the compute-index-values HTTP route (3b) were all
migrated onto the shared populate_industry_index_value_* populators,
leaving these with no live callers. Kept here (not deleted) so their
behaviour and direct tests stay on record.

run_index_value_backfill_all calls the sibling run_index_value_backfill
defined above it. Shared private helpers (CHECKPOINT_EVERY, the ISO-date
and terminal-status helpers, the trading-day lister) are imported from
their canonical home in fundamentals.runners.
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import Engine

from ..fundamentals.runners import (
    CHECKPOINT_EVERY,
    _list_trading_days_in_range,
    _parse_iso_date,
    _terminal_status,
)
from ..persistence import transaction
from ..persistence.fundamentals_repo import FundamentalsRunRepository

logger = logging.getLogger(__name__)


def run_index_value_backfill(
    db_engine: Engine,
    industry_name: str,
    *,
    from_date: date,
    to_date: date,
    resume_run_id: int | None = None,
    force: bool = False,
) -> int:
    """Walk trading days in [from_date, to_date], computing and
    writing daily index values for `industry_name`.

    Trading days come from daily_market_cap.trade_date — any day
    we have mcap data is a candidate. Pre-base_date days return
    None from compute_index_value_for_date and are skipped without
    error.

    Same fundamentals_run lifecycle as the Phase 1-3 runners.
    The progress_json carries the date list (not ticker list) for
    resume.

    Returns run_id.
    """
    from ..fundamentals.index_compute import compute_index_value_for_date
    from ..persistence.fundamentals_repo import (
        IndustryIndexRepository,
        IndustryIndexValueRepository,
    )

    if from_date > to_date:
        raise ValueError(
            f"from_date {from_date} must be <= to_date {to_date}"
        )

    # Sanity check: index must exist.
    with db_engine.connect() as conn:
        idx = IndustryIndexRepository(conn).get(industry_name)
    if idx is None:
        raise ValueError(
            f"run_index_value_backfill: index {industry_name!r} "
            f"does not exist"
        )

    # Skip-if-recent guard: use the value table's latest entry,
    # not the run-record (which uses a fixed-width exchange column
    # that doesn't accommodate per-industry granularity). If the
    # latest value row is within 80 days of today, skip unless
    # forced.
    phase = "index_value"
    if not force and resume_run_id is None:
        with db_engine.connect() as conn:
            latest = IndustryIndexValueRepository(conn).get_latest(
                industry_name,
            )
        if latest is not None:
            age = (date.today() - latest.trade_date).days
            if age < 80:
                logger.info(
                    "Skipping index value backfill for %s: latest "
                    "value is from %s (%d days ago)",
                    industry_name, latest.trade_date, age,
                )
                # Return a synthetic "no-op success" run id by
                # opening and closing one. Different from the
                # mcap pattern but the skip-if-recent contract
                # requires we return an int.
                with transaction(db_engine) as conn:
                    run_id = FundamentalsRunRepository(conn).open(
                        phase=phase, exchange="ALL",
                        notes=f"Skipped (recent values exist): {industry_name}",
                    )
                    FundamentalsRunRepository(conn).close(
                        run_id, status="SUCCESS",
                        succeeded=0, failed=0, skipped=0,
                    )
                return run_id

    # ----- resume path ----- #
    if resume_run_id is not None:
        with db_engine.connect() as conn:
            run = FundamentalsRunRepository(conn).get(resume_run_id)
        if run is None:
            raise ValueError(f"No run with id {resume_run_id}")
        if run.phase != phase:
            raise ValueError(
                f"Cannot resume: run {resume_run_id} has phase "
                f"{run.phase!r}, expected {phase!r}"
            )
        progress = run.progress_json or {}
        date_list_iso = progress.get("date_list", [])
        cursor_index = progress.get("cursor_index", 0)
        date_list = [_parse_iso_date(s) for s in date_list_iso]
        run_id = resume_run_id
        logger.info(
            "Resuming index value backfill run %d at date index %d/%d",
            run_id, cursor_index, len(date_list),
        )
        # Also restore the range — even if the caller passed a
        # different one, the resumed run uses the original.
        if "from_date" in progress:
            from_date = _parse_iso_date(progress["from_date"])
            to_date = _parse_iso_date(progress["to_date"])
    else:
        # Fresh run.
        date_list = _list_trading_days_in_range(
            db_engine, from_date, to_date,
        )
        if not date_list:
            logger.warning(
                "No trading days in [%s, %s] for index %s — nothing to do",
                from_date, to_date, industry_name,
            )
            with transaction(db_engine) as conn:
                run_id = FundamentalsRunRepository(conn).open(
                    phase=phase, exchange="ALL",
                    notes=f"No trading days in range for {industry_name}",
                )
                FundamentalsRunRepository(conn).close(
                    run_id, status="SUCCESS",
                    succeeded=0, failed=0, skipped=0,
                )
            return run_id
        with transaction(db_engine) as conn:
            run_id = FundamentalsRunRepository(conn).open(
                phase=phase, exchange="ALL",
                notes=(
                    f"Index value backfill for {industry_name} "
                    f"{from_date}→{to_date}, {len(date_list)} trading days"
                ),
            )
            FundamentalsRunRepository(conn).update_progress(
                run_id,
                progress_json={
                    "date_list": [d.isoformat() for d in date_list],
                    "cursor_index": 0,
                    "from_date": from_date.isoformat(),
                    "to_date": to_date.isoformat(),
                    "industry_name": industry_name,
                },
            )
        cursor_index = 0

    # ----- main loop ----- #
    succeeded = 0  # days we wrote a value for
    skipped = 0    # days we skipped (pre-base, no constituents, etc.)
    failed = 0     # days that errored

    try:
        for idx_pos in range(cursor_index, len(date_list)):
            td = date_list[idx_pos]
            try:
                cv = compute_index_value_for_date(
                    db_engine, industry_name, td,
                )
                if cv is None:
                    skipped += 1
                else:
                    with transaction(db_engine) as conn:
                        IndustryIndexValueRepository(conn).upsert_value(
                            industry_name,
                            trade_date=cv.trade_date,
                            value=cv.value,
                            constituents_used=cv.constituents_used,
                            constituents_missing=cv.constituents_missing,
                            renormalized=cv.renormalized,
                            effective_rebalance_date=cv.effective_rebalance_date,
                        )
                    succeeded += 1
            except Exception as e:
                logger.exception(
                    "Failed computing value for %s on %s: %s",
                    industry_name, td, e,
                )
                failed += 1

            if (idx_pos + 1) % CHECKPOINT_EVERY == 0:
                # Build a checkpoint snapshot — date_list re-encoded
                # so the progress_json is self-contained.
                with transaction(db_engine) as conn:
                    FundamentalsRunRepository(conn).update_progress(
                        run_id,
                        succeeded=succeeded,
                        failed=failed,
                        skipped=skipped,
                        progress_json={
                            "date_list": [d.isoformat() for d in date_list],
                            "cursor_index": idx_pos + 1,
                            "from_date": from_date.isoformat(),
                            "to_date": to_date.isoformat(),
                            "industry_name": industry_name,
                        },
                    )
                logger.info(
                    "Checkpoint: %d/%d dates (s=%d sk=%d f=%d) for %s",
                    idx_pos + 1, len(date_list),
                    succeeded, skipped, failed, industry_name,
                )

    except KeyboardInterrupt:
        logger.warning(
            "KeyboardInterrupt — closing index value run %d as PARTIAL",
            run_id,
        )
        with transaction(db_engine) as conn:
            FundamentalsRunRepository(conn).close(
                run_id, status="PARTIAL",
                succeeded=succeeded, failed=failed, skipped=skipped,
                notes="Interrupted by KeyboardInterrupt",
            )
        raise

    status = _terminal_status(succeeded, failed)
    with transaction(db_engine) as conn:
        FundamentalsRunRepository(conn).close(
            run_id, status=status,
            succeeded=succeeded, failed=failed, skipped=skipped,
        )
    logger.info(
        "Index value backfill run %d for %s finished: status=%s s=%d sk=%d f=%d",
        run_id, industry_name, status, succeeded, skipped, failed,
    )
    return run_id


def run_index_value_backfill_all(
    db_engine: Engine,
    *,
    from_date: date,
    to_date: date,
    force: bool = False,
) -> dict:
    """Run the daily index-value backfill for EVERY active index.

    Iterates ACTIVE industry_index rows and calls run_index_value_backfill for
    each over [from_date, to_date]. Each per-industry call walks its own trading
    days (from daily_market_cap), skips pre-base_date days, and writes
    industry_index_value rows using the chain-linked value model.

    Returns {'industries': [(name, run_id|None, error|None)], 'n_ok', 'n_failed'}.
    """
    from ..persistence.fundamentals_repo import IndustryIndexRepository

    with db_engine.connect() as conn:
        indices = IndustryIndexRepository(conn).list_all()
    active = [ix for ix in indices if getattr(ix, "status", "ACTIVE") == "ACTIVE"]

    results: list[tuple] = []
    n_ok = 0
    n_failed = 0
    for ix in active:
        name = ix.industry_name
        try:
            run_id = run_index_value_backfill(
                db_engine, name,
                from_date=from_date, to_date=to_date, force=force,
            )
            results.append((name, run_id, None))
            n_ok += 1
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "run_index_value_backfill_all: %s failed: %s", name, e,
            )
            results.append((name, None, str(e)))
            n_failed += 1

    logger.info(
        "run_index_value_backfill_all %s..%s: %d ok, %d failed (of %d active)",
        from_date, to_date, n_ok, n_failed, len(active),
    )
    return {"industries": results, "n_ok": n_ok, "n_failed": n_failed}
