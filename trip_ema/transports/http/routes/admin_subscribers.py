"""Subscribers admin endpoints (Slice 4).

CRUD over the `subscribers` table plus its child `subscriptions` rows
(per-subscriber filters by ticker / signal). The PySide Subscribers
tab is the primary consumer.

  GET    /v1/admin/subscribers                       — list all
  GET    /v1/admin/subscribers/{id}                  — fetch one
  POST   /v1/admin/subscribers                       — create
  PATCH  /v1/admin/subscribers/{id}                  — update fields
  DELETE /v1/admin/subscribers/{id}                  — delete (cascades)
  PUT    /v1/admin/subscribers/{id}/filters          — replace filter set
  POST   /v1/admin/subscribers/{id}/test-notification — fire a one-shot
                                                       test message

Auth: bearer token via require_admin_token (same as ETL admin).
"""

from __future__ import annotations

import logging
from datetime import datetime, UTC

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import Engine, text

from ....config import Config
from ..dependencies import (
    get_config, get_db_engine, require_admin_token,
)
from ..schemas import (
    SubscriberCreatePayload,
    SubscriberPayload,
    SubscriberUpdatePayload,
    SubscribersListPayload,
    SubscriptionFilterPayload,
    TestNotificationResultPayload,
)

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/v1/admin/subscribers",
    tags=["admin", "subscribers"],
    dependencies=[Depends(require_admin_token)],
)


# ---------- read endpoints ---------- #


@router.get("", response_model=SubscribersListPayload)
def list_subscribers(
    db_engine: Engine = Depends(get_db_engine),
) -> SubscribersListPayload:
    """Every subscriber, with embedded filter list. ~tens to low
    hundreds in practice; no pagination yet."""
    with db_engine.connect() as conn:
        subscriber_rows = list(conn.execute(text("""
            SELECT subscriber_id, transport, address, notify_cadence_minutes,
                   last_notified_at, is_active, created_at
            FROM subscribers
            ORDER BY subscriber_id ASC
        """)))
        filters_by_sub = _load_filters_grouped(conn, [r.subscriber_id for r in subscriber_rows])

    return SubscribersListPayload(
        subscribers=[
            _row_to_payload(r, filters_by_sub.get(r.subscriber_id, []))
            for r in subscriber_rows
        ],
        total=len(subscriber_rows),
    )


@router.get("/{subscriber_id}", response_model=SubscriberPayload)
def get_subscriber(
    subscriber_id: int,
    db_engine: Engine = Depends(get_db_engine),
) -> SubscriberPayload:
    with db_engine.connect() as conn:
        row = conn.execute(text("""
            SELECT subscriber_id, transport, address, notify_cadence_minutes,
                   last_notified_at, is_active, created_at
            FROM subscribers WHERE subscriber_id = :sid
        """), {"sid": subscriber_id}).first()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Subscriber {subscriber_id} not found",
            )
        filters = _load_filters_grouped(conn, [subscriber_id]).get(subscriber_id, [])
    return _row_to_payload(row, filters)


# ---------- write endpoints ---------- #


@router.post("", response_model=SubscriberPayload, status_code=201)
def create_subscriber(
    body: SubscriberCreatePayload,
    db_engine: Engine = Depends(get_db_engine),
) -> SubscriberPayload:
    """Insert a new subscriber and, if `filters` was provided, its
    initial subscriptions. If `filters` is None, the subscriber is
    created with NO filters — meaning the SubscriberDistributor will
    not match it to any event until filters are added later. Callers
    should typically supply at least one filter row, even if it's
    'all tickers, all signals' (ticker_id=null, signal_filter=null)."""
    with db_engine.begin() as conn:
        result = conn.execute(text("""
            INSERT INTO subscribers
                (transport, address, notify_cadence_minutes, is_active)
            VALUES (:t, :a, :c, :act)
            RETURNING subscriber_id, transport, address,
                      notify_cadence_minutes, last_notified_at,
                      is_active, created_at
        """), {
            "t": body.transport, "a": body.address,
            "c": body.notify_cadence_minutes, "act": body.is_active,
        }).first()
        assert result is not None, "INSERT ... RETURNING produced no row"
        sub_id = result.subscriber_id
        if body.filters:
            _replace_filters(conn, sub_id, body.filters)
        filters = _load_filters_grouped(conn, [sub_id]).get(sub_id, [])

    logger.info(
        "Admin: subscriber created",
        extra={"subscriber_id": sub_id, "transport": body.transport},
    )
    return _row_to_payload(result, filters)


@router.patch("/{subscriber_id}", response_model=SubscriberPayload)
def update_subscriber(
    subscriber_id: int,
    body: SubscriberUpdatePayload,
    db_engine: Engine = Depends(get_db_engine),
) -> SubscriberPayload:
    """Partial update. Filters are NOT touched here — use PUT
    /filters for that."""
    # Build a dynamic SET clause from supplied fields. PATCH semantics:
    # absent field = unchanged.
    updates = []
    params: dict = {"sid": subscriber_id}
    for field in ("transport", "address", "is_active"):
        v = getattr(body, field)
        if v is not None:
            updates.append(f"{field} = :{field}")
            params[field] = v
    if body.notify_cadence_minutes is not None:
        updates.append("notify_cadence_minutes = :notify_cadence_minutes")
        params["notify_cadence_minutes"] = body.notify_cadence_minutes
    if not updates:
        # No-op — still return current row for consistency
        return get_subscriber(subscriber_id, db_engine)

    sql = f"""
        UPDATE subscribers SET {', '.join(updates)}
        WHERE subscriber_id = :sid
        RETURNING subscriber_id, transport, address,
                  notify_cadence_minutes, last_notified_at,
                  is_active, created_at
    """
    with db_engine.begin() as conn:
        row = conn.execute(text(sql), params).first()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Subscriber {subscriber_id} not found",
            )
        filters = _load_filters_grouped(conn, [subscriber_id]).get(subscriber_id, [])
    return _row_to_payload(row, filters)


@router.delete("/{subscriber_id}", status_code=204)
def delete_subscriber(
    subscriber_id: int,
    db_engine: Engine = Depends(get_db_engine),
):
    """Cascades to subscriptions and notification_log via FK
    ON DELETE CASCADE."""
    with db_engine.begin() as conn:
        result = conn.execute(text("""
            DELETE FROM subscribers WHERE subscriber_id = :sid
        """), {"sid": subscriber_id})
        if result.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Subscriber {subscriber_id} not found",
            )
    logger.info("Admin: subscriber deleted", extra={"subscriber_id": subscriber_id})
    return None


@router.put("/{subscriber_id}/filters", response_model=SubscriberPayload)
def replace_filters(
    subscriber_id: int,
    filters: list[SubscriptionFilterPayload],
    db_engine: Engine = Depends(get_db_engine),
) -> SubscriberPayload:
    """Replace the entire filter set in one call. DELETEs old rows,
    INSERTs new. This is simpler than per-filter add/remove from
    the GUI's perspective — the GUI maintains the list locally and
    sends the canonical state."""
    with db_engine.begin() as conn:
        # Verify subscriber exists
        exists = conn.execute(text("""
            SELECT 1 FROM subscribers WHERE subscriber_id = :sid
        """), {"sid": subscriber_id}).first()
        if exists is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Subscriber {subscriber_id} not found",
            )
        _replace_filters(conn, subscriber_id, filters)
        # Re-fetch the subscriber to return canonical state
        row = conn.execute(text("""
            SELECT subscriber_id, transport, address, notify_cadence_minutes,
                   last_notified_at, is_active, created_at
            FROM subscribers WHERE subscriber_id = :sid
        """), {"sid": subscriber_id}).first()
        loaded = _load_filters_grouped(conn, [subscriber_id]).get(subscriber_id, [])
    return _row_to_payload(row, loaded)


# ---------- test-notification endpoint ---------- #


@router.post(
    "/{subscriber_id}/test-notification",
    response_model=TestNotificationResultPayload,
)
def test_notification(
    subscriber_id: int,
    db_engine: Engine = Depends(get_db_engine),
    config: Config = Depends(get_config),
) -> TestNotificationResultPayload:
    """Fire a one-shot test message to this subscriber's transport
    and address. Doesn't go through the distributor — we bypass it
    so the test message isn't recorded in notification_log and
    doesn't affect cadence tracking.

    Returns success/failure of the transport's send() call.
    """
    with db_engine.connect() as conn:
        row = conn.execute(text("""
            SELECT subscriber_id, transport, address
            FROM subscribers WHERE subscriber_id = :sid
        """), {"sid": subscriber_id}).first()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Subscriber {subscriber_id} not found",
            )

    now = datetime.now(UTC)
    transport_name = row.transport
    address = row.address

    # Build a tiny one-event NotificationBatch.
    from ....domain.events import NotificationBatch, SignalEvent
    from ....domain import ChangeSignal, TickerRef, Timeframe

    synthetic_event = SignalEvent(
        ticker=TickerRef(exchange="TEST", symbol="TEST_PING", instrument="EQ"),
        timeframe=Timeframe.WEEKLY,
        bar_timestamp=now,
        signal=ChangeSignal.BUY,
        price_at_signal=100.0,
        computed_at=now,
    )
    batch = NotificationBatch(
        subscriber_id=subscriber_id,
        events=[synthetic_event],
        composed_at=now,
    )

    succeeded = False
    notes = ""
    try:
        transport = _build_transport(transport_name, config)
        succeeded = bool(transport.send(address, batch))
        notes = "Send returned True." if succeeded else "Send returned False."
    except Exception as e:
        notes = f"Transport raised: {e}"
        logger.exception(
            "Test notification raised",
            extra={"subscriber_id": subscriber_id, "transport": transport_name},
        )

    return TestNotificationResultPayload(
        subscriber_id=subscriber_id,
        transport=transport_name,
        address=address,
        succeeded=succeeded,
        sent_at=now,
        notes=notes,
    )


# ---------- helpers ---------- #


def _load_filters_grouped(
    conn, subscriber_ids: list[int],
) -> dict[int, list[SubscriptionFilterPayload]]:
    """Fetch all subscriptions for the given subscribers, grouped by
    subscriber_id. Single query with a left join to tickers so we
    can include the symbol+exchange in the response (saves a round
    trip for the GUI)."""
    if not subscriber_ids:
        return {}
    rows = conn.execute(text("""
        SELECT s.subscription_id, s.subscriber_id, s.ticker_id,
               s.signal_filter, t.symbol AS ticker_symbol,
               t.exchange AS ticker_exchange
        FROM subscriptions s
        LEFT JOIN tickers t ON s.ticker_id = t.ticker_id
        WHERE s.subscriber_id = ANY(:sids)
        ORDER BY s.subscriber_id ASC, s.subscription_id ASC
    """), {"sids": subscriber_ids})
    out: dict[int, list[SubscriptionFilterPayload]] = {}
    for r in rows:
        out.setdefault(r.subscriber_id, []).append(SubscriptionFilterPayload(
            subscription_id=r.subscription_id,
            ticker_id=r.ticker_id,
            ticker_symbol=r.ticker_symbol,
            ticker_exchange=r.ticker_exchange,
            signal_filter=r.signal_filter,
        ))
    return out


def _replace_filters(
    conn, subscriber_id: int,
    filters: list[SubscriptionFilterPayload],
) -> None:
    """DELETE then INSERT inside the caller's transaction. Validates
    that any ticker_ids reference existing tickers."""
    if filters:
        # Validate ticker references in one pass
        wanted_ids = [f.ticker_id for f in filters if f.ticker_id is not None]
        if wanted_ids:
            found_rows = conn.execute(text("""
                SELECT ticker_id FROM tickers WHERE ticker_id = ANY(:ids)
            """), {"ids": wanted_ids})
            found = {r.ticker_id for r in found_rows}
            missing = set(wanted_ids) - found
            if missing:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Unknown ticker_ids: {sorted(missing)}",
                )

    conn.execute(text("""
        DELETE FROM subscriptions WHERE subscriber_id = :sid
    """), {"sid": subscriber_id})
    for f in filters:
        conn.execute(text("""
            INSERT INTO subscriptions
                (subscriber_id, ticker_id, signal_filter)
            VALUES (:sid, :tid, :sf)
        """), {
            "sid": subscriber_id,
            "tid": f.ticker_id,
            "sf": f.signal_filter,
        })


def _row_to_payload(
    row, filters: list[SubscriptionFilterPayload],
) -> SubscriberPayload:
    return SubscriberPayload(
        subscriber_id=row.subscriber_id,
        transport=row.transport,
        address=row.address,
        notify_cadence_minutes=row.notify_cadence_minutes,
        is_active=row.is_active,
        last_notified_at=row.last_notified_at,
        created_at=row.created_at,
        filters=filters,
    )


def _build_transport(transport_name: str, config: Config):
    """Construct a transport for the test-notification call. We
    bypass the SubscriberDistributor / circuit breaker — the test
    is supposed to verify reachability of the underlying channel,
    not the production routing fabric.

    Reuses the same transport-builder helpers app.py uses so the
    test sends through the same code paths production does."""
    if transport_name == "email":
        # 4 dots: trip_ema.transports.http.routes -> trip_ema.app
        # (this used to be 3 dots, resolving to the non-existent
        # trip_ema.transports.app; silently broken until the test-
        # notification endpoint was first exercised.)
        from ....app import _build_email_transport_or_null
        return _build_email_transport_or_null(config)
    if transport_name == "webhook":
        from ....notifications.webhook_transport import WebhookTransport
        return WebhookTransport()
    if transport_name == "slack":
        # Slack uses the webhook transport under the hood — same
        # payload contract.
        from ....notifications.webhook_transport import WebhookTransport
        return WebhookTransport()
    if transport_name == "sms":
        # No SMS transport implemented yet; fail loudly.
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="SMS transport not yet implemented.",
        )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unknown transport: {transport_name}",
    )
