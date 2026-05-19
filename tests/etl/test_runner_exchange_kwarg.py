"""Tests for the `exchange` kwarg on the ETL runner internals.

Phase 14.6a: `exchange` is now a REQUIRED keyword argument on every
public runner entry point. The internal `_select_tickers` helper
accordingly takes a required `exchange: str` and no longer falls
back to `config.etl.exchange`. These tests cover the per-exchange
isolation guarantee — each market's fetch pass only sees its own
tickers.

These don't require a real Postgres — they exercise `_select_tickers`
against an in-memory SQLite database. The SQL uses standard named
parameters and ANSI WHERE clauses, so it ports cleanly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text

from trip_ema.etl.runner import _select_tickers


def _make_config(default_exchange: str = "NSE") -> MagicMock:
    """Minimal mock config: only the etl.exchange field is needed for
    these tests (a few legacy assertions reference it for backward-
    compat shape, but the runner no longer reads it). Using MagicMock
    keeps the test independent of the real Config schema, which would
    otherwise drag in the entire config layer."""
    cfg = MagicMock()
    cfg.etl.exchange = default_exchange
    return cfg


@pytest.fixture
def engine():
    """Bare SQLite engine seeded with a `tickers` table containing
    three exchanges' worth of data. Tier values are integers (1, 2, 3)
    per the real Tier(int, Enum) in trip_ema.etl.types."""
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
# Required exchange — the kwarg is no longer optional
# ---------------------------------------------------------------------------


class TestExchangeKwargIsRequired:
    """In Phase 14.6a the runner no longer derives exchange from
    config. Callers MUST pass it explicitly. Missing it is a
    TypeError."""

    def test_missing_kwarg_raises_typeerror(self, engine):
        cfg = _make_config(default_exchange="NSE")
        with pytest.raises(TypeError):
            _select_tickers(engine, cfg)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Per-exchange isolation
# ---------------------------------------------------------------------------


class TestExchangeKwargIsolatesMarkets:

    def test_returns_only_nse_tickers(self, engine):
        cfg = _make_config(default_exchange="NSE")
        tickers = _select_tickers(engine, cfg, exchange="NSE")
        symbols = {t.symbol for t in tickers}
        # Three active, non-delisted NSE tickers.
        assert symbols == {"RELIANCE", "INFY", "TCS"}

    def test_returns_only_bse_tickers(self, engine):
        cfg = _make_config(default_exchange="NSE")
        tickers = _select_tickers(engine, cfg, exchange="BSE")
        symbols = {t.symbol for t in tickers}
        assert symbols == {"BSEONLY", "BSEMID"}

    def test_returns_only_nyse_tickers(self, engine):
        cfg = _make_config(default_exchange="NSE")
        tickers = _select_tickers(engine, cfg, exchange="NYSE")
        symbols = {t.symbol for t in tickers}
        assert symbols == {"AAPL"}

    def test_no_cross_contamination(self, engine):
        cfg = _make_config(default_exchange="NSE")
        nse = {t.symbol for t in _select_tickers(engine, cfg, exchange="NSE")}
        bse = {t.symbol for t in _select_tickers(engine, cfg, exchange="BSE")}
        nyse = {t.symbol for t in _select_tickers(engine, cfg, exchange="NYSE")}
        # No cross-contamination across the three markets.
        assert nse.isdisjoint(bse)
        assert nse.isdisjoint(nyse)
        assert bse.isdisjoint(nyse)

    def test_config_default_does_not_leak(self, engine):
        """The runner must NOT fall back to config.etl.exchange.
        Passing exchange='BSE' must return BSE tickers regardless of
        what config.etl.exchange happens to say."""
        cfg = _make_config(default_exchange="NSE")
        tickers = _select_tickers(engine, cfg, exchange="BSE")
        symbols = {t.symbol for t in tickers}
        assert symbols == {"BSEONLY", "BSEMID"}

    def test_unknown_exchange_returns_empty(self, engine):
        cfg = _make_config(default_exchange="NSE")
        # `_select_tickers` doesn't itself validate the exchange code
        # against the known suffix map — that's the job of the public
        # entry points. So an unknown exchange simply returns an
        # empty list (no rows match).
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

    def test_bse_exchange_and_tier_filter(self, engine):
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
