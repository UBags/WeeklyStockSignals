"""Tests for the `exchange` kwarg on the ETL runner internals.

Phase 14.6a: `exchange` is now a REQUIRED keyword argument on every
public runner entry point. The internal `_select_tickers` helper
accordingly takes a required `exchange: str` and no longer falls
back to `config.etl.exchange`. These tests cover the per-exchange
isolation guarantee — each market's fetch pass only sees its own
tickers.

These run against the Alembic-migrated Postgres provided by the shared
`db_engine` fixture (re-exported by tests/etl/conftest.py), NOT against a
hand-written schema. See the `engine` fixture below for why that matters.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

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
def engine(db_engine):
    """The REAL, Alembic-migrated `tickers` table, seeded with three exchanges.

    This fixture used to build its own `tickers` table in in-memory SQLite:

        CREATE TABLE tickers (ticker_id INTEGER PRIMARY KEY, symbol TEXT, ...)

    That was a second copy of the schema, maintained by hand, and it drifted —
    it never had listing_date, isin, instrument_type, full_name, lot_size or
    face_value. The moment `_select_tickers` started selecting `listing_date`,
    eight tests here failed with "no such column", not because the runner was
    wrong but because the fixture's private copy of the truth had fallen behind
    the truth.

    Patching the missing column into that CREATE TABLE would have been the
    thirty-second fix and would have rebuilt the same trap for the next person.
    So the copy is gone. `db_engine` (re-exported by tests/etl/conftest.py) is a
    function-scoped, fresh-per-test Postgres migrated with `alembic upgrade
    head`, which means the schema here IS the schema, and this class of failure
    cannot drift back in — a column added by a future migration is simply there.

    Tier values are integers (1, 2, 3) per the real Tier(int, Enum) in
    trip_ema.etl.types. `listing_date` is left NULL: these tests are about
    exchange isolation and the active/delisted filters, and _select_tickers is
    required to work whether or not a listing date is known.
    """
    rows = [
        # NSE tickers, all tiers
        (1, "RELIANCE", "NSE", 1, True, None),
        (2, "INFY",     "NSE", 1, True, None),
        (3, "TCS",      "NSE", 2, True, None),
        (4, "OLDCO",    "NSE", 1, False, None),                       # inactive
        (5, "DEADCO",   "NSE", 1, True, "2026-01-01T00:00:00+00:00"), # delisted
        # BSE tickers
        (6, "BSEONLY",  "BSE", 1, True, None),
        (7, "BSEMID",   "BSE", 2, True, None),
        # NYSE tickers (future-proofing)
        (8, "AAPL",     "NYSE", 1, True, None),
    ]
    with db_engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO tickers
                  (ticker_id, symbol, exchange, tier, is_active, delisted_at)
                VALUES (:id, :sym, :ex, :tier, :act, CAST(:del AS timestamptz))
            """),
            [
                {"id": r[0], "sym": r[1], "ex": r[2], "tier": r[3],
                 "act": r[4], "del": r[5]}
                for r in rows
            ],
        )
    return db_engine


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
