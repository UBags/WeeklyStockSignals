"""Tests for migration 0020_clean_pipeline_tables.

Verifies that the four new V1 tables are created with the right
columns, constraints, indexes, and grants — and that downgrade +
upgrade round-trips cleanly.

These tests use the standard `db_engine` fixture from
tests/persistence/conftest.py which runs `alembic upgrade head`
on a fresh database, so by the time each test runs the four
tables already exist.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


# ============================================================
# Table existence and column shape
# ============================================================

def _columns_of(conn, table_name: str) -> dict[str, dict]:
    """Return {column_name: {data_type, is_nullable, column_default}} for a table."""
    rows = conn.execute(text("""
        SELECT column_name, data_type, is_nullable, column_default
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = :t
         ORDER BY ordinal_position
    """), {"t": table_name}).all()
    return {
        r.column_name: {
            "data_type": r.data_type,
            "is_nullable": r.is_nullable,
            "column_default": r.column_default,
        }
        for r in rows
    }


def test_stock_indicator_state_exists_with_expected_columns(db_engine):
    with db_engine.connect() as conn:
        cols = _columns_of(conn, "stock_indicator_state")
    assert cols, "stock_indicator_state table not created"
    expected = {
        "ticker_id": ("integer", "NO"),
        "timeframe": ("character varying", "NO"),
        "bar_timestamp": ("timestamp with time zone", "NO"),
        "med_ema": ("double precision", "NO"),
        "long_ema": ("double precision", "NO"),
        "longest_ema": ("double precision", "NO"),
        "atr": ("double precision", "NO"),
        "upper_inner_band": ("double precision", "NO"),
        "lower_inner_band": ("double precision", "NO"),
        "upper_mid_band": ("double precision", "NO"),
        "lower_mid_band": ("double precision", "NO"),
        "upper_outer_band": ("double precision", "NO"),
        "lower_outer_band": ("double precision", "NO"),
        "display_regime": ("smallint", "NO"),
        "computed_at": ("timestamp with time zone", "NO"),
        "date": ("date", "NO"),
    }
    for name, (dtype, nullable) in expected.items():
        assert name in cols, f"missing column {name}"
        assert cols[name]["data_type"] == dtype, (
            f"{name}: expected {dtype}, got {cols[name]['data_type']}"
        )
        assert cols[name]["is_nullable"] == nullable, (
            f"{name}: expected nullable={nullable}, got {cols[name]['is_nullable']}"
        )
    # No stray columns
    assert set(cols.keys()) == set(expected.keys()), (
        f"unexpected columns: {set(cols.keys()) - set(expected.keys())}"
    )


def test_industry_indicator_state_exists_with_expected_columns(db_engine):
    with db_engine.connect() as conn:
        cols = _columns_of(conn, "industry_indicator_state")
    assert cols, "industry_indicator_state table not created"
    assert "industry_name" in cols
    assert cols["industry_name"]["data_type"] == "character varying"
    assert "ticker_id" not in cols, (
        "industry table must not have ticker_id"
    )
    # Same shape as stock_indicator_state from here on
    for c in ("timeframe", "bar_timestamp", "med_ema", "long_ema", "longest_ema",
              "atr", "upper_inner_band", "lower_inner_band",
              "upper_mid_band", "lower_mid_band",
              "upper_outer_band", "lower_outer_band",
              "display_regime", "computed_at"):
        assert c in cols, f"missing column {c}"


def test_current_signals_exists_with_expected_columns(db_engine):
    with db_engine.connect() as conn:
        cols = _columns_of(conn, "current_signals")
    assert cols, "current_signals table not created"
    expected = {
        "ticker_id", "timeframe", "bar_timestamp", "signal_type",
        "regime_before", "regime_after", "price_at_signal", "computed_at",
    }
    assert set(cols.keys()) == expected


def test_historical_signals_exists_with_expected_columns(db_engine):
    with db_engine.connect() as conn:
        cols = _columns_of(conn, "historical_signals")
    assert cols, "historical_signals table not created"
    expected = {
        "signal_id", "ticker_id", "timeframe", "bar_timestamp",
        "signal_type", "regime_before", "regime_after",
        "price_at_signal", "computed_at", "archived_at",
    }
    assert set(cols.keys()) == expected
    # signal_id should be BIGSERIAL backed by a sequence
    assert cols["signal_id"]["data_type"] == "bigint"
    assert cols["signal_id"]["column_default"] is not None
    assert "nextval" in cols["signal_id"]["column_default"]


# ============================================================
# Primary keys
# ============================================================

def _pk_columns(conn, table_name: str) -> list[str]:
    rows = conn.execute(text("""
        SELECT a.attname
          FROM pg_index i
          JOIN pg_class c ON c.oid = i.indrelid
          JOIN pg_attribute a ON a.attrelid = i.indrelid
                              AND a.attnum = ANY(i.indkey)
         WHERE c.relname = :t AND i.indisprimary
         ORDER BY array_position(i.indkey, a.attnum)
    """), {"t": table_name}).all()
    return [r.attname for r in rows]


def test_primary_keys(db_engine):
    with db_engine.connect() as conn:
        assert _pk_columns(conn, "stock_indicator_state") == [
            "ticker_id", "timeframe", "date",
        ]
        assert _pk_columns(conn, "industry_indicator_state") == [
            "industry_name", "timeframe", "bar_timestamp",
        ]
        assert _pk_columns(conn, "current_signals") == ["ticker_id"]
        assert _pk_columns(conn, "historical_signals") == ["signal_id"]


# ============================================================
# Indexes
# ============================================================

def _index_names(conn, table_name: str) -> set[str]:
    rows = conn.execute(text("""
        SELECT indexname FROM pg_indexes
         WHERE schemaname = 'public' AND tablename = :t
    """), {"t": table_name}).all()
    return {r.indexname for r in rows}


def test_indexes(db_engine):
    with db_engine.connect() as conn:
        sis_idx = _index_names(conn, "stock_indicator_state")
        assert "idx_stock_indicator_state_bar_ts" in sis_idx
        assert "idx_stock_indicator_state_ticker_bar" in sis_idx

        iis_idx = _index_names(conn, "industry_indicator_state")
        assert "idx_industry_indicator_state_bar_ts" in iis_idx
        assert "idx_industry_indicator_state_industry_bar" in iis_idx

        hs_idx = _index_names(conn, "historical_signals")
        assert "idx_historical_signals_ticker_bar" in hs_idx
        assert "idx_historical_signals_bar_ts" in hs_idx


# ============================================================
# CHECK constraints — fire as expected
# ============================================================

def _seed_ticker(conn, ticker_id: int = 1) -> int:
    """Insert one ticker so FKs can be satisfied. Returns the ticker_id.

    The tickers table has NOT NULL columns without defaults: only
    exchange and symbol. Everything else has a default. We supply a
    unique symbol per ticker_id so the (exchange, symbol, instrument)
    UNIQUE never collides across multiple seeds in the same test.
    """
    conn.execute(text("""
        INSERT INTO tickers (ticker_id, exchange, symbol)
        VALUES (:id, 'NSE', :sym)
        ON CONFLICT (ticker_id) DO NOTHING
    """), {"id": ticker_id, "sym": f"TEST{ticker_id}"})
    return ticker_id


def test_stock_indicator_state_regime_check(db_engine):
    """display_regime must be in (-1, 0, 1)."""
    with db_engine.begin() as conn:
        tid = _seed_ticker(conn)
        with pytest.raises(IntegrityError, match=r"display_regime"):
            conn.execute(text("""
                INSERT INTO stock_indicator_state (
                    ticker_id, timeframe, bar_timestamp,
                    med_ema, long_ema, longest_ema, atr,
                    upper_inner_band, lower_inner_band,
                    upper_mid_band, lower_mid_band,
                    upper_outer_band, lower_outer_band,
                    display_regime, date
                ) VALUES (
                    :tid, '1W', NOW(),
                    100, 100, 100, 5,
                    105, 95, 110, 90, 120, 80,
                    9, CURRENT_DATE
                )
            """), {"tid": tid})


def test_stock_indicator_state_atr_nonnegative(db_engine):
    with db_engine.begin() as conn:
        tid = _seed_ticker(conn)
        with pytest.raises(IntegrityError, match=r"atr"):
            conn.execute(text("""
                INSERT INTO stock_indicator_state (
                    ticker_id, timeframe, bar_timestamp,
                    med_ema, long_ema, longest_ema, atr,
                    upper_inner_band, lower_inner_band,
                    upper_mid_band, lower_mid_band,
                    upper_outer_band, lower_outer_band,
                    display_regime, date
                ) VALUES (
                    :tid, '1W', NOW(),
                    100, 100, 100, -1,
                    105, 95, 110, 90, 120, 80,
                    1, CURRENT_DATE
                )
            """), {"tid": tid})


def test_stock_indicator_state_emas_positive(db_engine):
    with db_engine.begin() as conn:
        tid = _seed_ticker(conn)
        with pytest.raises(IntegrityError, match=r"emas_positive"):
            conn.execute(text("""
                INSERT INTO stock_indicator_state (
                    ticker_id, timeframe, bar_timestamp,
                    med_ema, long_ema, longest_ema, atr,
                    upper_inner_band, lower_inner_band,
                    upper_mid_band, lower_mid_band,
                    upper_outer_band, lower_outer_band,
                    display_regime, date
                ) VALUES (
                    :tid, '1W', NOW(),
                    0, 100, 100, 5,
                    105, 95, 110, 90, 120, 80,
                    1, CURRENT_DATE
                )
            """), {"tid": tid})


def test_current_signals_signal_type_check(db_engine):
    with db_engine.begin() as conn:
        tid = _seed_ticker(conn)
        with pytest.raises(IntegrityError, match=r"signal_type"):
            conn.execute(text("""
                INSERT INTO current_signals (
                    ticker_id, timeframe, bar_timestamp,
                    signal_type, regime_before, regime_after,
                    price_at_signal
                ) VALUES (
                    :tid, '1W', NOW(),
                    'INVALID_KIND', 0, 1, 100
                )
            """), {"tid": tid})


def test_current_signals_regime_must_change(db_engine):
    """regime_before == regime_after is invalid by definition."""
    with db_engine.begin() as conn:
        tid = _seed_ticker(conn)
        with pytest.raises(IntegrityError, match=r"regime_changes"):
            conn.execute(text("""
                INSERT INTO current_signals (
                    ticker_id, timeframe, bar_timestamp,
                    signal_type, regime_before, regime_after,
                    price_at_signal
                ) VALUES (
                    :tid, '1W', NOW(),
                    'BUY', 1, 1, 100
                )
            """), {"tid": tid})


def test_current_signals_price_positive(db_engine):
    with db_engine.begin() as conn:
        tid = _seed_ticker(conn)
        with pytest.raises(IntegrityError, match=r"price"):
            conn.execute(text("""
                INSERT INTO current_signals (
                    ticker_id, timeframe, bar_timestamp,
                    signal_type, regime_before, regime_after,
                    price_at_signal
                ) VALUES (
                    :tid, '1W', NOW(),
                    'BUY', 0, 1, 0
                )
            """), {"tid": tid})


def test_current_signals_ticker_id_is_unique(db_engine):
    """ticker_id is the PRIMARY KEY — only one row per ticker."""
    with db_engine.begin() as conn:
        tid = _seed_ticker(conn)
        conn.execute(text("""
            INSERT INTO current_signals (
                ticker_id, timeframe, bar_timestamp,
                signal_type, regime_before, regime_after, price_at_signal
            ) VALUES (
                :tid, '1W', NOW(),
                'BUY', 0, 1, 100
            )
        """), {"tid": tid})
        with pytest.raises(IntegrityError, match=r"current_signals_pkey"):
            conn.execute(text("""
                INSERT INTO current_signals (
                    ticker_id, timeframe, bar_timestamp,
                    signal_type, regime_before, regime_after, price_at_signal
                ) VALUES (
                    :tid, '1W', NOW(),
                    'SELL', 1, -1, 101
                )
            """), {"tid": tid})


# ============================================================
# Historical signals: duplicates ARE allowed
# ============================================================

def test_historical_signals_allows_duplicates(db_engine):
    """Same (ticker_id, bar_timestamp, signal_type) can repeat —
    by design, no uniqueness constraint beyond signal_id."""
    with db_engine.begin() as conn:
        tid = _seed_ticker(conn)
        bar_ts = datetime(2026, 6, 22, 0, 0, tzinfo=timezone.utc)
        for _ in range(3):
            conn.execute(text("""
                INSERT INTO historical_signals (
                    ticker_id, timeframe, bar_timestamp,
                    signal_type, regime_before, regime_after,
                    price_at_signal, computed_at
                ) VALUES (
                    :tid, '1W', :bt,
                    'BUY', 0, 1, 100, NOW()
                )
            """), {"tid": tid, "bt": bar_ts})
        count = conn.execute(text(
            "SELECT COUNT(*) FROM historical_signals WHERE ticker_id = :tid"
        ), {"tid": tid}).scalar_one()
        assert count == 3


# ============================================================
# Foreign keys: ticker cascade
# ============================================================

def test_stock_indicator_state_ticker_cascade(db_engine):
    """Deleting a ticker cascades to stock_indicator_state."""
    with db_engine.begin() as conn:
        tid = _seed_ticker(conn, ticker_id=42)
        conn.execute(text("""
            INSERT INTO stock_indicator_state (
                ticker_id, timeframe, bar_timestamp,
                med_ema, long_ema, longest_ema, atr,
                upper_inner_band, lower_inner_band,
                upper_mid_band, lower_mid_band,
                upper_outer_band, lower_outer_band,
                display_regime, date
            ) VALUES (
                :tid, '1W', NOW(),
                100, 100, 100, 5,
                105, 95, 110, 90, 120, 80,
                1, CURRENT_DATE
            )
        """), {"tid": tid})
        conn.execute(text("DELETE FROM tickers WHERE ticker_id = :tid"), {"tid": tid})
        count = conn.execute(text("""
            SELECT COUNT(*) FROM stock_indicator_state WHERE ticker_id = :tid
        """), {"tid": tid}).scalar_one()
        assert count == 0


# ============================================================
# Idempotency and downgrade round-trip
# ============================================================

def test_migration_is_idempotent(db_engine):
    """Re-running 0020's CREATE TABLE statements (via the IF NOT EXISTS
    guards) is a no-op."""
    from alembic import command
    from alembic.config import Config as AlembicConfig
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    alembic_cfg = AlembicConfig(str(repo_root / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", str(db_engine.url))
    alembic_cfg.cmd_opts = type("X", (), {"x": ["test_mode=1"]})()
    # Already at head from the fixture — re-running upgrade is a no-op.
    command.upgrade(alembic_cfg, "head")

    # Verify the four tables still exist
    with db_engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT table_name FROM information_schema.tables
             WHERE table_schema = 'public'
               AND table_name IN (
                 'stock_indicator_state', 'industry_indicator_state',
                 'current_signals', 'historical_signals'
               )
        """)).all()
        present = {r.table_name for r in rows}
    assert present == {
        "stock_indicator_state", "industry_indicator_state",
        "current_signals", "historical_signals",
    }


def test_downgrade_then_upgrade(db_engine):
    """Downgrade 0020 drops the four tables; re-upgrade recreates them."""
    from alembic import command
    from alembic.config import Config as AlembicConfig
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    alembic_cfg = AlembicConfig(str(repo_root / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", str(db_engine.url))
    alembic_cfg.cmd_opts = type("X", (), {"x": ["test_mode=1"]})()

    # Downgrade one revision
    # Pin to the down_revision name rather than the relative "-1".
    # "-1" means "one back from current head", which silently moves
    # when later migrations land on top, breaking this test.
    command.downgrade(alembic_cfg, "0019_drop_signal_industry")
    with db_engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT table_name FROM information_schema.tables
             WHERE table_schema = 'public'
               AND table_name IN (
                 'stock_indicator_state', 'industry_indicator_state',
                 'current_signals', 'historical_signals'
               )
        """)).all()
        present_after_downgrade = {r.table_name for r in rows}
    assert present_after_downgrade == set(), (
        f"downgrade should drop all 4 tables; still present: {present_after_downgrade}"
    )

    # Upgrade back to head
    command.upgrade(alembic_cfg, "head")
    with db_engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT table_name FROM information_schema.tables
             WHERE table_schema = 'public'
               AND table_name IN (
                 'stock_indicator_state', 'industry_indicator_state',
                 'current_signals', 'historical_signals'
               )
        """)).all()
        present_after_reupgrade = {r.table_name for r in rows}
    assert present_after_reupgrade == {
        "stock_indicator_state", "industry_indicator_state",
        "current_signals", "historical_signals",
    }
