"""Tests for migration 0024_v1_industry_idx_value_phase.

Mirrors test_migration_0022.py / test_migration_0023.py shape.
Uses an explicit down_revision target rather than the brittle "-1".
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


ACCEPTED_PHASES_POST_0024 = (
    "industry", "shares", "financials",
    "market_cap", "index_build", "index_value",
    "index_indicators",
    "v1_stock_indicator_state",
    "v1_stock_indicator_enrichment",
    "v1_industry_index_value",
)


def _insert_phase(conn, phase: str, *, exchange: str = "ALL") -> int:
    row = conn.execute(text("""
        INSERT INTO fundamentals_run (phase, exchange, started_at, status)
        VALUES (:phase, :ex, NOW(), 'RUNNING')
        RETURNING run_id
    """), {"phase": phase, "ex": exchange}).fetchone()
    return int(row[0])


# ============================================================
# Accepts new + all prior values
# ============================================================

@pytest.mark.parametrize("phase", ACCEPTED_PHASES_POST_0024)
def test_check_accepts_phase(db_engine, phase):
    with db_engine.begin() as conn:
        run_id = _insert_phase(conn, phase)
        assert run_id > 0


# ============================================================
# Still rejects unknown
# ============================================================

def test_check_still_rejects_unknown_phase(db_engine):
    with db_engine.begin() as conn:
        with pytest.raises(
            IntegrityError, match=r"fundamentals_run_phase_chk",
        ):
            _insert_phase(conn, "totally_made_up_phase")


# ============================================================
# Downgrade restores pre-0024 state and removes new-phase rows
# ============================================================

def test_downgrade_removes_industry_value_phase_and_restores_old_check(
    db_engine,
):
    from alembic import command
    from alembic.config import Config as AlembicConfig
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    alembic_cfg = AlembicConfig(str(repo_root / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", str(db_engine.url))
    alembic_cfg.cmd_opts = type("X", (), {"x": ["test_mode=1"]})()

    # Seed a row with the new phase value
    with db_engine.begin() as conn:
        _insert_phase(conn, "v1_industry_index_value")

    # Pin the downgrade target to the down_revision name.
    command.downgrade(alembic_cfg, "0023_v1_stock_enrich_phase")

    # New-phase rows should be gone
    with db_engine.connect() as conn:
        cnt = conn.execute(text("""
            SELECT COUNT(*) FROM fundamentals_run
             WHERE phase = 'v1_industry_index_value'
        """)).scalar_one()
    assert cnt == 0

    # The new phase should now be rejected
    with db_engine.begin() as conn:
        with pytest.raises(
            IntegrityError, match=r"fundamentals_run_phase_chk",
        ):
            _insert_phase(conn, "v1_industry_index_value")

    # Prior V1 phases still work
    with db_engine.begin() as conn:
        run_id_a = _insert_phase(conn, "v1_stock_indicator_state")
        run_id_b = _insert_phase(conn, "v1_stock_indicator_enrichment")
        assert run_id_a > 0 and run_id_b > 0

    # Restore to head
    command.upgrade(alembic_cfg, "head")


def test_downgrade_then_upgrade_round_trip(db_engine):
    from alembic import command
    from alembic.config import Config as AlembicConfig
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    alembic_cfg = AlembicConfig(str(repo_root / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", str(db_engine.url))
    alembic_cfg.cmd_opts = type("X", (), {"x": ["test_mode=1"]})()

    command.downgrade(alembic_cfg, "0023_v1_stock_enrich_phase")
    command.upgrade(alembic_cfg, "head")

    with db_engine.begin() as conn:
        run_id = _insert_phase(conn, "v1_industry_index_value")
        assert run_id > 0
