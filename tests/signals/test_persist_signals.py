"""Tests for `trip_ema.signals.persist_signals.persist_signal_events`.

Two layers:

  - **Unit tests** (`TestPersistSignalEvents*Unit`) — pure-Python, fast.
    Inject a fake repository to verify the per-event loop, the
    cache write-through, the NO_CHANGE guard, and order preservation
    without touching a database.

  - **Integration tests** (`TestPersistSignalEvents*Integration`) — real
    Postgres. Verify ON CONFLICT DO NOTHING idempotency end-to-end,
    `computed_at` is honoured verbatim, and the table actually receives
    the rows we expect. Skipped automatically when a sandbox cluster
    isn't reachable.

The integration tests assume a Postgres 16 sandbox running as the
`postgres` superuser (per the project's testing convention). They
create a throwaway schema in a transaction that's rolled back at
test teardown — no permanent state lands.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import MagicMock

import pytest

from trip_ema.domain import (
    ChangeSignal,
    SignalEvent,
    TickerRef,
    Timeframe,
)
from trip_ema.signals import persist_signal_events


# ---------------------------------------------------------------------------
# Shared event builders.
# ---------------------------------------------------------------------------

_TICKER = TickerRef(exchange="NSE", symbol="SANSERA", instrument="EQ")


def _event(
    *,
    signal: ChangeSignal = ChangeSignal.BUY,
    bar_timestamp: Optional[datetime] = None,
    computed_at: Optional[datetime] = None,
    price: float = 1327.10,
    timeframe: Timeframe = Timeframe.WEEKLY,
) -> SignalEvent:
    """A canonical-ish SignalEvent. Override only what each test cares about."""
    return SignalEvent(
        ticker=_TICKER,
        timeframe=timeframe,
        bar_timestamp=bar_timestamp or datetime(2025, 6, 16, 0, 0, tzinfo=timezone.utc),
        signal=signal,
        price_at_signal=price,
        computed_at=computed_at or datetime(2025, 6, 16, 0, 0, tzinfo=timezone.utc),
    )


# ===========================================================================
# UNIT TESTS — no database, no real repository
# ===========================================================================


class TestPersistSignalEventsUnit:
    """Verify the per-event loop with a fake repository.

    We patch `SignalsRepository` at the module boundary so
    `persist_signal_events` exercises its own loop / guard / cache logic
    without needing a database.
    """

    @pytest.fixture
    def fake_repo_cls(self, monkeypatch):
        """Patch the SignalsRepository import used by persist_signals.

        The fake's `insert_event` returns sequential integers so tests
        can verify event_ids come back in input order.
        """
        instances = []

        class FakeRepo:
            def __init__(self, conn):
                self.conn = conn
                self._next_id = 1000
                self.insert_calls: list[tuple[int, SignalEvent]] = []
                instances.append(self)

            def insert_event(self, ticker_id: int, event: SignalEvent) -> int:
                self.insert_calls.append((ticker_id, event))
                self._next_id += 1
                return self._next_id

        monkeypatch.setattr(
            "trip_ema.signals.persist_signals.SignalsRepository",
            FakeRepo,
        )
        return instances

    def test_inserts_each_event_once_and_returns_ids_in_order(self, fake_repo_cls):
        events = [
            _event(signal=ChangeSignal.BUY,
                   bar_timestamp=datetime(2025, 6, 16, tzinfo=timezone.utc)),
            _event(signal=ChangeSignal.EXIT_FROM_BUY,
                   bar_timestamp=datetime(2025, 7, 14, tzinfo=timezone.utc)),
            _event(signal=ChangeSignal.SELL,
                   bar_timestamp=datetime(2025, 7, 21, tzinfo=timezone.utc)),
        ]
        conn = MagicMock(name="connection")

        ids = persist_signal_events(conn, ticker_id=1806, events=events)

        assert ids == [1001, 1002, 1003], "event_ids must be in input order"

        repo = fake_repo_cls[0]
        assert len(repo.insert_calls) == 3
        assert all(call[0] == 1806 for call in repo.insert_calls)
        assert [call[1].signal for call in repo.insert_calls] == [
            ChangeSignal.BUY,
            ChangeSignal.EXIT_FROM_BUY,
            ChangeSignal.SELL,
        ]

    def test_no_change_event_raises_value_error(self, fake_repo_cls):
        """The guardrail: NO_CHANGE must never be persisted."""
        conn = MagicMock()
        events = [_event(signal=ChangeSignal.NO_CHANGE)]

        with pytest.raises(ValueError, match="NO_CHANGE"):
            persist_signal_events(conn, ticker_id=1806, events=events)

        # And the repo must NOT have been called.
        repo = fake_repo_cls[0]
        assert repo.insert_calls == []

    def test_no_change_event_among_others_short_circuits(self, fake_repo_cls):
        """A NO_CHANGE in the middle of a batch raises and stops the loop.

        We don't promise atomicity here (the caller owns the transaction
        boundary); but we do promise that the loop stops at the first
        bad event so the writer's bug surfaces immediately rather than
        being masked by later good events.
        """
        conn = MagicMock()
        events = [
            _event(signal=ChangeSignal.BUY,
                   bar_timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc)),
            _event(signal=ChangeSignal.NO_CHANGE,
                   bar_timestamp=datetime(2025, 1, 8, tzinfo=timezone.utc)),
            _event(signal=ChangeSignal.SELL,
                   bar_timestamp=datetime(2025, 1, 15, tzinfo=timezone.utc)),
        ]

        with pytest.raises(ValueError, match="NO_CHANGE"):
            persist_signal_events(conn, ticker_id=1806, events=events)

        # First event was inserted before the loop hit the bad one.
        repo = fake_repo_cls[0]
        assert len(repo.insert_calls) == 1
        assert repo.insert_calls[0][1].signal == ChangeSignal.BUY

    def test_cache_writer_invoked_after_each_insert(self, fake_repo_cls):
        events = [
            _event(signal=ChangeSignal.BUY,
                   bar_timestamp=datetime(2025, 6, 16, tzinfo=timezone.utc)),
            _event(signal=ChangeSignal.EXIT_FROM_BUY,
                   bar_timestamp=datetime(2025, 7, 14, tzinfo=timezone.utc)),
        ]
        conn = MagicMock()
        cache_calls: list[tuple[int, Timeframe, SignalEvent]] = []

        def cache_writer(ticker_id, timeframe, event):
            cache_calls.append((ticker_id, timeframe, event))

        ids = persist_signal_events(
            conn, ticker_id=1806, events=events,
            signal_cache_writer=cache_writer,
        )

        assert len(ids) == 2
        assert len(cache_calls) == 2
        # ticker_id is threaded through
        assert all(call[0] == 1806 for call in cache_calls)
        # timeframe matches the event's
        assert all(call[1] == Timeframe.WEEKLY for call in cache_calls)
        # event passed verbatim
        assert cache_calls[0][2].signal == ChangeSignal.BUY
        assert cache_calls[1][2].signal == ChangeSignal.EXIT_FROM_BUY

    def test_cache_writer_none_means_no_callback(self, fake_repo_cls):
        """Default behaviour: no cache writer = no callback. Verified
        by absence of any side effect."""
        events = [_event(signal=ChangeSignal.BUY)]
        conn = MagicMock()

        # No exception, no error. Just the insert.
        ids = persist_signal_events(
            conn, ticker_id=1806, events=events,
            signal_cache_writer=None,
        )
        assert len(ids) == 1

    def test_empty_iterable_is_no_op(self, fake_repo_cls):
        """Zero events = empty return list. No repo construction needed
        in principle, but it's cheap and we don't optimise for this."""
        conn = MagicMock()

        ids = persist_signal_events(conn, ticker_id=1806, events=[])

        assert ids == []
        repo = fake_repo_cls[0]
        assert repo.insert_calls == []

    def test_iterable_consumed_once(self, fake_repo_cls):
        """If the caller passes a generator, we consume it once cleanly."""
        def gen():
            yield _event(signal=ChangeSignal.BUY,
                         bar_timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc))
            yield _event(signal=ChangeSignal.EXIT_FROM_BUY,
                         bar_timestamp=datetime(2025, 1, 8, tzinfo=timezone.utc))

        conn = MagicMock()
        ids = persist_signal_events(conn, ticker_id=1806, events=gen())

        assert ids == [1001, 1002]

    def test_computed_at_honoured_verbatim(self, fake_repo_cls):
        """The whole point of the writer split — backfill stamps the
        simulated day, live stamps NOW(). The persist layer must not
        touch event.computed_at."""
        simulated_day = datetime(2024, 3, 15, 23, 59, 59, tzinfo=timezone.utc)
        events = [_event(signal=ChangeSignal.BUY, computed_at=simulated_day)]
        conn = MagicMock()

        persist_signal_events(conn, ticker_id=1806, events=events)

        repo = fake_repo_cls[0]
        passed_event = repo.insert_calls[0][1]
        assert passed_event.computed_at == simulated_day


# ===========================================================================
# INTEGRATION TESTS — real Postgres
# ===========================================================================

def _pg_available() -> bool:
    """Return True iff a sandbox Postgres cluster looks reachable.

    We check via psycopg2 connect rather than a TCP probe because the
    sandbox uses a Unix socket and the cluster might be on a
    non-default port; the env-var-driven URL is the authoritative
    location."""
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        return False
    url = os.environ.get(
        "TRIP_EMA_TEST_DB_URL",
        "postgresql://postgres@/postgres",
    )
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        conn.close()
        return True
    except Exception:
        return False


pytestmark_integration = pytest.mark.skipif(
    not _pg_available(),
    reason="sandbox Postgres not reachable; set TRIP_EMA_TEST_DB_URL",
)


@pytestmark_integration
class TestPersistSignalEventsIntegration:
    """End-to-end: real Postgres, real SignalsRepository, real ON
    CONFLICT DO NOTHING. Each test creates a fresh throwaway DB and
    drops it on teardown so tests don't bleed state into each other.
    """

    @pytest.fixture
    def db_url(self):
        return os.environ.get(
            "TRIP_EMA_TEST_DB_URL",
            "postgresql://postgres@/postgres",
        )

    @pytest.fixture
    def engine(self, db_url):
        """Create a throwaway database for the test, yield a SQLAlchemy
        engine bound to it, drop the database on teardown."""
        import psycopg2
        from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
        from sqlalchemy import create_engine

        test_db = f"trip_ema_test_{os.getpid()}"

        # Create
        admin = psycopg2.connect(db_url)
        admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        with admin.cursor() as c:
            c.execute(f"DROP DATABASE IF EXISTS {test_db}")
            c.execute(f"CREATE DATABASE {test_db}")
        admin.close()

        # Connect via SQLAlchemy
        test_url = db_url.rsplit("/", 1)[0] + f"/{test_db}"
        engine = create_engine(test_url)

        # Materialise the schema slice we need.
        with engine.begin() as conn:
            conn.exec_driver_sql("""
                CREATE TABLE tickers (
                    ticker_id   SERIAL PRIMARY KEY,
                    exchange    VARCHAR(8)  NOT NULL,
                    symbol      VARCHAR(64) NOT NULL,
                    instrument  VARCHAR(8)  NOT NULL
                );
                CREATE TABLE industry_index (
                    industry_name VARCHAR(128) PRIMARY KEY
                );
                CREATE TABLE signal_events (
                    event_id        BIGSERIAL PRIMARY KEY,
                    ticker_id       INTEGER REFERENCES tickers(ticker_id),
                    timeframe       VARCHAR(8),
                    industry_name   VARCHAR(128)
                                    REFERENCES industry_index(industry_name),
                    bar_timestamp   TIMESTAMPTZ NOT NULL,
                    signal_type     VARCHAR(16) NOT NULL,
                    regime_before   INTEGER,
                    regime_after    INTEGER,
                    price_at_signal DOUBLE PRECISION NOT NULL,
                    computed_at     TIMESTAMPTZ NOT NULL
                );
                -- Unique index that backs ON CONFLICT DO NOTHING in
                -- SignalsRepository.insert_event. In production this is a
                -- partial index (WHERE ticker_id IS NOT NULL — see
                -- migration 0016) because the unified table also holds
                -- industry rows with NULL ticker_id. For tests, where
                -- only per-ticker rows exist, a non-partial index has
                -- the same observable effect and lets the bare
                -- ON CONFLICT (cols) clause find the constraint.
                CREATE UNIQUE INDEX uq_signal_events_ticker
                    ON signal_events (ticker_id, timeframe, bar_timestamp, signal_type);
                INSERT INTO tickers (ticker_id, exchange, symbol, instrument)
                    VALUES (1806, 'NSE', 'SANSERA', 'EQ');
            """)

        yield engine

        # Teardown: drop the throwaway DB.
        engine.dispose()
        admin = psycopg2.connect(db_url)
        admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        with admin.cursor() as c:
            # Boot anyone still connected (paranoia for test parallelism).
            c.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (test_db,),
            )
            c.execute(f"DROP DATABASE IF EXISTS {test_db}")
        admin.close()

    def test_single_insert_lands_in_table(self, engine):
        event = _event(
            signal=ChangeSignal.BUY,
            bar_timestamp=datetime(2025, 6, 16, 0, 0, tzinfo=timezone.utc),
            computed_at=datetime(2026, 6, 20, 11, 36, tzinfo=timezone.utc),
            price=1327.10,
        )
        with engine.begin() as conn:
            ids = persist_signal_events(conn, ticker_id=1806, events=[event])

        assert len(ids) == 1
        with engine.begin() as conn:
            rows = list(conn.exec_driver_sql(
                "SELECT signal_type, price_at_signal, computed_at "
                "FROM signal_events WHERE ticker_id = 1806"
            ))
        assert len(rows) == 1
        assert rows[0][0] == "BUY"
        assert float(rows[0][1]) == pytest.approx(1327.10)

    def test_duplicate_insert_returns_existing_id_and_no_new_row(self, engine):
        """The headline ON CONFLICT DO NOTHING contract."""
        event = _event(signal=ChangeSignal.BUY)

        with engine.begin() as conn:
            ids_first = persist_signal_events(conn, ticker_id=1806, events=[event])
        with engine.begin() as conn:
            ids_second = persist_signal_events(conn, ticker_id=1806, events=[event])

        assert ids_first == ids_second, (
            "re-inserting the same logical event must return the existing event_id"
        )
        with engine.begin() as conn:
            count = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM signal_events WHERE ticker_id = 1806"
            ).scalar()
        assert count == 1, "ON CONFLICT DO NOTHING must not create a duplicate row"

    def test_distinct_computed_at_with_same_bar_idempotent(self, engine):
        """The intra-week re-evaluation case from the spec: two ticks
        on the same bar with different computed_at MUST collapse to
        one row (the partial unique index doesn't include computed_at)."""
        event_first  = _event(
            signal=ChangeSignal.BUY,
            computed_at=datetime(2026, 6, 20, 11, 36, tzinfo=timezone.utc),
        )
        event_second = _event(
            signal=ChangeSignal.BUY,
            computed_at=datetime(2026, 6, 20, 14, 36, tzinfo=timezone.utc),
        )
        with engine.begin() as conn:
            persist_signal_events(conn, 1806, [event_first])
        with engine.begin() as conn:
            persist_signal_events(conn, 1806, [event_second])

        with engine.begin() as conn:
            rows = list(conn.exec_driver_sql(
                "SELECT computed_at FROM signal_events WHERE ticker_id = 1806"
            ))
        assert len(rows) == 1, (
            "intra-week re-evaluation must be idempotent on the unique index"
        )

    def test_multiple_events_all_persisted_in_order(self, engine):
        events = [
            _event(signal=ChangeSignal.BUY,
                   bar_timestamp=datetime(2025, 6, 16, tzinfo=timezone.utc)),
            _event(signal=ChangeSignal.EXIT_FROM_BUY,
                   bar_timestamp=datetime(2025, 7, 14, tzinfo=timezone.utc)),
            _event(signal=ChangeSignal.SELL,
                   bar_timestamp=datetime(2025, 7, 21, tzinfo=timezone.utc)),
        ]
        with engine.begin() as conn:
            ids = persist_signal_events(conn, ticker_id=1806, events=events)

        assert len(ids) == 3
        assert all(eid > 0 for eid in ids)

        with engine.begin() as conn:
            signals = [r[0] for r in conn.exec_driver_sql(
                "SELECT signal_type FROM signal_events "
                "WHERE ticker_id = 1806 ORDER BY bar_timestamp"
            )]
        assert signals == ["BUY", "EXIT_FROM_BUY", "SELL"]

    def test_computed_at_persisted_verbatim(self, engine):
        """Backfill writes the simulated day; live writes NOW().
        The persist layer must not override either."""
        simulated_day = datetime(2024, 3, 15, 23, 59, 59, tzinfo=timezone.utc)
        event = _event(signal=ChangeSignal.BUY, computed_at=simulated_day)

        with engine.begin() as conn:
            persist_signal_events(conn, ticker_id=1806, events=[event])

        with engine.begin() as conn:
            stored = conn.exec_driver_sql(
                "SELECT computed_at FROM signal_events WHERE ticker_id = 1806"
            ).scalar()
        assert stored == simulated_day

    def test_cache_writer_called_with_real_event_after_insert(self, engine):
        event = _event(signal=ChangeSignal.BUY)
        calls = []

        with engine.begin() as conn:
            persist_signal_events(
                conn, ticker_id=1806, events=[event],
                signal_cache_writer=lambda tid, tf, ev: calls.append((tid, tf, ev)),
            )

        assert len(calls) == 1
        assert calls[0][0] == 1806
        assert calls[0][1] == Timeframe.WEEKLY
        assert calls[0][2].signal == ChangeSignal.BUY
