"""close_period must never write effective_to < effective_from.

A constituent that leaves an index before it joins is not a schema curiosity:
industry_index_value is computed from membership over an effective period, so
an inverted period silently mis-weights every value derived from it.
iic_range_chk stopped exactly that on 2026-07-19, when a rebalance chain was
rebuilt over rows a previous run had left open and the 2024 rebalance tried to
close a period that began in 2026.

The DB constraint is the last line of defence. These tests pin the two lines
in front of it: the UPDATE is scoped so the inverting write is never attempted,
and the case is reported as a named error rather than an opaque CheckViolation
with no indication of which rebalance was running.

Seeded through the repository rather than raw INSERTs -- industry_index_constituent
carries five CHECK constraints and two FKs, and hand-rolled fixtures drift from
them.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text

from trip_ema.persistence import transaction
from trip_ema.persistence.fundamentals_repo import (
    IndustryIndexConstituentRepository,
    IndustryIndexRepository,
)

IND = "TestCloseIndustry"
TID = 990001


@pytest.fixture
def seeded(db_engine):
    """One index, one ticker, no constituent rows yet."""
    with transaction(db_engine) as conn:
        conn.execute(text(
            "INSERT INTO tickers (ticker_id, exchange, symbol, tier, "
            "is_active, delisted_at) "
            "VALUES (:t, 'NSE', 'CLOSEPRD', 2, TRUE, NULL) "
            "ON CONFLICT (ticker_id) DO NOTHING"
        ), {"t": TID})
        IndustryIndexRepository(conn).upsert(
            IND, sector_name="TestSector", base_date=date(2020, 4, 1),
        )
    yield db_engine
    with transaction(db_engine) as conn:
        conn.execute(text(
            "DELETE FROM industry_index_constituent WHERE industry_name = :n"
        ), {"n": IND})
        conn.execute(text(
            "DELETE FROM industry_index WHERE industry_name = :n"
        ), {"n": IND})


def _open_period(conn, rebalance_date):
    IndustryIndexConstituentRepository(conn).insert_constituent(
        IND, ticker_id=TID, rebalance_date=rebalance_date,
        weight_at_rebalance=0.5,
        market_cap_at_rebalance=1_000_000.0,
        close_at_rebalance=100.0,
    )


def test_a_normal_close_succeeds(seeded):
    with transaction(seeded) as conn:
        _open_period(conn, date(2024, 4, 1))
        assert IndustryIndexConstituentRepository(conn).close_period(
            IND, TID, date(2025, 3, 31), "REBALANCE_EXIT",
        ) is True


def test_closing_on_the_open_date_itself_is_allowed(seeded):
    """iic_range_chk is effective_to >= effective_from, so a same-day close is
    a legal zero-length period and must not be rejected."""
    with transaction(seeded) as conn:
        _open_period(conn, date(2024, 4, 1))
        assert IndustryIndexConstituentRepository(conn).close_period(
            IND, TID, date(2024, 4, 1), "REBALANCE_EXIT",
        ) is True


def test_no_open_period_returns_false(seeded):
    with transaction(seeded) as conn:
        assert IndustryIndexConstituentRepository(conn).close_period(
            IND, TID, date(2025, 3, 31), "REBALANCE_EXIT",
        ) is False


def test_closing_backwards_raises_instead_of_violating_the_constraint(seeded):
    """The 2026-07-19 failure, reproduced: an open period from 2026 and a
    close request dated 2024. Must raise a ValueError naming both dates, not
    a psycopg2 CheckViolation."""
    with transaction(seeded) as conn:
        _open_period(conn, date(2026, 4, 1))
        with pytest.raises(ValueError) as exc:
            IndustryIndexConstituentRepository(conn).close_period(
                IND, TID, date(2024, 3, 31), "REBALANCE_EXIT",
            )
    msg = str(exc.value)
    assert "2026-04-01" in msg and "2024-03-31" in msg
    assert "rebuilt" in msg


def test_a_backwards_close_leaves_the_row_untouched(seeded):
    """It must not half-apply: the period stays open and unmarked so the
    rebuild can be re-run cleanly after clearing."""
    with transaction(seeded) as conn:
        _open_period(conn, date(2026, 4, 1))
    with transaction(seeded) as conn:
        with pytest.raises(ValueError):
            IndustryIndexConstituentRepository(conn).close_period(
                IND, TID, date(2024, 3, 31), "REBALANCE_EXIT",
            )
    with seeded.connect() as conn:
        row = conn.execute(text(
            "SELECT effective_to, removed_reason "
            "FROM industry_index_constituent "
            "WHERE industry_name = :n AND ticker_id = :t"
        ), {"n": IND, "t": TID}).one()
    assert row[0] is None
    assert row[1] is None



def test_an_earlier_open_period_is_still_closable(seeded):
    """The guard is `effective_from <= effective_to`, not an equality — a
    period opened years before must still close normally."""
    with transaction(seeded) as conn:
        _open_period(conn, date(2020, 4, 1))
        assert IndustryIndexConstituentRepository(conn).close_period(
            IND, TID, date(2026, 3, 31), "REBALANCE_EXIT",
        ) is True
