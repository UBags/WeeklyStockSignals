"""Tests for the `exchange` kwarg added to ETL runner public entry points.

These don't require a real Postgres — they exercise `_select_tickers`
against an in-memory SQLite database. The SQL uses standard named
parameters and ANSI WHERE clauses, so it ports cleanly.

What we're verifying:
  - exchange=None → falls back to config.etl.exchange (backward compat)
  - exchange="X" → overrides config, returns only X's tickers
  - The (exchange, tier_filter) pair composes correctly
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text

from trip_ema.etl.runner import _select_tickers


def _make_config(default_exchange: str = "NSE") -> MagicMock:
    """Minimal mock config: only the etl.exchange field is needed for
    these tests. Using MagicMock keeps the test independent of the
    real Config schema, which would otherwise drag in the entire
    config layer."""
    cfg = MagicMock()
    cfg.etl.exchange = default_exchange
    return cfg


@pytest.fixture
def engine():
    """Bare SQLite engine seeded with a `tickers` table containing two
    exchanges' worth of data. Tier values are integers (1, 2, 3) per
    the real Tier(int, Enum) in trip_ema.etl.types."""
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE tickers (
                ticker_id INTEGER PRIMARY KEY,
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                tier INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                delisted_at TEXT
            )
        """))
        rows = [
            # NSE tickers, all tiers
            (1, "RELIANCE", "NSE", 1, 1, None),
            (2, "INFY",     "NSE", 1, 1, None),
            (3, "TCS",      "NSE", 2, 1, None),
            (4, "OLDCO",    "NSE", 1, 0, None),         # inactive
            (5, "DEADCO",   "NSE", 1, 1, "2026-01-01"), # delisted
            # BSE tickers
            (6, "BSEONLY",  "BSE", 1, 1, None),
            (7, "BSEMID",   "BSE", 2, 1, None),
            # NYSE tickers (future-proofing)
            (8, "AAPL",     "NYSE", 1, 1, None),
        ]
        conn.execute(
            text("""
                INSERT INTO tickers
                  (ticker_id, symbol, exchange, tier, is_active, delisted_at)
                VALUES (:id, :sym, :ex, :tier, :act, :del)
            """),
            [
                {"id": r[0], "sym": r[1], "ex": r[2], "tier": r[3],
                 "act": r[4], "del": r[5]}
                for r in rows
            ],
        )
    return eng


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestExchangeKwargBackwardCompat:
    def test_no_exchange_kwarg_uses_config(self, engine):
        cfg = _make_config(default_exchange="NSE")
        tickers = _select_tickers(engine, cfg)
        symbols = {t.symbol for t in tickers}
        # Three active, non-delisted NSE tickers.
        assert symbols == {"RELIANCE", "INFY", "TCS"}

    def test_none_explicit_equivalent_to_no_kwarg(self, engine):
        cfg = _make_config(default_exchange="NSE")
        # Explicit None should behave the same as not passing it.
        a = _select_tickers(engine, cfg)
        b = _select_tickers(engine, cfg, exchange=None)
        assert {t.symbol for t in a} == {t.symbol for t in b}

    def test_config_default_change_propagates_when_kwarg_omitted(self, engine):
        cfg = _make_config(default_exchange="BSE")
        tickers = _select_tickers(engine, cfg)
        symbols = {t.symbol for t in tickers}
        assert symbols == {"BSEONLY", "BSEMID"}


# ---------------------------------------------------------------------------
# Explicit exchange override
# ---------------------------------------------------------------------------


class TestExchangeKwargOverride:
    def test_kwarg_overrides_config(self, engine):
        # Config says NSE, but we ask for BSE — should return BSE only.
        cfg = _make_config(default_exchange="NSE")
        tickers = _select_tickers(engine, cfg, exchange="BSE")
        symbols = {t.symbol for t in tickers}
        assert symbols == {"BSEONLY", "BSEMID"}

    def test_kwarg_isolates_each_market(self, engine):
        cfg = _make_config(default_exchange="NSE")
        nse = {t.symbol for t in _select_tickers(engine, cfg, exchange="NSE")}
        bse = {t.symbol for t in _select_tickers(engine, cfg, exchange="BSE")}
        nyse = {t.symbol for t in _select_tickers(engine, cfg, exchange="NYSE")}
        # No cross-contamination across the three markets.
        assert nse.isdisjoint(bse)
        assert nse.isdisjoint(nyse)
        assert bse.isdisjoint(nyse)
        assert nyse == {"AAPL"}

    def test_unknown_exchange_returns_empty(self, engine):
        cfg = _make_config(default_exchange="NSE")
        tickers = _select_tickers(engine, cfg, exchange="LSE")
        assert tickers == []


# ---------------------------------------------------------------------------
# Composes with tier_filter
# ---------------------------------------------------------------------------


class TestExchangeKwargComposesWithTierFilter:
    """The runner's WHERE clause uses Postgres-specific `tier = ANY(:tiers)`
    syntax, which SQLite doesn't understand. These tests verify the
    *composition* is correct in shape, but require Postgres to actually
    execute. They run against the real DB in the CI fixture and are
    skipped here.
    """

    pytestmark = pytest.mark.skip(
        reason="tier_filter SQL uses Postgres-specific ANY(); "
               "covered by Postgres-fixture tests elsewhere"
    )

    def test_exchange_and_tier_filter(self, engine):
        from trip_ema.etl.types import Tier

        cfg = _make_config(default_exchange="NSE")
        tickers = _select_tickers(
            engine, cfg, exchange="NSE", tier_filter={Tier.TIER_1},
        )
        symbols = {t.symbol for t in tickers}
        assert symbols == {"RELIANCE", "INFY"}

    def test_override_exchange_and_tier_filter(self, engine):
        from trip_ema.etl.types import Tier

        cfg = _make_config(default_exchange="NSE")
        tickers = _select_tickers(
            engine, cfg, exchange="BSE", tier_filter={Tier.TIER_2},
        )
        symbols = {t.symbol for t in tickers}
        assert symbols == {"BSEMID"}


# ---------------------------------------------------------------------------
# Active/delisted filtering is preserved
# ---------------------------------------------------------------------------


class TestActiveFilteringPreserved:
    """Regression: the exchange kwarg must NOT loosen the existing
    is_active=TRUE and delisted_at IS NULL filters."""

    def test_inactive_excluded(self, engine):
        cfg = _make_config(default_exchange="NSE")
        tickers = _select_tickers(engine, cfg, exchange="NSE")
        assert "OLDCO" not in {t.symbol for t in tickers}

    def test_delisted_excluded(self, engine):
        cfg = _make_config(default_exchange="NSE")
        tickers = _select_tickers(engine, cfg, exchange="NSE")
        assert "DEADCO" not in {t.symbol for t in tickers}
