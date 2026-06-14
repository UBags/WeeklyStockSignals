"""Integration tests for select_top_n_constituents and
compute_index_value_for_date (Phase 4B-2).

The selector exercises the real instrument_industry × tickers ×
daily_market_cap JOIN paths. The computer exercises the real
modified Laspeyres math against fixture data with known answers.

Some of these tests are about specific numeric outcomes — when
asserting computed values I use pytest.approx to avoid float-
equality landmines.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text

from trip_ema.domain import TickerRef
from trip_ema.fundamentals.index_compute import (
    ComputedValue,
    SelectedConstituent,
    compute_index_value_for_date,
    select_top_n_constituents,
)
from trip_ema.persistence import transaction
from trip_ema.persistence.fundamentals_repo import (
    DailyMarketCapRepository,
    IndustryIndexConstituentRepository,
    IndustryIndexRepository,
    IndustryOverrideRepository,
    InstrumentIndustryRepository,
)
from trip_ema.persistence.tickers_repo import TickersRepository


# ---------- fixtures ---------- #


@pytest.fixture
def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture
def universe(db_engine, now):
    """A 5-ticker NSE universe spanning two industries, one ETF
    (negative control). Returns dict[(exchange, symbol)] → ticker_id.
    """
    refs = [
        TickerRef("NSE", "RELIANCE",   "EQ"),  # Energy
        TickerRef("NSE", "ONGC",       "EQ"),  # Energy
        TickerRef("NSE", "TCS",        "EQ"),  # IT
        TickerRef("NSE", "INFY",       "EQ"),  # IT
        TickerRef("NSE", "WIPRO",      "EQ"),  # IT
        TickerRef("NSE", "NIFTYBEES",  "EQ"),  # marked ETF below
    ]
    ids: dict[tuple, int] = {}
    with transaction(db_engine) as conn:
        repo = TickersRepository(conn)
        for ref in refs:
            ids[(ref.exchange, ref.symbol)] = repo.insert(ref, created_at=now)
        # Mark the ETF.
        conn.execute(text(
            "UPDATE tickers SET instrument_type='ETF' WHERE ticker_id=:tid"
        ), {"tid": ids[("NSE", "NIFTYBEES")]})
    return ids


def _classify(db_engine, ticker_id: int, sector: str, industry: str):
    with transaction(db_engine) as conn:
        InstrumentIndustryRepository(conn).upsert(
            ticker_id, sector=sector, industry=industry,
        )


def _set_mcap(
    db_engine, ticker_id: int, trade_date: date,
    close: float, shares: int,
):
    """Helper to write one daily_market_cap row."""
    with transaction(db_engine) as conn:
        DailyMarketCapRepository(conn).upsert_snapshot(
            ticker_id, trade_date=trade_date,
            close_price=close, shares_used=shares,
            market_cap=close * shares,
            source="COMPUTED",
        )


# ============================================================
# select_top_n_constituents
# ============================================================


class TestSelectorHappyPath:

    def test_picks_top_n_by_mcap(self, db_engine, universe):
        """3 IT tickers, varied mcaps. n=2 picks the top 2 by mcap."""
        rd = date(2025, 10, 1)
        # Classify all three as IT industry.
        for sym in ("TCS", "INFY", "WIPRO"):
            _classify(db_engine, universe[("NSE", sym)],
                      "Technology", "IT - Services")
        # Set mcaps: TCS=large, INFY=medium, WIPRO=small.
        _set_mcap(db_engine, universe[("NSE", "TCS")],   rd, 4100.0, 3_600_000_000)
        _set_mcap(db_engine, universe[("NSE", "INFY")],  rd, 1500.0, 4_100_000_000)
        _set_mcap(db_engine, universe[("NSE", "WIPRO")], rd,  300.0, 5_400_000_000)

        result = select_top_n_constituents(
            db_engine, "IT - Services", rd, n=2,
        )
        assert len(result) == 2
        # TCS has the highest mcap (4100 × 3.6e9 = 1.476e13).
        # INFY second (1500 × 4.1e9 = 6.15e12).
        # WIPRO third (300 × 5.4e9 = 1.62e12).
        symbols_in_order = [c.symbol for c in result]
        assert symbols_in_order == ["TCS", "INFY"]

    def test_default_n_is_10(self, db_engine, universe):
        """Defaulting to n=10 means we return all 3 IT tickers when
        only 3 are classified."""
        rd = date(2025, 10, 1)
        for sym in ("TCS", "INFY", "WIPRO"):
            _classify(db_engine, universe[("NSE", sym)],
                      "Technology", "IT - Services")
            _set_mcap(db_engine, universe[("NSE", sym)],
                      rd, 100.0, 1_000_000_000)
        result = select_top_n_constituents(
            db_engine, "IT - Services", rd,
        )
        assert len(result) == 3

    def test_weights_sum_to_one(self, db_engine, universe):
        """For any selection, Σ weights == 1.0 modulo float error."""
        rd = date(2025, 10, 1)
        for sym, mc in [("TCS", 5.0), ("INFY", 3.0), ("WIPRO", 2.0)]:
            _classify(db_engine, universe[("NSE", sym)],
                      "Technology", "IT - Services")
            _set_mcap(db_engine, universe[("NSE", sym)],
                      rd, mc, 1_000_000)
        result = select_top_n_constituents(
            db_engine, "IT - Services", rd, n=3,
        )
        total = sum(c.weight for c in result)
        assert total == pytest.approx(1.0, rel=1e-12)

    def test_weight_proportional_to_mcap(self, db_engine, universe):
        """With market caps 5, 3, 2 the weights should be 0.5, 0.3, 0.2."""
        rd = date(2025, 10, 1)
        for sym, mc in [("TCS", 5.0), ("INFY", 3.0), ("WIPRO", 2.0)]:
            _classify(db_engine, universe[("NSE", sym)],
                      "Technology", "IT - Services")
            _set_mcap(db_engine, universe[("NSE", sym)],
                      rd, mc, 1_000_000)
        result = select_top_n_constituents(
            db_engine, "IT - Services", rd, n=3,
        )
        weights = {c.symbol: c.weight for c in result}
        assert weights["TCS"] == pytest.approx(0.5)
        assert weights["INFY"] == pytest.approx(0.3)
        assert weights["WIPRO"] == pytest.approx(0.2)


class TestSelectorFiltering:
    """The filters that scope the picking universe."""

    def test_excludes_other_industries(self, db_engine, universe):
        """Picking 'IT - Services' must not include Energy
        constituents even if their mcaps are larger."""
        rd = date(2025, 10, 1)
        # IT tickers — smaller mcap.
        for sym in ("TCS", "INFY"):
            _classify(db_engine, universe[("NSE", sym)],
                      "Technology", "IT - Services")
            _set_mcap(db_engine, universe[("NSE", sym)],
                      rd, 100.0, 1_000_000)
        # Energy tickers — huge mcap.
        for sym in ("RELIANCE", "ONGC"):
            _classify(db_engine, universe[("NSE", sym)],
                      "Energy", "Oil & Gas")
            _set_mcap(db_engine, universe[("NSE", sym)],
                      rd, 1000.0, 100_000_000_000)
        result = select_top_n_constituents(
            db_engine, "IT - Services", rd, n=10,
        )
        symbols = {c.symbol for c in result}
        assert symbols == {"TCS", "INFY"}

    def test_excludes_etfs(self, db_engine, universe):
        """ETFs must be excluded even if classified into the
        industry (which shouldn't happen via the runner but might
        happen via operator override)."""
        rd = date(2025, 10, 1)
        # Classify the ETF into IT (deliberately).
        _classify(db_engine, universe[("NSE", "NIFTYBEES")],
                  "Technology", "IT - Services")
        _set_mcap(db_engine, universe[("NSE", "NIFTYBEES")],
                  rd, 200.0, 1_000_000_000)
        # And one real EQ ticker.
        _classify(db_engine, universe[("NSE", "TCS")],
                  "Technology", "IT - Services")
        _set_mcap(db_engine, universe[("NSE", "TCS")],
                  rd, 100.0, 1_000_000)
        result = select_top_n_constituents(
            db_engine, "IT - Services", rd, n=10,
        )
        symbols = {c.symbol for c in result}
        assert symbols == {"TCS"}  # NIFTYBEES excluded

    def test_excludes_inactive(self, db_engine, universe):
        """is_active=FALSE → not picked even if mcap is large."""
        rd = date(2025, 10, 1)
        _classify(db_engine, universe[("NSE", "TCS")],
                  "Technology", "IT - Services")
        _set_mcap(db_engine, universe[("NSE", "TCS")],
                  rd, 100.0, 1_000_000_000)
        # Make TCS inactive.
        with transaction(db_engine) as conn:
            conn.execute(text(
                "UPDATE tickers SET is_active = FALSE WHERE ticker_id=:tid"
            ), {"tid": universe[("NSE", "TCS")]})
        result = select_top_n_constituents(
            db_engine, "IT - Services", rd, n=10,
        )
        assert result == []

    def test_excludes_delisted_before_rebalance(self, db_engine, universe):
        """A ticker delisted BEFORE rebalance_date should not be
        picked, but one delisted AFTER should be."""
        rd = date(2025, 10, 1)
        for sym in ("TCS", "INFY"):
            _classify(db_engine, universe[("NSE", sym)],
                      "Technology", "IT - Services")
            _set_mcap(db_engine, universe[("NSE", sym)],
                      rd, 100.0, 1_000_000)
        # TCS delisted 2025-09-15 — before rebalance.
        with transaction(db_engine) as conn:
            conn.execute(text(
                "UPDATE tickers SET delisted_at=:d WHERE ticker_id=:tid"
            ), {"d": date(2025, 9, 15),
                "tid": universe[("NSE", "TCS")]})
        result = select_top_n_constituents(
            db_engine, "IT - Services", rd, n=10,
        )
        symbols = {c.symbol for c in result}
        assert symbols == {"INFY"}

    def test_includes_delisted_after_rebalance(self, db_engine, universe):
        """A ticker delisted AFTER rebalance_date IS picked — it
        was valid at rebalance time. (The compute layer handles
        the mid-cycle delisting via drop-and-renormalize.)"""
        rd = date(2025, 10, 1)
        _classify(db_engine, universe[("NSE", "TCS")],
                  "Technology", "IT - Services")
        _set_mcap(db_engine, universe[("NSE", "TCS")],
                  rd, 100.0, 1_000_000)
        with transaction(db_engine) as conn:
            conn.execute(text(
                "UPDATE tickers SET delisted_at=:d WHERE ticker_id=:tid"
            ), {"d": date(2025, 11, 15),
                "tid": universe[("NSE", "TCS")]})
        result = select_top_n_constituents(
            db_engine, "IT - Services", rd, n=10,
        )
        symbols = {c.symbol for c in result}
        assert "TCS" in symbols

    def test_override_beats_auto_classification(self, db_engine, universe):
        """An industry_override row takes precedence over the
        auto-classified instrument_industry row."""
        rd = date(2025, 10, 1)
        # TCS auto-classified as IT.
        _classify(db_engine, universe[("NSE", "TCS")],
                  "Technology", "IT - Services")
        _set_mcap(db_engine, universe[("NSE", "TCS")],
                  rd, 100.0, 1_000_000)
        # Operator overrides TCS to "Banks - Regional".
        with transaction(db_engine) as conn:
            IndustryOverrideRepository(conn).upsert(
                universe[("NSE", "TCS")],
                sector="Financials", industry="Banks - Regional",
            )
        # Now selecting IT - Services should NOT include TCS.
        result = select_top_n_constituents(
            db_engine, "IT - Services", rd, n=10,
        )
        assert result == []
        # But selecting Banks - Regional SHOULD include it.
        result = select_top_n_constituents(
            db_engine, "Banks - Regional", rd, n=10,
        )
        assert {c.symbol for c in result} == {"TCS"}

    def test_excludes_tickers_with_no_mcap(self, db_engine, universe):
        """Classified into the industry but no mcap row on
        rebalance_date → excluded."""
        rd = date(2025, 10, 1)
        for sym in ("TCS", "INFY"):
            _classify(db_engine, universe[("NSE", sym)],
                      "Technology", "IT - Services")
        # Only TCS gets a mcap row; INFY doesn't.
        _set_mcap(db_engine, universe[("NSE", "TCS")],
                  rd, 100.0, 1_000_000)
        result = select_top_n_constituents(
            db_engine, "IT - Services", rd, n=10,
        )
        symbols = {c.symbol for c in result}
        assert symbols == {"TCS"}

    def test_exchange_filter(self, db_engine, universe, now):
        """When exchange='NSE' is passed, BSE tickers don't appear."""
        rd = date(2025, 10, 1)
        # Add a BSE ticker classified the same.
        with transaction(db_engine) as conn:
            bse_tid = TickersRepository(conn).insert(
                TickerRef("BSE", "TCS", "EQ"), created_at=now,
            )
        _classify(db_engine, bse_tid, "Technology", "IT - Services")
        _set_mcap(db_engine, bse_tid, rd, 100.0, 1_000_000)
        # Plus an NSE one.
        _classify(db_engine, universe[("NSE", "TCS")],
                  "Technology", "IT - Services")
        _set_mcap(db_engine, universe[("NSE", "TCS")],
                  rd, 100.0, 1_000_000)
        # No exchange filter: 2 results.
        all_results = select_top_n_constituents(
            db_engine, "IT - Services", rd, n=10,
        )
        assert len(all_results) == 2
        # With NSE filter: 1 result.
        nse_results = select_top_n_constituents(
            db_engine, "IT - Services", rd, n=10, exchange="NSE",
        )
        assert len(nse_results) == 1


class TestSelectorEdgeCases:

    def test_empty_industry(self, db_engine, universe):
        """No tickers classified into this industry → empty list,
        no crash."""
        result = select_top_n_constituents(
            db_engine, "Made Up Industry", date(2025, 10, 1), n=10,
        )
        assert result == []

    def test_no_mcaps_on_rebalance_date(self, db_engine, universe):
        """All tickers are classified but none have mcap rows on
        the rebalance_date → empty list."""
        _classify(db_engine, universe[("NSE", "TCS")],
                  "Technology", "IT - Services")
        # mcap exists for OTHER dates, not rebalance.
        _set_mcap(db_engine, universe[("NSE", "TCS")],
                  date(2025, 11, 1), 100.0, 1_000_000)
        result = select_top_n_constituents(
            db_engine, "IT - Services", date(2025, 10, 1), n=10,
        )
        assert result == []


# ============================================================
# compute_index_value_for_date
# ============================================================


class TestComputeIndexValueHappyPath:
    """Numeric correctness for the simple cases first."""

    def _setup_index_with_constituents(
        self, db_engine, universe, *,
        rebalance_date: date = date(2025, 10, 1),
    ):
        """Build an 'IT - Services' index with TCS, INFY, WIPRO as
        constituents, each with a different weight and close at
        rebalance.

        Pre-conditions for the value computer:
          - industry_index row exists.
          - industry_index_constituent rows exist with
            weight_at_rebalance, close_at_rebalance.
        """
        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).upsert(
                "IT - Services", sector_name="Technology",
                base_date=rebalance_date, base_value=1000.0,
            )
            cr = IndustryIndexConstituentRepository(conn)
            # Weights: 50% TCS, 30% INFY, 20% WIPRO.
            # Closes at rebalance: 100, 200, 300.
            cr.insert_constituent(
                "IT - Services",
                ticker_id=universe[("NSE", "TCS")],
                rebalance_date=rebalance_date,
                weight_at_rebalance=0.5,
                market_cap_at_rebalance=5.0e11,
                close_at_rebalance=100.0,
            )
            cr.insert_constituent(
                "IT - Services",
                ticker_id=universe[("NSE", "INFY")],
                rebalance_date=rebalance_date,
                weight_at_rebalance=0.3,
                market_cap_at_rebalance=3.0e11,
                close_at_rebalance=200.0,
            )
            cr.insert_constituent(
                "IT - Services",
                ticker_id=universe[("NSE", "WIPRO")],
                rebalance_date=rebalance_date,
                weight_at_rebalance=0.2,
                market_cap_at_rebalance=2.0e11,
                close_at_rebalance=300.0,
            )

    def test_at_rebalance_date_value_equals_base(self, db_engine, universe):
        """Sanity check: at rebalance, every close_i(t) ==
        close_i(t_rebalance), so all ratios are 1.0, weighted sum
        is 1.0, and value = base_value × 1.0 = base_value."""
        rd = date(2025, 10, 1)
        self._setup_index_with_constituents(db_engine, universe,
                                            rebalance_date=rd)
        # Mcap rows on rd with the same close prices as recorded.
        for sym, close in [("TCS", 100.0), ("INFY", 200.0), ("WIPRO", 300.0)]:
            _set_mcap(db_engine, universe[("NSE", sym)],
                      rd, close, 1_000_000)

        cv = compute_index_value_for_date(db_engine, "IT - Services", rd)
        assert cv is not None
        assert cv.value == pytest.approx(1000.0, rel=1e-9)
        assert cv.constituents_used == 3
        assert cv.constituents_missing == 0
        assert cv.renormalized is False
        assert cv.effective_rebalance_date == rd

    def test_all_constituents_up_10_percent(self, db_engine, universe):
        """If every constituent's price goes up 10%, the index
        also goes up exactly 10% (the weights don't matter for
        this case because every ratio is 1.1)."""
        rd = date(2025, 10, 1)
        td = date(2025, 11, 1)
        self._setup_index_with_constituents(db_engine, universe,
                                            rebalance_date=rd)
        # 10% higher than the closes at rebalance.
        for sym, close in [("TCS", 110.0), ("INFY", 220.0), ("WIPRO", 330.0)]:
            _set_mcap(db_engine, universe[("NSE", sym)],
                      td, close, 1_000_000)

        cv = compute_index_value_for_date(db_engine, "IT - Services", td)
        assert cv is not None
        assert cv.value == pytest.approx(1100.0, rel=1e-9)
        assert cv.constituents_used == 3
        assert cv.renormalized is False

    def test_mixed_movements_correct_weighting(self, db_engine, universe):
        """TCS (50% weight) flat. INFY (30%) up 50%. WIPRO (20%)
        down 50%.
        Expected = 1000 × (0.5×1.0 + 0.3×1.5 + 0.2×0.5)
                 = 1000 × (0.5 + 0.45 + 0.1)
                 = 1000 × 1.05
                 = 1050.0"""
        rd = date(2025, 10, 1)
        td = date(2025, 11, 1)
        self._setup_index_with_constituents(db_engine, universe,
                                            rebalance_date=rd)
        # Movements relative to closes (100, 200, 300).
        for sym, close in [("TCS", 100.0), ("INFY", 300.0), ("WIPRO", 150.0)]:
            _set_mcap(db_engine, universe[("NSE", sym)],
                      td, close, 1_000_000)
        cv = compute_index_value_for_date(db_engine, "IT - Services", td)
        assert cv.value == pytest.approx(1050.0, rel=1e-9)
        assert cv.constituents_used == 3
        assert cv.renormalized is False


class TestComputeIndexValueDropAndRenormalize:
    """The interesting case: a constituent is missing for the day."""

    def _setup(self, db_engine, universe):
        rd = date(2025, 10, 1)
        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).upsert(
                "IT - Services", sector_name="Technology",
                base_date=rd, base_value=1000.0,
            )
            cr = IndustryIndexConstituentRepository(conn)
            cr.insert_constituent(
                "IT - Services",
                ticker_id=universe[("NSE", "TCS")],
                rebalance_date=rd,
                weight_at_rebalance=0.5,
                market_cap_at_rebalance=5.0e11,
                close_at_rebalance=100.0,
            )
            cr.insert_constituent(
                "IT - Services",
                ticker_id=universe[("NSE", "INFY")],
                rebalance_date=rd,
                weight_at_rebalance=0.3,
                market_cap_at_rebalance=3.0e11,
                close_at_rebalance=200.0,
            )
            cr.insert_constituent(
                "IT - Services",
                ticker_id=universe[("NSE", "WIPRO")],
                rebalance_date=rd,
                weight_at_rebalance=0.2,
                market_cap_at_rebalance=2.0e11,
                close_at_rebalance=300.0,
            )
        return rd

    def test_one_constituent_missing_renormalizes(self, db_engine, universe):
        """WIPRO (20% original weight) has no mcap on td. Remaining
        constituents (TCS 50%, INFY 30%) get renormalized to
        62.5% and 37.5% of the present-weight-sum (0.8).

        TCS up 10%, INFY up 20%:
        renormalized formula:
            (0.5 / 0.8) × 1.10 + (0.3 / 0.8) × 1.20
          = 0.625 × 1.10 + 0.375 × 1.20
          = 0.6875 + 0.45
          = 1.1375
        value = 1000 × 1.1375 = 1137.5"""
        rd = self._setup(db_engine, universe)
        td = date(2025, 11, 1)
        _set_mcap(db_engine, universe[("NSE", "TCS")],
                  td, 110.0, 1_000_000)
        _set_mcap(db_engine, universe[("NSE", "INFY")],
                  td, 240.0, 1_000_000)
        # WIPRO: no mcap row on td.

        cv = compute_index_value_for_date(db_engine, "IT - Services", td)
        assert cv is not None
        assert cv.value == pytest.approx(1137.5, rel=1e-9)
        assert cv.constituents_used == 2
        assert cv.constituents_missing == 1
        assert cv.renormalized is True

    def test_all_constituents_missing_returns_None(
        self, db_engine, universe,
    ):
        """If NO constituents have mcap on this day, we can't
        compute anything. Return None — the runner won't write
        a row."""
        self._setup(db_engine, universe)
        td = date(2025, 11, 1)
        # No mcap inserts at all on td.
        cv = compute_index_value_for_date(db_engine, "IT - Services", td)
        assert cv is None


class TestComputeIndexValuePrebaseAndMissingIndex:

    def test_pre_base_date_returns_None(self, db_engine, universe):
        """trade_date < base_date → return None silently."""
        rd = date(2025, 10, 1)
        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).upsert(
                "IT - Services", sector_name="Technology",
                base_date=rd, base_value=1000.0,
            )
        # No constituents — but we expect early return on the
        # pre-base check before that matters.
        td = date(2025, 9, 1)
        cv = compute_index_value_for_date(db_engine, "IT - Services", td)
        assert cv is None

    def test_unknown_index_returns_None(self, db_engine):
        """No row in industry_index → None."""
        cv = compute_index_value_for_date(
            db_engine, "Made Up Industry", date(2025, 10, 1),
        )
        assert cv is None

    def test_no_constituents_returns_None(self, db_engine, universe):
        """Index exists but list_as_of returns empty."""
        rd = date(2025, 10, 1)
        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).upsert(
                "IT - Services", sector_name="Technology",
                base_date=rd, base_value=1000.0,
            )
        # No constituents inserted.
        cv = compute_index_value_for_date(
            db_engine, "IT - Services", rd,
        )
        assert cv is None


class TestComputeIndexValueAcrossRebalances:
    """Verify that compute_index_value_for_date picks the correct
    rebalance period's weights when there are multiple rebalances
    in history."""

    def test_picks_latest_rebalance_for_post_apr_date(
        self, db_engine, universe,
    ):
        """Two rebalances: 2025-10-01 (TCS+INFY) and 2026-04-01
        (INFY+WIPRO with different weights). A trade_date in May
        2026 should use the April rebalance, not the October one."""
        oct_rd = date(2025, 10, 1)
        apr_rd = date(2026, 4, 1)

        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).upsert(
                "IT - Services", sector_name="Technology",
                base_date=oct_rd, base_value=1000.0,
            )
            cr = IndustryIndexConstituentRepository(conn)
            # October rebalance: TCS 60%, INFY 40%.
            cr.insert_constituent(
                "IT - Services",
                ticker_id=universe[("NSE", "TCS")],
                rebalance_date=oct_rd,
                weight_at_rebalance=0.6,
                market_cap_at_rebalance=6.0e11,
                close_at_rebalance=100.0,
            )
            cr.insert_constituent(
                "IT - Services",
                ticker_id=universe[("NSE", "INFY")],
                rebalance_date=oct_rd,
                weight_at_rebalance=0.4,
                market_cap_at_rebalance=4.0e11,
                close_at_rebalance=200.0,
            )
            # April rebalance: close prior, open new with very
            # different weights.
            cr.close_period("IT - Services",
                            ticker_id=universe[("NSE", "TCS")],
                            effective_to=date(2026, 3, 31),
                            removed_reason="REBALANCE_EXIT")
            cr.close_period("IT - Services",
                            ticker_id=universe[("NSE", "INFY")],
                            effective_to=date(2026, 3, 31),
                            removed_reason="REBALANCE_EXIT")
            cr.insert_constituent(
                "IT - Services",
                ticker_id=universe[("NSE", "INFY")],
                rebalance_date=apr_rd,
                weight_at_rebalance=0.3,
                market_cap_at_rebalance=3.0e11,
                close_at_rebalance=220.0,  # different from Oct
            )
            cr.insert_constituent(
                "IT - Services",
                ticker_id=universe[("NSE", "WIPRO")],
                rebalance_date=apr_rd,
                weight_at_rebalance=0.7,
                market_cap_at_rebalance=7.0e11,
                close_at_rebalance=400.0,
            )

        # Trade date in May 2026 — should use April rebalance.
        td = date(2026, 5, 1)
        for sym, close in [("INFY", 220.0), ("WIPRO", 400.0)]:
            _set_mcap(db_engine, universe[("NSE", sym)],
                      td, close, 1_000_000)
        # TCS has no mcap, but TCS isn't a constituent anymore.

        cv = compute_index_value_for_date(db_engine, "IT - Services", td)
        assert cv is not None
        # Value should be 1000 (= base × 1.0 since all closes match
        # rebalance closes). NOT touched by missing TCS because
        # TCS isn't in the April rebalance.
        assert cv.value == pytest.approx(1000.0, rel=1e-9)
        assert cv.constituents_used == 2
        assert cv.constituents_missing == 0
        assert cv.effective_rebalance_date == apr_rd
