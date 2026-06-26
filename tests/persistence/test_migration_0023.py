"""Tests for migration 0023_v1_stock_enrich_phase.

Mirrors the test_migration_0022.py shape. Uses an explicit
down_revision target rather than the brittle "-1" relative offset.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


ACCEPTED_PHASES_POST_0023 = (
    "industry", "shares", "financials",
    "market_cap", "index_build", "index_value",
    "index_indicators",
    "v1_stock_indicator_state",
    "v1_stock_indicator_enrichment",
)


def _insert_phase(conn, phase: str, *, exchange: str = "NSE") -> int:
    row = conn.execute(text("""
        INSERT INTO fundamentals_run (phase, exchange, started_at, status)
        VALUES (:phase, :ex, NOW(), 'RUNNING')
        RETURNING run_id
    """), {"phase": phase, "ex": exchange}).fetchone()
    return int(row[0])


# ============================================================
# Accepts new + all prior values
# ============================================================

@pytest.mark.parametrize("phase", ACCEPTED_PHASES_POST_0023)
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
# Downgrade restores pre-0023 state and removes v1_enrichment rows
# ============================================================

def test_downgrade_removes_enrichment_phase_and_restores_old_check(
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
        _insert_phase(conn, "v1_stock_indicator_enrichment")

    # Pin the downgrade target to the down_revision name
    # (not "-1" — that breaks when later migrations land).
    command.downgrade(alembic_cfg, "0022_v1_stock_state_phase")

    # Enrichment rows should be gone
    with db_engine.connect() as conn:
        cnt = conn.execute(text("""
            SELECT COUNT(*) FROM fundamentals_run
             WHERE phase = 'v1_stock_indicator_enrichment'
        """)).scalar_one()
    assert cnt == 0

    # The new phase should now be rejected
    with db_engine.begin() as conn:
        with pytest.raises(
            IntegrityError, match=r"fundamentals_run_phase_chk",
        ):
            _insert_phase(conn, "v1_stock_indicator_enrichment")

    # But the prior V1 phase (v1_stock_indicator_state) still works
    with db_engine.begin() as conn:
        run_id = _insert_phase(conn, "v1_stock_indicator_state")
        assert run_id > 0

    # Restore to head for any subsequent tests in the session
    command.upgrade(alembic_cfg, "head")


def test_downgrade_then_upgrade_round_trip(db_engine):
    from alembic import command
    from alembic.config import Config as AlembicConfig
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    alembic_cfg = AlembicConfig(str(repo_root / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", str(db_engine.url))
    alembic_cfg.cmd_opts = type("X", (), {"x": ["test_mode=1"]})()

    command.downgrade(alembic_cfg, "0022_v1_stock_state_phase")
    command.upgrade(alembic_cfg, "head")

    with db_engine.begin() as conn:
        run_id = _insert_phase(conn, "v1_stock_indicator_enrichment")
        assert run_id > 0
