"""Tests for migration 0022_v1_stock_state_phase.

Verifies:
  - The CHECK constraint allows the new 'v1_stock_indicator_state' value
  - The CHECK still allows all the pre-existing phase values
  - Unknown phase values are still rejected (so the CHECK isn't gone)
  - Downgrade restores the prior CHECK and removes any v1 rows
  - Downgrade-then-upgrade round-trip is clean
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


# All phase values that should be accepted POST-0022.
ACCEPTED_PHASES_POST_0022 = (
    "industry", "shares", "financials",
    "market_cap", "index_build", "index_value",
    "index_indicators",
    "v1_stock_indicator_state",
)


def _insert_phase(conn, phase: str, *, exchange: str = "NSE") -> int:
    """Insert one fundamentals_run row with the given phase. Returns
    its run_id. Raises IntegrityError if the CHECK constraint rejects
    the value."""
    row = conn.execute(text("""
        INSERT INTO fundamentals_run (phase, exchange, started_at, status)
        VALUES (:phase, :ex, NOW(), 'RUNNING')
        RETURNING run_id
    """), {"phase": phase, "ex": exchange}).fetchone()
    return int(row[0])


# ====================================================================
# CHECK accepts the new value + all the old ones
# ====================================================================

@pytest.mark.parametrize("phase", ACCEPTED_PHASES_POST_0022)
def test_check_accepts_phase(db_engine, phase):
    with db_engine.begin() as conn:
        run_id = _insert_phase(conn, phase)
        assert run_id > 0


# ====================================================================
# CHECK still rejects unknown values
# ====================================================================

def test_check_still_rejects_unknown_phase(db_engine):
    with db_engine.begin() as conn:
        with pytest.raises(IntegrityError, match=r"fundamentals_run_phase_chk"):
            _insert_phase(conn, "totally_made_up_phase")


def test_check_rejects_empty_string(db_engine):
    with db_engine.begin() as conn:
        with pytest.raises(IntegrityError, match=r"fundamentals_run_phase_chk"):
            _insert_phase(conn, "")


# ====================================================================
# Downgrade behavior
# ====================================================================

def test_downgrade_removes_v1_phase_and_restores_old_check(db_engine):
    """Downgrade 0022 deletes v1_stock_indicator_state rows AND
    restores the pre-0022 CHECK. After downgrade, attempting to
    insert with the new phase should fail; the old phases still work."""
    from alembic import command
    from alembic.config import Config as AlembicConfig
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    alembic_cfg = AlembicConfig(str(repo_root / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", str(db_engine.url))
    alembic_cfg.cmd_opts = type("X", (), {"x": ["test_mode=1"]})()

    # Seed a v1-phase row so we can verify it's deleted on downgrade.
    with db_engine.begin() as conn:
        _insert_phase(conn, "v1_stock_indicator_state")

    with db_engine.connect() as conn:
        cnt_v1 = conn.execute(text("""
            SELECT COUNT(*) FROM fundamentals_run
             WHERE phase = 'v1_stock_indicator_state'
        """)).scalar_one()
    assert cnt_v1 >= 1

    # Downgrade.
    command.downgrade(alembic_cfg, "-1")

    # The v1 rows should be gone.
    with db_engine.connect() as conn:
        cnt_v1_after = conn.execute(text("""
            SELECT COUNT(*) FROM fundamentals_run
             WHERE phase = 'v1_stock_indicator_state'
        """)).scalar_one()
    assert cnt_v1_after == 0, (
        "Downgrade should delete v1_stock_indicator_state rows"
    )

    # New phase value should now be rejected.
    with db_engine.begin() as conn:
        with pytest.raises(
            IntegrityError, match=r"fundamentals_run_phase_chk",
        ):
            _insert_phase(conn, "v1_stock_indicator_state")

    # Old phases still accepted post-downgrade.
    with db_engine.begin() as conn:
        run_id = _insert_phase(conn, "industry")
        assert run_id > 0

    # Bring back to head for subsequent tests in the session.
    command.upgrade(alembic_cfg, "head")


def test_downgrade_then_upgrade_round_trip(db_engine):
    """After downgrade+upgrade, the v1 phase is accepted again."""
    from alembic import command
    from alembic.config import Config as AlembicConfig
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    alembic_cfg = AlembicConfig(str(repo_root / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", str(db_engine.url))
    alembic_cfg.cmd_opts = type("X", (), {"x": ["test_mode=1"]})()

    command.downgrade(alembic_cfg, "-1")
    command.upgrade(alembic_cfg, "head")

    with db_engine.begin() as conn:
        run_id = _insert_phase(conn, "v1_stock_indicator_state")
        assert run_id > 0
