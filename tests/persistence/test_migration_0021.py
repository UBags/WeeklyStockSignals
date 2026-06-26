"""Tests for migration 0021_indicator_enrichment_tables.

Verifies:
  - Both enrichment tables created with the right columns + defaults
  - Composite PK matches the corresponding state table
  - Composite FK to the state table with ON DELETE CASCADE
  - Cascade fires when a state row is deleted
  - Cascade also fires transitively (delete ticker → state → enrichment)
  - FK violation when inserting enrichment without a parent state row
  - Default values for BOOLEAN (FALSE) and VARCHAR ('NA') pattern_label
  - Idempotency and downgrade-then-upgrade round-trip

Uses the standard `db_engine` fixture from tests/persistence/conftest.py
which runs `alembic upgrade head` on a fresh database, so by the time
each test runs all six V1 tables (4 from 0020 + 2 from 0021) exist.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


# ====================================================================
# Helpers
# ====================================================================

def _columns_of(conn, table_name: str) -> dict[str, dict]:
    """Return {column_name: {data_type, is_nullable, column_default}}."""
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


def _fk_info(conn, table_name: str, constraint_name: str) -> dict:
    """Return FK info: referenced table + columns + delete action.

    Uses pg_constraint directly instead of information_schema.
    information_schema's kcu × ccu join produces a cartesian product
    for composite FKs (N local cols × N referenced cols rows), which
    makes it hard to extract just the local-column list in order.
    pg_constraint stores the local columns as the `conkey` smallint
    array (column attnums), which we unnest + order to recover the
    composite key in definition order.
    """
    rows = conn.execute(text("""
        SELECT
            c.confrelid::regclass::text AS foreign_table,
            c.confdeltype                AS delete_action,
            a.attname                    AS local_col,
            array_position(c.conkey, a.attnum) AS local_position
        FROM pg_constraint c
        JOIN pg_attribute a
          ON a.attrelid = c.conrelid
         AND a.attnum = ANY(c.conkey)
        WHERE c.conname = :c
          AND c.contype = 'f'
        ORDER BY local_position
    """), {"c": constraint_name}).all()
    if not rows:
        return {}
    # confdeltype encoding: 'a'=NO ACTION, 'r'=RESTRICT, 'c'=CASCADE,
    # 'n'=SET NULL, 'd'=SET DEFAULT. Map to information_schema's
    # delete_rule strings so existing test assertions keep working.
    delete_rule_map = {
        "a": "NO ACTION",
        "r": "RESTRICT",
        "c": "CASCADE",
        "n": "SET NULL",
        "d": "SET DEFAULT",
    }
    return {
        "local_cols": [r.local_col for r in rows],
        "foreign_table": rows[0].foreign_table,
        "delete_rule": delete_rule_map.get(rows[0].delete_action, "UNKNOWN"),
    }


def _seed_ticker(conn, ticker_id: int = 1) -> int:
    """Insert one ticker so FKs can be satisfied. Returns the ticker_id.

    Post-0019 tickers schema only requires `exchange` + `symbol` as
    NOT NULL without defaults. Symbol is parameterized so multiple
    seeds in the same test don't collide on (exchange, symbol,
    instrument) UNIQUE.
    """
    conn.execute(text("""
        INSERT INTO tickers (ticker_id, exchange, symbol)
        VALUES (:id, 'NSE', :sym)
        ON CONFLICT (ticker_id) DO NOTHING
    """), {"id": ticker_id, "sym": f"TEST{ticker_id}"})
    return ticker_id


def _seed_stock_state(
    conn, ticker_id: int, *,
    timeframe: str = "1W",
    bar_timestamp: datetime | None = None,
    display_regime: int = 1,
) -> datetime:
    """Insert one stock_indicator_state row so an enrichment row can
    FK to it. Returns the bar_timestamp used (caller passes it to
    the enrichment insert)."""
    if bar_timestamp is None:
        bar_timestamp = datetime(2026, 6, 22, 0, 0, tzinfo=timezone.utc)
    conn.execute(text("""
        INSERT INTO stock_indicator_state (
            ticker_id, timeframe, bar_timestamp,
            med_ema, long_ema, longest_ema, atr,
            upper_inner_band, lower_inner_band,
            upper_mid_band, lower_mid_band,
            upper_outer_band, lower_outer_band,
            display_regime
        ) VALUES (
            :tid, :tf, :bt,
            100, 100, 100, 5,
            105, 95, 110, 90, 120, 80,
            :dr
        )
    """), {
        "tid": ticker_id, "tf": timeframe,
        "bt": bar_timestamp, "dr": display_regime,
    })
    return bar_timestamp


def _seed_industry(conn, industry_name: str = "IT - Services") -> str:
    """Insert one industry_index row so industry_indicator_state can
    reference it. Returns the industry_name."""
    conn.execute(text("""
        INSERT INTO industry_index (industry_name, sector_name, base_date)
        VALUES (:n, 'Technology', '2025-10-01')
        ON CONFLICT (industry_name) DO NOTHING
    """), {"n": industry_name})
    return industry_name


def _seed_industry_state(
    conn, industry_name: str, *,
    timeframe: str = "1W",
    bar_timestamp: datetime | None = None,
) -> datetime:
    """Insert one industry_indicator_state row."""
    if bar_timestamp is None:
        bar_timestamp = datetime(2026, 6, 22, 0, 0, tzinfo=timezone.utc)
    conn.execute(text("""
        INSERT INTO industry_indicator_state (
            industry_name, timeframe, bar_timestamp,
            med_ema, long_ema, longest_ema, atr,
            upper_inner_band, lower_inner_band,
            upper_mid_band, lower_mid_band,
            upper_outer_band, lower_outer_band,
            display_regime
        ) VALUES (
            :n, :tf, :bt,
            1000, 1000, 1000, 50,
            1050, 950, 1100, 900, 1200, 800,
            1
        )
    """), {"n": industry_name, "tf": timeframe, "bt": bar_timestamp})
    return bar_timestamp


# ====================================================================
# Table existence and column shape
# ====================================================================

# The full enrichment column shape, used by both stock and industry
# table assertions. (data_type, is_nullable, has_default).
_ENRICHMENT_COLS = {
    "pierces_upper_mid":              ("boolean", "NO", True),
    "pierces_lower_mid":              ("boolean", "NO", True),
    "dist_to_med_in_hl_units":        ("double precision", "YES", False),
    "dist_to_long_in_hl_units":       ("double precision", "YES", False),
    "dist_to_longest_in_hl_units":    ("double precision", "YES", False),
    "mean_dist_to_med_in_hl_units":   ("double precision", "YES", False),
    "mean_dist_to_long_in_hl_units":  ("double precision", "YES", False),
    "mean_dist_to_longest_in_hl_units": ("double precision", "YES", False),
    "bars_since_med_turn":            ("integer", "YES", False),
    "bars_since_long_turn":           ("integer", "YES", False),
    "bars_since_longest_turn":        ("integer", "YES", False),
    "pct_change_since_med_turn":      ("double precision", "YES", False),
    "pct_change_since_long_turn":     ("double precision", "YES", False),
    "pct_change_since_longest_turn":  ("double precision", "YES", False),
    "med_pattern_label":              ("character varying", "NO", True),
    "long_pattern_label":             ("character varying", "NO", True),
    "longest_pattern_label":          ("character varying", "NO", True),
    "med_turn_buy":                   ("boolean", "NO", True),
    "med_turn_sell":                  ("boolean", "NO", True),
    "long_turn_buy":                  ("boolean", "NO", True),
    "long_turn_sell":                 ("boolean", "NO", True),
    "longest_turn_buy":               ("boolean", "NO", True),
    "longest_turn_sell":              ("boolean", "NO", True),
}


def test_stock_indicator_enrichment_exists_with_expected_columns(db_engine):
    with db_engine.connect() as conn:
        cols = _columns_of(conn, "stock_indicator_enrichment")
    assert cols, "stock_indicator_enrichment table not created"

    # Keys
    assert cols["ticker_id"]["data_type"] == "integer"
    assert cols["ticker_id"]["is_nullable"] == "NO"
    assert cols["timeframe"]["data_type"] == "character varying"
    assert cols["timeframe"]["is_nullable"] == "NO"
    assert cols["bar_timestamp"]["data_type"] == "timestamp with time zone"
    assert cols["bar_timestamp"]["is_nullable"] == "NO"

    # All 23 enrichment columns
    for name, (dtype, nullable, has_default) in _ENRICHMENT_COLS.items():
        assert name in cols, f"missing column {name}"
        assert cols[name]["data_type"] == dtype, (
            f"{name}: expected {dtype}, got {cols[name]['data_type']}"
        )
        assert cols[name]["is_nullable"] == nullable, (
            f"{name}: expected nullable={nullable}, "
            f"got {cols[name]['is_nullable']}"
        )
        if has_default:
            assert cols[name]["column_default"] is not None, (
                f"{name}: expected a default, got NULL"
            )

    # computed_at present with NOW() default
    assert cols["computed_at"]["data_type"] == "timestamp with time zone"
    assert cols["computed_at"]["is_nullable"] == "NO"
    assert cols["computed_at"]["column_default"] is not None

    # No stray columns
    expected_cols = (
        {"ticker_id", "timeframe", "bar_timestamp", "computed_at"}
        | set(_ENRICHMENT_COLS.keys())
    )
    assert set(cols.keys()) == expected_cols, (
        f"unexpected columns: {set(cols.keys()) - expected_cols}, "
        f"missing: {expected_cols - set(cols.keys())}"
    )


def test_industry_indicator_enrichment_exists_with_expected_columns(db_engine):
    with db_engine.connect() as conn:
        cols = _columns_of(conn, "industry_indicator_enrichment")
    assert cols, "industry_indicator_enrichment table not created"

    # Keys — industry_name instead of ticker_id
    assert cols["industry_name"]["data_type"] == "character varying"
    assert cols["industry_name"]["is_nullable"] == "NO"
    assert "ticker_id" not in cols, (
        "industry enrichment table must not have ticker_id"
    )
    assert cols["timeframe"]["data_type"] == "character varying"
    assert cols["bar_timestamp"]["data_type"] == "timestamp with time zone"

    # All 23 enrichment columns same as stock
    for name, (dtype, nullable, _) in _ENRICHMENT_COLS.items():
        assert name in cols, f"missing column {name}"
        assert cols[name]["data_type"] == dtype
        assert cols[name]["is_nullable"] == nullable

    # No stray columns
    expected_cols = (
        {"industry_name", "timeframe", "bar_timestamp", "computed_at"}
        | set(_ENRICHMENT_COLS.keys())
    )
    assert set(cols.keys()) == expected_cols


# ====================================================================
# Primary keys
# ====================================================================

def test_primary_keys(db_engine):
    with db_engine.connect() as conn:
        assert _pk_columns(conn, "stock_indicator_enrichment") == [
            "ticker_id", "timeframe", "bar_timestamp",
        ]
        assert _pk_columns(conn, "industry_indicator_enrichment") == [
            "industry_name", "timeframe", "bar_timestamp",
        ]


# ====================================================================
# Foreign keys back to state tables, with ON DELETE CASCADE
# ====================================================================

def test_stock_enrichment_fk_to_state_with_cascade(db_engine):
    with db_engine.connect() as conn:
        info = _fk_info(
            conn,
            "stock_indicator_enrichment",
            "stock_indicator_enrichment_state_fk",
        )
    assert info, "FK constraint not found"
    assert info["foreign_table"] == "stock_indicator_state"
    assert info["local_cols"] == ["ticker_id", "timeframe", "bar_timestamp"]
    assert info["delete_rule"] == "CASCADE"


def test_industry_enrichment_fk_to_state_with_cascade(db_engine):
    with db_engine.connect() as conn:
        info = _fk_info(
            conn,
            "industry_indicator_enrichment",
            "industry_indicator_enrichment_state_fk",
        )
    assert info, "FK constraint not found"
    assert info["foreign_table"] == "industry_indicator_state"
    assert info["local_cols"] == [
        "industry_name", "timeframe", "bar_timestamp",
    ]
    assert info["delete_rule"] == "CASCADE"


# ====================================================================
# FK behavior — orphan enrichment row is rejected
# ====================================================================

def test_stock_enrichment_requires_parent_state_row(db_engine):
    """Inserting enrichment without a matching state row must fail."""
    with db_engine.begin() as conn:
        tid = _seed_ticker(conn)
        # Note: no _seed_stock_state call here.
        with pytest.raises(IntegrityError, match=r"foreign key"):
            conn.execute(text("""
                INSERT INTO stock_indicator_enrichment (
                    ticker_id, timeframe, bar_timestamp
                ) VALUES (:tid, '1W', NOW())
            """), {"tid": tid})


def test_industry_enrichment_requires_parent_state_row(db_engine):
    """Same for industry."""
    with db_engine.begin() as conn:
        name = _seed_industry(conn)
        # Note: no _seed_industry_state call here.
        with pytest.raises(IntegrityError, match=r"foreign key"):
            conn.execute(text("""
                INSERT INTO industry_indicator_enrichment (
                    industry_name, timeframe, bar_timestamp
                ) VALUES (:n, '1W', NOW())
            """), {"n": name})


# ====================================================================
# CASCADE behavior — deleting state row drops enrichment row
# ====================================================================

def test_stock_state_delete_cascades_to_enrichment(db_engine):
    """Delete state row → enrichment row gone."""
    with db_engine.begin() as conn:
        tid = _seed_ticker(conn, ticker_id=42)
        bt = _seed_stock_state(conn, tid)
        conn.execute(text("""
            INSERT INTO stock_indicator_enrichment (
                ticker_id, timeframe, bar_timestamp,
                pierces_upper_mid, med_pattern_label, med_turn_buy
            ) VALUES (
                :tid, '1W', :bt,
                TRUE, '1 SUCCESSIVE HIGHER VALLEY', TRUE
            )
        """), {"tid": tid, "bt": bt})

        # Sanity: row is there
        n = conn.execute(text("""
            SELECT COUNT(*) FROM stock_indicator_enrichment
            WHERE ticker_id = :tid
        """), {"tid": tid}).scalar_one()
        assert n == 1

        # Delete the state row
        conn.execute(text("""
            DELETE FROM stock_indicator_state WHERE ticker_id = :tid
        """), {"tid": tid})

        # Enrichment should now be gone
        n = conn.execute(text("""
            SELECT COUNT(*) FROM stock_indicator_enrichment
            WHERE ticker_id = :tid
        """), {"tid": tid}).scalar_one()
        assert n == 0


def test_industry_state_delete_cascades_to_enrichment(db_engine):
    """Delete industry state row → industry enrichment row gone."""
    with db_engine.begin() as conn:
        name = _seed_industry(conn)
        bt = _seed_industry_state(conn, name)
        conn.execute(text("""
            INSERT INTO industry_indicator_enrichment (
                industry_name, timeframe, bar_timestamp,
                pierces_upper_mid
            ) VALUES (:n, '1W', :bt, TRUE)
        """), {"n": name, "bt": bt})

        n = conn.execute(text("""
            SELECT COUNT(*) FROM industry_indicator_enrichment
            WHERE industry_name = :n
        """), {"n": name}).scalar_one()
        assert n == 1

        conn.execute(text("""
            DELETE FROM industry_indicator_state WHERE industry_name = :n
        """), {"n": name})

        n = conn.execute(text("""
            SELECT COUNT(*) FROM industry_indicator_enrichment
            WHERE industry_name = :n
        """), {"n": name}).scalar_one()
        assert n == 0


def test_ticker_delete_cascades_transitively_to_enrichment(db_engine):
    """Delete a ticker → state row goes (existing 0020 FK) → enrichment
    row goes too (new 0021 FK). Validates the full cascade chain."""
    with db_engine.begin() as conn:
        tid = _seed_ticker(conn, ticker_id=99)
        bt = _seed_stock_state(conn, tid)
        conn.execute(text("""
            INSERT INTO stock_indicator_enrichment (
                ticker_id, timeframe, bar_timestamp
            ) VALUES (:tid, '1W', :bt)
        """), {"tid": tid, "bt": bt})

        # Delete the ticker — cascades through state to enrichment
        conn.execute(text(
            "DELETE FROM tickers WHERE ticker_id = :tid"
        ), {"tid": tid})

        n_state = conn.execute(text("""
            SELECT COUNT(*) FROM stock_indicator_state
            WHERE ticker_id = :tid
        """), {"tid": tid}).scalar_one()
        n_enrich = conn.execute(text("""
            SELECT COUNT(*) FROM stock_indicator_enrichment
            WHERE ticker_id = :tid
        """), {"tid": tid}).scalar_one()
        assert n_state == 0, "state row should have been cascaded"
        assert n_enrich == 0, "enrichment row should have been cascaded transitively"


# ====================================================================
# Default values fire for unspecified columns
# ====================================================================

def test_stock_enrichment_defaults_apply(db_engine):
    """Inserting with only the PK columns should populate defaults for
    everything else: BOOLEANs → FALSE, pattern_labels → 'NA',
    nullable doubles/ints → NULL."""
    with db_engine.begin() as conn:
        tid = _seed_ticker(conn, ticker_id=7)
        bt = _seed_stock_state(conn, tid)
        conn.execute(text("""
            INSERT INTO stock_indicator_enrichment (
                ticker_id, timeframe, bar_timestamp
            ) VALUES (:tid, '1W', :bt)
        """), {"tid": tid, "bt": bt})

        row = conn.execute(text("""
            SELECT pierces_upper_mid, pierces_lower_mid,
                   med_pattern_label, long_pattern_label, longest_pattern_label,
                   med_turn_buy, med_turn_sell,
                   long_turn_buy, long_turn_sell,
                   longest_turn_buy, longest_turn_sell,
                   dist_to_med_in_hl_units, bars_since_med_turn,
                   pct_change_since_med_turn,
                   computed_at
              FROM stock_indicator_enrichment
             WHERE ticker_id = :tid
        """), {"tid": tid}).one()

        # Boolean defaults
        assert row.pierces_upper_mid is False
        assert row.pierces_lower_mid is False
        assert row.med_turn_buy is False
        assert row.med_turn_sell is False
        assert row.long_turn_buy is False
        assert row.long_turn_sell is False
        assert row.longest_turn_buy is False
        assert row.longest_turn_sell is False

        # Pattern label defaults
        assert row.med_pattern_label == "NA"
        assert row.long_pattern_label == "NA"
        assert row.longest_pattern_label == "NA"

        # Nullable fields are NULL when not provided
        assert row.dist_to_med_in_hl_units is None
        assert row.bars_since_med_turn is None
        assert row.pct_change_since_med_turn is None

        # computed_at defaulted to NOW()
        assert row.computed_at is not None


def test_pattern_label_accepts_any_string(db_engine):
    """No CHECK on pattern_label per design — application-level
    validation only. Any VARCHAR(32) value should be accepted by the DB."""
    with db_engine.begin() as conn:
        tid = _seed_ticker(conn, ticker_id=8)
        bt = _seed_stock_state(conn, tid)
        # Use a value that is NOT in PATTERN_LABELS — DB should accept it.
        conn.execute(text("""
            INSERT INTO stock_indicator_enrichment (
                ticker_id, timeframe, bar_timestamp,
                med_pattern_label
            ) VALUES (:tid, '1W', :bt, 'SOMETHING_FUTURE')
        """), {"tid": tid, "bt": bt})
        v = conn.execute(text("""
            SELECT med_pattern_label FROM stock_indicator_enrichment
             WHERE ticker_id = :tid
        """), {"tid": tid}).scalar_one()
        assert v == "SOMETHING_FUTURE"


# ====================================================================
# PK uniqueness — only one enrichment row per state row
# ====================================================================

def test_stock_enrichment_pk_uniqueness(db_engine):
    """The PK (ticker_id, timeframe, bar_timestamp) enforces 1:1 with state."""
    with db_engine.begin() as conn:
        tid = _seed_ticker(conn, ticker_id=11)
        bt = _seed_stock_state(conn, tid)
        conn.execute(text("""
            INSERT INTO stock_indicator_enrichment (
                ticker_id, timeframe, bar_timestamp
            ) VALUES (:tid, '1W', :bt)
        """), {"tid": tid, "bt": bt})
        with pytest.raises(IntegrityError, match=r"pkey|duplicate"):
            conn.execute(text("""
                INSERT INTO stock_indicator_enrichment (
                    ticker_id, timeframe, bar_timestamp
                ) VALUES (:tid, '1W', :bt)
            """), {"tid": tid, "bt": bt})


# ====================================================================
# Idempotency and downgrade round-trip
# ====================================================================

def test_migration_is_idempotent(db_engine):
    """Re-running 0021 (via IF NOT EXISTS guards) is a no-op."""
    from alembic import command
    from alembic.config import Config as AlembicConfig
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    alembic_cfg = AlembicConfig(str(repo_root / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", str(db_engine.url))
    alembic_cfg.cmd_opts = type("X", (), {"x": ["test_mode=1"]})()
    command.upgrade(alembic_cfg, "head")

    with db_engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT table_name FROM information_schema.tables
             WHERE table_schema = 'public'
               AND table_name IN (
                 'stock_indicator_enrichment',
                 'industry_indicator_enrichment'
               )
        """)).all()
        present = {r.table_name for r in rows}
    assert present == {
        "stock_indicator_enrichment",
        "industry_indicator_enrichment",
    }


def test_downgrade_then_upgrade(db_engine):
    """Downgrade 0021 drops the two enrichment tables; re-upgrade
    recreates them. The 0020 tables (state + signals) should remain
    intact through the downgrade."""
    from alembic import command
    from alembic.config import Config as AlembicConfig
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    alembic_cfg = AlembicConfig(str(repo_root / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", str(db_engine.url))
    alembic_cfg.cmd_opts = type("X", (), {"x": ["test_mode=1"]})()

    # Pin to the down_revision name rather than the relative "-1".
    # "-1" means "one back from current head", which silently moves
    # when later migrations land on top, breaking this test.
    command.downgrade(alembic_cfg, "0020_clean_pipeline_tables")
    with db_engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT table_name FROM information_schema.tables
             WHERE table_schema = 'public'
               AND table_name IN (
                 'stock_indicator_enrichment',
                 'industry_indicator_enrichment'
               )
        """)).all()
        present_after_downgrade = {r.table_name for r in rows}
    assert present_after_downgrade == set(), (
        f"downgrade should drop both enrichment tables; still present: "
        f"{present_after_downgrade}"
    )

    # The 0020 tables should still be there
    with db_engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT table_name FROM information_schema.tables
             WHERE table_schema = 'public'
               AND table_name IN (
                 'stock_indicator_state', 'industry_indicator_state',
                 'current_signals', 'historical_signals'
               )
        """)).all()
        still_present = {r.table_name for r in rows}
    assert still_present == {
        "stock_indicator_state", "industry_indicator_state",
        "current_signals", "historical_signals",
    }, "0020 tables must remain intact through 0021 downgrade"

    command.upgrade(alembic_cfg, "head")
    with db_engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT table_name FROM information_schema.tables
             WHERE table_schema = 'public'
               AND table_name IN (
                 'stock_indicator_enrichment',
                 'industry_indicator_enrichment'
               )
        """)).all()
        present_after_reupgrade = {r.table_name for r in rows}
    assert present_after_reupgrade == {
        "stock_indicator_enrichment",
        "industry_indicator_enrichment",
    }
