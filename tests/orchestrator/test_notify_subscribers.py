"""Integration tests for `trip_ema.orchestrator.notify_subscribers`.

Synthetic fixture seeds:
  - 1-2 subscribers (with cadence + activity)
  - Optional current_signals rows + ticker/industry metadata for
    compose_email to read
  - Mocked send via monkeypatched `send_email` (production code is
    unchanged; only the test substitutes a fake)

The full pipeline is exercised end-to-end: count → find_due →
compose → send → log → update_last_notified.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, UTC
from unittest.mock import patch

import pytest
from sqlalchemy import text

from trip_ema.notifications.send_email import SendResult
from trip_ema.orchestrator.notify_subscribers import (
    NotifyResult,
    notify_subscribers,
)
from trip_ema.persistence import transaction


# ============================================================
# Helpers
# ============================================================

def _seed_subscriber(
    conn, *,
    subscriber_id: int = 1,
    address: str = "test@example.com",
    last_notified_at: datetime | None = None,
    cadence: int = 60,
    is_active: bool = True,
) -> int:
    conn.execute(text("""
        INSERT INTO subscribers (
            subscriber_id, transport, address,
            notify_cadence_minutes, last_notified_at, is_active
        ) VALUES (
            :sid, 'email', :addr, :c, :lna, :active
        )
        ON CONFLICT (subscriber_id) DO NOTHING
    """), {
        "sid": subscriber_id, "addr": address, "c": cadence,
        "lna": last_notified_at, "active": is_active,
    })
    return subscriber_id


def _seed_ticker_with_industry(
    conn, *,
    ticker_id: int, symbol: str,
    industry: str = "TestInd",
) -> None:
    conn.execute(text("""
        INSERT INTO tickers (ticker_id, exchange, symbol)
        VALUES (:id, 'NSE', :sym)
        ON CONFLICT (ticker_id) DO NOTHING
    """), {"id": ticker_id, "sym": symbol})
    conn.execute(text("""
        INSERT INTO instrument_industry (
            ticker_id, sector, industry, classification_source
        ) VALUES (:tid, 'Technology', :ind, 'MANUAL_OVERRIDE')
        ON CONFLICT (ticker_id) DO NOTHING
    """), {"tid": ticker_id, "ind": industry})


def _seed_current_signal(
    conn, *,
    ticker_id: int,
    signal_type: str = "BUY",
    regime_before: int = 0, regime_after: int = 1,
    price: float = 100.0,
) -> None:
    conn.execute(text("""
        INSERT INTO current_signals (
            ticker_id, timeframe, bar_timestamp,
            signal_type, regime_before, regime_after,
            price_at_signal, computed_at
        ) VALUES (
            :tid, '1W',
            TIMESTAMP WITH TIME ZONE '2025-10-06 00:00:00+00',
            :st, :rb, :ra, :p,
            TIMESTAMP WITH TIME ZONE '2025-10-09 14:30:00+00'
        )
        ON CONFLICT (ticker_id) DO NOTHING
    """), {
        "tid": ticker_id, "st": signal_type,
        "rb": regime_before, "ra": regime_after, "p": price,
    })


def _make_send_result(success: bool, error: str | None = None):
    """Build a SendResult fake."""
    def _factory(subscriber, body, *, api_key, from_address,
                 from_name="WeeklyStockSignals",
                 api_url=None, timeout_seconds=30.0,
                 now, poster=None):
        return SendResult(
            subscriber_id=subscriber.subscriber_id,
            address=subscriber.address,
            success=success,
            error=error,
            sent_at=now,
        )
    return _factory


# Standard kwargs for notify_subscribers (api key + sender supplied
# explicitly so tests don't depend on config loading).
_BREVO_KWARGS = {
    "brevo_api_key": "xkeysib-test",
    "brevo_from_address": "sender@example.com",
}


# ============================================================
# Early-exit paths
# ============================================================

class TestEarlyExits:

    def test_no_current_signals_returns_early(self, db_engine):
        """No rows in current_signals → no emails, zero everything."""
        result = notify_subscribers(
            db_engine,
            **_BREVO_KWARGS,
            cycle_id="X", now=datetime.now(UTC),
        )
        assert isinstance(result, NotifyResult)
        assert result.signals_in_current == 0
        assert result.subscribers_due == 0
        assert result.emails_sent == 0
        assert result.emails_failed == 0
        assert result.log_rows_written == 0
        assert result.errors == []

    def test_no_due_subscribers_returns_early(self, db_engine):
        """Signals exist but every subscriber is either inactive
        or not yet due → no emails."""
        with transaction(db_engine) as conn:
            # Inactive subscriber
            _seed_subscriber(
                conn, subscriber_id=1, is_active=False,
            )
            # Active but notified RECENTLY (cadence not elapsed)
            _seed_subscriber(
                conn, subscriber_id=2,
                address="other@example.com",
                last_notified_at=datetime.now(UTC),
                cadence=1440,   # 1 day cadence
            )
            _seed_ticker_with_industry(
                conn, ticker_id=1001, symbol="TICK1",
            )
            _seed_current_signal(conn, ticker_id=1001)

        result = notify_subscribers(
            db_engine,
            **_BREVO_KWARGS,
            cycle_id="X", now=datetime.now(UTC),
        )
        assert result.signals_in_current == 1
        assert result.subscribers_due == 0
        assert result.emails_sent == 0


# ============================================================
# Happy path
# ============================================================

class TestHappyPath:

    def test_one_subscriber_one_signal(self, db_engine):
        with transaction(db_engine) as conn:
            _seed_subscriber(conn)
            _seed_ticker_with_industry(
                conn, ticker_id=1001, symbol="TICK1",
            )
            _seed_current_signal(conn, ticker_id=1001)

        now = datetime(2026, 6, 27, 14, 30, tzinfo=UTC)
        with patch(
            "trip_ema.notifications.signal_report.send_email",
            side_effect=_make_send_result(success=True),
        ):
            result = notify_subscribers(
                db_engine,
                **_BREVO_KWARGS,
                cycle_id="HAPPY", now=now,
            )

        assert result.signals_in_current == 1
        assert result.subscribers_due == 1
        assert result.emails_composed == 1
        assert result.emails_sent == 1
        assert result.emails_failed == 0
        assert result.log_rows_written == 1
        assert result.errors == []

        # Verify notification_log got the row
        with db_engine.connect() as conn:
            log_row = conn.execute(text("""
                SELECT subscriber_id, signal_ticker_id,
                       signal_type, last_sent_at, event_id
                  FROM notification_log
            """)).fetchone()
        assert log_row is not None
        assert int(log_row[0]) == 1
        assert int(log_row[1]) == 1001
        assert log_row[2] == "BUY"
        assert log_row[3] == now
        assert log_row[4] is None    # V1 writer doesn't set event_id

        # Verify last_notified_at was updated
        with db_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT last_notified_at FROM subscribers
                 WHERE subscriber_id = 1
            """)).fetchone()
        assert row[0] == now

    def test_n_signals_produces_n_log_rows_per_subscriber(
        self, db_engine,
    ):
        """1 subscriber × 3 signals → 1 email + 3 log rows."""
        with transaction(db_engine) as conn:
            _seed_subscriber(conn)
            for i, sym in enumerate(["A", "B", "C"], start=1):
                tid = 1000 + i
                _seed_ticker_with_industry(
                    conn, ticker_id=tid, symbol=sym,
                )
                _seed_current_signal(conn, ticker_id=tid)

        with patch(
            "trip_ema.notifications.signal_report.send_email",
            side_effect=_make_send_result(success=True),
        ):
            result = notify_subscribers(
                db_engine,
                **_BREVO_KWARGS,
                cycle_id="X", now=datetime.now(UTC),
            )
        assert result.signals_in_current == 3
        assert result.emails_sent == 1
        assert result.log_rows_written == 3

    def test_m_subscribers_n_signals(self, db_engine):
        """3 subscribers × 2 signals → 3 emails + 6 log rows."""
        with transaction(db_engine) as conn:
            for sid in (1, 2, 3):
                _seed_subscriber(
                    conn, subscriber_id=sid,
                    address=f"sub{sid}@example.com",
                )
            _seed_ticker_with_industry(
                conn, ticker_id=1001, symbol="A",
            )
            _seed_current_signal(conn, ticker_id=1001)
            _seed_ticker_with_industry(
                conn, ticker_id=1002, symbol="B",
            )
            _seed_current_signal(conn, ticker_id=1002)

        with patch(
            "trip_ema.notifications.signal_report.send_email",
            side_effect=_make_send_result(success=True),
        ):
            result = notify_subscribers(
                db_engine,
                **_BREVO_KWARGS,
                cycle_id="X", now=datetime.now(UTC),
            )
        assert result.signals_in_current == 2
        assert result.subscribers_due == 3
        assert result.emails_sent == 3
        assert result.log_rows_written == 6


# ============================================================
# Failure isolation
# ============================================================

class TestFailureIsolation:

    def test_send_failure_does_not_update_last_notified(
        self, db_engine,
    ):
        with transaction(db_engine) as conn:
            _seed_subscriber(conn)
            _seed_ticker_with_industry(
                conn, ticker_id=1001, symbol="A",
            )
            _seed_current_signal(conn, ticker_id=1001)

        with patch(
            "trip_ema.notifications.signal_report.send_email",
            side_effect=_make_send_result(
                success=False, error="500 Internal Server Error",
            ),
        ):
            result = notify_subscribers(
                db_engine,
                **_BREVO_KWARGS,
                cycle_id="X", now=datetime.now(UTC),
            )

        assert result.emails_failed == 1
        assert result.emails_sent == 0
        assert result.log_rows_written == 0
        assert len(result.errors) == 1
        assert result.errors[0][0] == 1
        assert "500" in result.errors[0][1]

        # last_notified_at NOT updated → next cycle retries
        with db_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT last_notified_at FROM subscribers
                 WHERE subscriber_id = 1
            """)).fetchone()
        assert row[0] is None

    def test_one_subscriber_failure_doesnt_abort_others(
        self, db_engine,
    ):
        """Subscriber 1 fails, subscribers 2 and 3 succeed."""
        with transaction(db_engine) as conn:
            for sid in (1, 2, 3):
                _seed_subscriber(
                    conn, subscriber_id=sid,
                    address=f"sub{sid}@example.com",
                )
            _seed_ticker_with_industry(
                conn, ticker_id=1001, symbol="A",
            )
            _seed_current_signal(conn, ticker_id=1001)

        # Fake send: fail on subscriber 1, succeed on others
        def selective_fake(subscriber, body, *, api_key,
                          from_address,
                          from_name="WeeklyStockSignals",
                          api_url=None, timeout_seconds=30.0,
                          now, poster=None):
            ok = subscriber.subscriber_id != 1
            return SendResult(
                subscriber_id=subscriber.subscriber_id,
                address=subscriber.address,
                success=ok,
                error=None if ok else "simulated",
                sent_at=now,
            )

        with patch(
            "trip_ema.notifications.signal_report.send_email",
            side_effect=selective_fake,
        ):
            result = notify_subscribers(
                db_engine,
                **_BREVO_KWARGS,
                cycle_id="X", now=datetime.now(UTC),
            )
        assert result.subscribers_due == 3
        assert result.emails_sent == 2
        assert result.emails_failed == 1
        assert len(result.errors) == 1
        assert result.errors[0][0] == 1


# ============================================================
# Result invariants
# ============================================================

class TestResultInvariants:

    def test_cycle_id_propagates(self, db_engine):
        result = notify_subscribers(
            db_engine,
            **_BREVO_KWARGS,
            cycle_id="UNIQUE-RUN-42", now=datetime.now(UTC),
        )
        assert result.cycle_id == "UNIQUE-RUN-42"

    def test_started_finished_ordering(self, db_engine):
        result = notify_subscribers(
            db_engine,
            **_BREVO_KWARGS,
            cycle_id="X", now=datetime.now(UTC),
        )
        assert result.started_at <= result.finished_at
