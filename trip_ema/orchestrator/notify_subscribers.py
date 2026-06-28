"""V1 distribute stage: `notify_subscribers`.

The function the V1 cycle invokes after `recompute_market` (Step 6)
finishes. Reads `current_signals`, iterates subscribers due for
notification, composes + sends + logs.

Wires together:
  - SubscribersRepository.find_due_for_notification (cadence-aware)
  - Step 7's compose_email + send_email
  - Step 8's NotificationLogRepository
  - SubscribersRepository.update_last_notified

Error model:
  - Early exit when current_signals is empty (no emails to send).
  - Per-subscriber error isolation: a failure on one subscriber's
    send (4xx, timeout, etc.) doesn't abort the rest. The error is
    captured in result.errors; the subscriber's last_notified_at is
    NOT updated so the next cycle retries.
  - `send_email` never raises, but the surrounding logic (DB writes
    for logging, last_notified update) wraps in try/except to keep
    the loop alive.

Hooks (not implemented in V1, but called out in code for P2-A):
  - ML ensemble filter: between current_signals SELECT and the
    per-subscriber loop. Could reorder or drop signals based on a
    learned scoring model.
  - Per-subscriber rules JSON filter: inside the per-subscriber loop,
    before compose_email. Drops signals that don't match a
    subscriber's rules.
"""
from __future__ import annotations

from collections.abc import Callable

import logging
from dataclasses import dataclass, field
from datetime import datetime, UTC

from sqlalchemy.engine import Engine

from ..notifications.compose_email import compose_email
from ..notifications.send_email import send_email
from ..persistence import transaction
from ..persistence.current_signals_repo import CurrentSignalsRepository
from ..persistence.notification_log_repo import (
    NotificationLogEntry,
    NotificationLogRepository,
)
from ..persistence.subscribers_repo import (
    SubscriberRecord,
    SubscribersRepository,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NotifyResult:
    """Aggregated outcome of one notify_subscribers invocation.

    Counters are intentionally aggregate — per-subscriber detail is
    in `errors` (failures only) and the notification_log table
    (successes). Callers reaching for "which subscribers got which
    signals" should query notification_log directly.
    """
    cycle_id: str
    started_at: datetime
    finished_at: datetime
    signals_in_current: int
    subscribers_due: int
    emails_composed: int
    emails_sent: int
    emails_failed: int
    log_rows_written: int
    errors: list[tuple[int, str]] = field(default_factory=list)



# === STEP 13D1: progress_cb ===
def _emit_notify_progress(
    cb,
    msg: str,
) -> None:
    """Stage-progress emit for the notify stage. No-op when cb is
    None. Stage name is always 'notify'.
    """
    if cb is not None:
        try:
            cb("notify", msg)
        except Exception:
            logger.exception(
                "progress_cb raised in notify_subscribers; "
                "continuing",
            )


def notify_subscribers(
    db_engine: Engine,
    *,
    brevo_api_key: str,
    brevo_from_address: str,
    brevo_from_name: str = "WeeklyStockSignals",
    cycle_id: str,
    now: datetime,
    progress_cb: "Callable[[str, str], None] | None" = None,
) -> NotifyResult:
    """V1 distribute stage. See module docstring for the flow.

    Args:
        db_engine: SQLAlchemy Engine. Per-stage connections opened
            internally.
        brevo_api_key: from `trip_ema.config.brevo_secrets` (the
            caller — usually app.py's wiring layer — loads it).
        brevo_from_address: verified Brevo sender, e.g.
            `weeklystocksignals@outlook.com`.
        brevo_from_name: display name. Defaults to
            'WeeklyStockSignals'.
        cycle_id: cycle identifier (e.g. '20260626-1500'). Appears in
            email subject + body.
        now: wall-clock for this cycle. Stamps log rows + governs
            cadence filtering of subscribers (find_due_for_notification).

    Returns:
        NotifyResult with aggregate counters + per-subscriber errors.
    """
    started_at = datetime.now(UTC)
    logger.info(
        "notify_subscribers[%s] starting (now=%s)",
        cycle_id, now.isoformat(),
    )
    _emit_notify_progress(progress_cb, "running")

    # ---- Early exit: any signals to send? ----
    with db_engine.connect() as conn:
        signal_count = CurrentSignalsRepository(conn).count()
    if signal_count == 0:
        logger.info(
            "notify_subscribers[%s] early exit: current_signals empty",
            cycle_id,
        )
        finished_at = datetime.now(UTC)
        _emit_notify_progress(progress_cb, "done: skipped (no signals)")
        return NotifyResult(
            cycle_id=cycle_id,
            started_at=started_at, finished_at=finished_at,
            signals_in_current=0, subscribers_due=0,
            emails_composed=0, emails_sent=0, emails_failed=0,
            log_rows_written=0,
        )

    # ---- Find due subscribers ----
    with db_engine.connect() as conn:
        subscribers = SubscribersRepository(
            conn,
        ).find_due_for_notification(now)
    if not subscribers:
        logger.info(
            "notify_subscribers[%s] no subscribers due (signals=%d)",
            cycle_id, signal_count,
        )
        finished_at = datetime.now(UTC)
        _emit_notify_progress(progress_cb, "done: skipped (no subscribers due)")
        return NotifyResult(
            cycle_id=cycle_id,
            started_at=started_at, finished_at=finished_at,
            signals_in_current=signal_count,
            subscribers_due=0,
            emails_composed=0, emails_sent=0, emails_failed=0,
            log_rows_written=0,
        )

    # ---- Snapshot current_signals once for logging ----
    # compose_email reads + joins independently for body building,
    # but we want a stable list for the log writer. Within one cycle
    # current_signals is stable (the compute side finished before
    # distribute started), so the two reads see identical data.
    with db_engine.connect() as conn:
        signals_snapshot = CurrentSignalsRepository(conn).list_all()
    log_entries_template = [
        (s.ticker_id, s.timeframe, s.bar_timestamp,
         s.signal_type, s.price_at_signal)
        for s in signals_snapshot
    ]

    # ---- Per-subscriber loop with isolation ----
    emails_composed = 0
    emails_sent = 0
    emails_failed = 0
    log_rows_written = 0
    errors: list[tuple[int, str]] = []

    for subscriber in subscribers:
        try:
            outcome = _process_one_subscriber(
                db_engine, subscriber,
                cycle_id=cycle_id, now=now,
                brevo_api_key=brevo_api_key,
                brevo_from_address=brevo_from_address,
                brevo_from_name=brevo_from_name,
                log_entries_template=log_entries_template,
            )
            emails_composed += outcome["composed"]
            emails_sent += outcome["sent"]
            emails_failed += outcome["failed"]
            log_rows_written += outcome["log_rows"]
            if outcome["error"] is not None:
                errors.append(
                    (subscriber.subscriber_id, outcome["error"]),
                )
        except Exception as e:
            # Defensive: _process_one_subscriber shouldn't propagate,
            # but if it does we still want to keep the loop alive.
            emails_failed += 1
            errors.append(
                (subscriber.subscriber_id,
                 f"{type(e).__name__}: {e}"),
            )
            logger.exception(
                "notify_subscribers[%s] uncaught error for "
                "subscriber_id=%d",
                cycle_id, subscriber.subscriber_id,
            )

    finished_at = datetime.now(UTC)
    duration_s = (finished_at - started_at).total_seconds()
    logger.info(
        "notify_subscribers[%s] finished in %.2fs: sent=%d failed=%d "
        "log_rows=%d errors=%d",
        cycle_id, duration_s, emails_sent, emails_failed,
        log_rows_written, len(errors),
    )

    _emit_notify_progress(progress_cb, "done")
    return NotifyResult(
        cycle_id=cycle_id,
        started_at=started_at, finished_at=finished_at,
        signals_in_current=signal_count,
        subscribers_due=len(subscribers),
        emails_composed=emails_composed,
        emails_sent=emails_sent,
        emails_failed=emails_failed,
        log_rows_written=log_rows_written,
        errors=errors,
    )


def _process_one_subscriber(
    db_engine: Engine,
    subscriber: SubscriberRecord,
    *,
    cycle_id: str,
    now: datetime,
    brevo_api_key: str,
    brevo_from_address: str,
    brevo_from_name: str,
    log_entries_template: list[
        tuple[int, str, datetime, str, float]
    ],
) -> dict:
    """One subscriber's full pipeline: compose → send → log → mark
    notified. Returns a dict of counters + optional error string.

    Catches its own exceptions to keep notify_subscribers's loop
    isolated, but the outer try/except remains as a safety net.
    """
    # Compose
    with db_engine.connect() as conn:
        body = compose_email(conn, subscriber, cycle_id, now)
    if body is None:
        # Rare: current_signals went empty between the upfront count
        # and this per-subscriber compose. Defensive.
        logger.warning(
            "notify_subscribers[%s] compose returned None for "
            "subscriber_id=%d; skipping",
            cycle_id, subscriber.subscriber_id,
        )
        return {
            "composed": 0, "sent": 0, "failed": 0, "log_rows": 0,
            "error": None,
        }

    composed = 1

    # Send (never raises — returns SendResult)
    result = send_email(
        subscriber, body,
        api_key=brevo_api_key,
        from_address=brevo_from_address,
        from_name=brevo_from_name,
        now=now,
    )
    if not result.success:
        # last_notified_at NOT updated → next cycle retries.
        logger.warning(
            "notify_subscribers[%s] send failed for "
            "subscriber_id=%d: %s",
            cycle_id, subscriber.subscriber_id, result.error,
        )
        return {
            "composed": composed, "sent": 0, "failed": 1,
            "log_rows": 0,
            "error": result.error or "send failed",
        }

    # Log + update last_notified — both inside one transaction so
    # they roll back together if anything goes wrong.
    try:
        log_rows = 0
        with transaction(db_engine) as conn:
            log_repo = NotificationLogRepository(conn)
            entries = [
                NotificationLogEntry(
                    subscriber_id=subscriber.subscriber_id,
                    signal_ticker_id=tid,
                    signal_timeframe=tf,
                    signal_bar_timestamp=bt,
                    signal_type=stype,
                    signal_price=sp,
                    sent_at=now,
                )
                for (tid, tf, bt, stype, sp) in log_entries_template
            ]
            log_rows = log_repo.record_many(entries)
            SubscribersRepository(conn).update_last_notified(
                subscriber.subscriber_id, now,
            )
    except Exception as e:
        # Email DID go out but the bookkeeping failed. Log the issue
        # but DON'T mark the send as failed — the user got the email.
        # Operator can reconcile via Brevo's delivery log if needed.
        logger.exception(
            "notify_subscribers[%s] email sent but bookkeeping "
            "failed for subscriber_id=%d: %s",
            cycle_id, subscriber.subscriber_id, e,
        )
        return {
            "composed": composed, "sent": 1, "failed": 0,
            "log_rows": 0,
            "error": (
                f"email sent but log/last_notified failed: "
                f"{type(e).__name__}: {e}"
            ),
        }

    return {
        "composed": composed, "sent": 1, "failed": 0,
        "log_rows": log_rows,
        "error": None,
    }
