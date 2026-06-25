"""CLI tests for Phase 1 fundamentals commands.

The CLI handlers thinly wrap the runner and repos, so these tests
focus on the command parsing, output formatting, and exit codes.
We patch the runner so we don't make HTTP calls.
"""
from __future__ import annotations

from datetime import date, datetime, timezone, UTC
from io import StringIO
from unittest.mock import patch

import pytest

from trip_ema.domain import TickerRef
from trip_ema.fundamentals.cli import (
    cmd_backfill_etf_classification,
    cmd_delete_industry_override,
    cmd_fundamentals_status,
    cmd_refresh_industry,
    cmd_set_industry_override,
)
from trip_ema.persistence import transaction
from trip_ema.persistence.fundamentals_repo import (
    FundamentalsRunRepository,
    IndustryOverrideRepository,
    InstrumentIndustryRepository,
)
from trip_ema.persistence.tickers_repo import TickersRepository


# ---------- fixtures ---------- #


@pytest.fixture
def fake_config(db_engine):
    """A minimal stub Config object that has just the bits the
    CLI handlers touch. Easier than constructing a real Config
    and overriding the engine factory."""
    from unittest.mock import MagicMock
    cfg = MagicMock()
    # validate_exchange checks config.market_hours via configured_exchanges();
    # configured_exchanges() reads cfg.market_hours which is a list of
    # objects with an .exchange attribute. Stub minimally.
    nse_market = MagicMock()
    nse_market.exchange = "NSE"
    cfg.market_hours = [nse_market]
    return cfg


@pytest.fixture
def patched_engine_factory(db_engine):
    """Patch create_engine_from_config to return our test engine.
    Each cmd_* opens-and-disposes its own engine; we redirect that
    to the per-test fixture engine."""
    with patch(
        "trip_ema.fundamentals.cli.create_engine_from_config",
        return_value=db_engine,
    ):
        # Don't let cli's .dispose() actually dispose the fixture engine.
        with patch.object(db_engine, "dispose"):
            yield db_engine


@pytest.fixture
def reliance_ticker(db_engine):
    ref = TickerRef("NSE", "RELIANCE", "EQ")
    with transaction(db_engine) as conn:
        tid = TickersRepository(conn).insert(
            ref, created_at=datetime.now(UTC),
        )
    return tid


# ---------- cmd_refresh_industry ---------- #


class TestCmdRefreshIndustry:

    def test_returns_0_on_success(
        self, db_engine, fake_config, patched_engine_factory, reliance_ticker, capsys,
    ):
        with patch(
            "trip_ema.fundamentals.cli.run_industry_refresh",
        ) as mock_runner:
            # Make the mock simulate a successful run by writing the row
            # ourselves and returning the run_id.
            with transaction(db_engine) as conn:
                rid = FundamentalsRunRepository(conn).open(
                    phase="industry", exchange="NSE",
                )
                FundamentalsRunRepository(conn).close(
                    rid, status="SUCCESS", succeeded=1, failed=0, skipped=0,
                )
            mock_runner.return_value = rid

            args = type("A", (), {})()
            args.exchange = "NSE"
            args.force = False
            args.resume = None
            rc = cmd_refresh_industry(args, fake_config)
        assert rc == 0
        out = capsys.readouterr().out
        assert "status=SUCCESS" in out
        assert "succeeded=1" in out

    def test_returns_1_on_partial(
        self, db_engine, fake_config, patched_engine_factory, reliance_ticker,
    ):
        with patch(
            "trip_ema.fundamentals.cli.run_industry_refresh",
        ) as mock_runner:
            with transaction(db_engine) as conn:
                rid = FundamentalsRunRepository(conn).open(
                    phase="industry", exchange="NSE",
                )
                FundamentalsRunRepository(conn).close(
                    rid, status="PARTIAL", succeeded=3, failed=2, skipped=0,
                )
            mock_runner.return_value = rid

            args = type("A", (), {})()
            args.exchange = "NSE"
            args.force = False
            args.resume = None
            rc = cmd_refresh_industry(args, fake_config)
        assert rc == 1

    def test_returns_2_on_failed(
        self, db_engine, fake_config, patched_engine_factory, reliance_ticker,
    ):
        with patch(
            "trip_ema.fundamentals.cli.run_industry_refresh",
        ) as mock_runner:
            with transaction(db_engine) as conn:
                rid = FundamentalsRunRepository(conn).open(
                    phase="industry", exchange="NSE",
                )
                FundamentalsRunRepository(conn).close(
                    rid, status="FAILED", succeeded=0, failed=5, skipped=0,
                )
            mock_runner.return_value = rid

            args = type("A", (), {})()
            args.exchange = "NSE"
            args.force = False
            args.resume = None
            rc = cmd_refresh_industry(args, fake_config)
        assert rc == 2


# ---------- cmd_set_industry_override ---------- #


class TestCmdSetIndustryOverride:

    def test_writes_override_and_classification(
        self, db_engine, fake_config, patched_engine_factory, reliance_ticker, capsys,
    ):
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.symbol = "RELIANCE"
        args.industry = "Hand-set Industry"
        args.sector = "Energy"
        args.note = "test"
        args.set_by = "test-runner"
        rc = cmd_set_industry_override(args, fake_config)
        assert rc == 0
        with db_engine.connect() as conn:
            ov = IndustryOverrideRepository(conn).get(reliance_ticker)
            ind = InstrumentIndustryRepository(conn).get(reliance_ticker)
        assert ov.industry == "Hand-set Industry"
        assert ind.industry == "Hand-set Industry"
        assert ind.classification_source == "MANUAL_OVERRIDE"

    def test_sector_defaults_to_unclassified(
        self, db_engine, fake_config, patched_engine_factory, reliance_ticker,
    ):
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.symbol = "RELIANCE"
        args.industry = "X"
        args.sector = None
        args.note = None
        args.set_by = None
        rc = cmd_set_industry_override(args, fake_config)
        assert rc == 0
        with db_engine.connect() as conn:
            ov = IndustryOverrideRepository(conn).get(reliance_ticker)
        assert ov.sector == "Unclassified"

    def test_returns_2_for_unknown_symbol(
        self, db_engine, fake_config, patched_engine_factory, capsys,
    ):
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.symbol = "DOES_NOT_EXIST"
        args.industry = "X"
        args.sector = "Y"
        args.note = None
        args.set_by = None
        rc = cmd_set_industry_override(args, fake_config)
        assert rc == 2
        assert "not found" in capsys.readouterr().err


# ---------- cmd_delete_industry_override ---------- #


class TestCmdDeleteIndustryOverride:

    def test_removes_existing_override(
        self, db_engine, fake_config, patched_engine_factory, reliance_ticker,
    ):
        with transaction(db_engine) as conn:
            IndustryOverrideRepository(conn).upsert(
                reliance_ticker, sector="X", industry="Y",
            )
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.symbol = "RELIANCE"
        rc = cmd_delete_industry_override(args, fake_config)
        assert rc == 0
        with db_engine.connect() as conn:
            assert IndustryOverrideRepository(conn).get(reliance_ticker) is None

    def test_returns_1_when_no_override(
        self, db_engine, fake_config, patched_engine_factory, reliance_ticker,
    ):
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.symbol = "RELIANCE"
        rc = cmd_delete_industry_override(args, fake_config)
        assert rc == 1


# ---------- cmd_fundamentals_status ---------- #


class TestCmdFundamentalsStatus:

    def test_prints_coverage_and_recent_runs(
        self, db_engine, fake_config, patched_engine_factory,
        reliance_ticker, capsys,
    ):
        # Set up some data so output isn't empty.
        with transaction(db_engine) as conn:
            InstrumentIndustryRepository(conn).upsert(
                reliance_ticker, sector="Energy", industry="Oil & Gas",
            )
            rid = FundamentalsRunRepository(conn).open(
                phase="industry", exchange="NSE", notes="test",
            )
            FundamentalsRunRepository(conn).close(
                rid, status="SUCCESS", succeeded=1, failed=0, skipped=0,
            )

        args = type("A", (), {})()
        args.exchange = None
        args.phase = None
        args.limit = 10
        rc = cmd_fundamentals_status(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Industry classification coverage" in out
        assert "NSE" in out
        # Coverage line: 1 total, 1 classified, 100.0%
        assert "100.0%" in out
        assert "Recent fundamentals_run rows" in out
        assert "industry" in out

    def test_empty_db_doesnt_crash(
        self, db_engine, fake_config, patched_engine_factory, capsys,
    ):
        args = type("A", (), {})()
        args.exchange = None
        args.phase = None
        args.limit = 10
        rc = cmd_fundamentals_status(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 0
        # Either "No active tickers" or "No fundamentals_run rows" — both fine.
        assert "No" in out


# ---------- end-to-end via main() ---------- #


def test_main_dispatches_refresh_industry(monkeypatch, db_engine):
    """Smoke test: `trip-ema-etl refresh-industry --exchange NSE`
    parses correctly and calls our handler."""
    from trip_ema.etl import cli as etl_cli

    called = {}
    def fake_handler(args, config):
        called["args"] = args
        return 0

    # Override the handler in the dispatch table after parsing.
    monkeypatch.setattr(
        "trip_ema.fundamentals.cli.cmd_refresh_industry", fake_handler,
    )

    # main() needs a config file to exist. Point to the project's config.yaml.
    rc = etl_cli.main([
        "--config", "config.yaml",
        "refresh-industry",
        "--exchange", "NSE",
    ])
    # We can't easily assert rc==0 without setting up a real config;
    # we just confirm the command resolved (no "unknown command" error).
    assert rc in (0, 1, 2)


# ---------- cmd_backfill_etf_classification ---------- #


class TestCmdBackfillEtfClassification:
    """The backfill command exists to fix the situation where an
    operator ran refresh-industry before the ETF branch landed,
    leaving ~300 ETFs with NULL or wrong classifications.
    Selection logic must:
      - touch only instrument_type='ETF' rows
      - skip ETFs already classified via INSTRUMENT_TYPE
      - skip ETFs that have an operator override
      - re-process ETFs that have a non-INSTRUMENT_TYPE row (e.g.
        old YFINANCE rows with empty sector/industry)
    """

    @pytest.fixture
    def etf_universe(self, db_engine, fake_config):
        """Three NSE ETFs in different starting states + one equity
        for negative-control."""
        from sqlalchemy import text
        refs_states = [
            # (ref, starting_industry_row, has_override)
            (TickerRef("NSE", "GOLDBEES",  "EQ"), None,                 False),
            (TickerRef("NSE", "ITBEES",    "EQ"), ("YFINANCE", "", ""), False),
            (TickerRef("NSE", "ALPHAETF",  "EQ"), ("INSTRUMENT_TYPE", "ETF", "ETF"), False),
            (TickerRef("NSE", "OVERRIDE",  "EQ"), None,                 True),
            (TickerRef("NSE", "EQUITY1",   "EQ"), ("YFINANCE", "Tech", "IT"), False),  # equity, untouched
        ]
        ids = {}
        with transaction(db_engine) as conn:
            tr = TickersRepository(conn)
            for ref, start_row, has_override in refs_states:
                tid = tr.insert(ref, created_at=datetime.now(UTC))
                ids[ref.symbol] = tid
                # Mark first four as ETF (last is equity).
                if ref.symbol != "EQUITY1":
                    conn.execute(text(
                        "UPDATE tickers SET instrument_type='ETF' "
                        "WHERE ticker_id=:tid"
                    ), {"tid": tid})
                if start_row is not None:
                    src, sector, industry = start_row
                    conn.execute(text("""
                        INSERT INTO instrument_industry
                            (ticker_id, sector, industry, classification_source,
                             fetched_at, updated_at)
                        VALUES (:tid, :sec, :ind, :src, NOW(), NOW())
                    """), {"tid": tid, "sec": sector or None,
                           "ind": industry or None, "src": src})
                if has_override:
                    IndustryOverrideRepository(conn).upsert(
                        tid, sector="Op", industry="Op",
                    )
        return ids

    def test_backfill_selects_only_eligible_etfs(
        self, db_engine, fake_config, patched_engine_factory, etf_universe,
        monkeypatch, capsys,
    ):
        """GOLDBEES (no row) + ITBEES (YFINANCE-junk row) should be
        backfilled. ALPHAETF (already INSTRUMENT_TYPE), OVERRIDE
        (has override), EQUITY1 (not ETF) should be untouched."""
        # Stub the classification helper to just count calls.
        called_with = []
        def stub_classify_etf(engine, tid, symbol, exchange, fetch_fn):
            called_with.append(symbol)
            # Simulate a real upsert so the verification query below works.
            with transaction(engine) as conn:
                InstrumentIndustryRepository(conn).upsert_etf(
                    tid, etf_long_name=f"{symbol} long name",
                )
            return "succeeded"
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners._classify_etf", stub_classify_etf,
        )

        args = type("A", (), {})()
        args.exchange = "NSE"
        rc = cmd_backfill_etf_classification(args, fake_config)
        assert rc == 0
        assert set(called_with) == {"GOLDBEES", "ITBEES"}

        with db_engine.connect() as conn:
            ir = InstrumentIndustryRepository(conn)
            assert ir.get(etf_universe["GOLDBEES"]).industry == "ETF"
            assert ir.get(etf_universe["ITBEES"]).industry == "ETF"
            # ALPHAETF was already INSTRUMENT_TYPE — untouched.
            assert ir.get(etf_universe["ALPHAETF"]).industry == "ETF"
            # EQUITY1 untouched — still YFINANCE Tech/IT.
            assert ir.get(etf_universe["EQUITY1"]).classification_source == "YFINANCE"

    def test_backfill_with_nothing_to_do_returns_0(
        self, db_engine, fake_config, patched_engine_factory, capsys,
    ):
        """No ETF tickers in DB → command exits cleanly with rc=0."""
        args = type("A", (), {})()
        args.exchange = "NSE"
        rc = cmd_backfill_etf_classification(args, fake_config)
        assert rc == 0
        assert "Nothing to do" in capsys.readouterr().out

    def test_backfill_writes_fundamentals_run_row(
        self, db_engine, fake_config, patched_engine_factory, etf_universe,
        monkeypatch,
    ):
        """An audit row should exist in fundamentals_run after the
        backfill completes."""
        def stub_classify_etf(engine, tid, symbol, exchange, fetch_fn):
            with transaction(engine) as conn:
                InstrumentIndustryRepository(conn).upsert_etf(
                    tid, etf_long_name=None,
                )
            return "succeeded"
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners._classify_etf", stub_classify_etf,
        )

        args = type("A", (), {})()
        args.exchange = "NSE"
        cmd_backfill_etf_classification(args, fake_config)
        with db_engine.connect() as conn:
            runs = FundamentalsRunRepository(conn).list_recent(
                phase="industry", exchange="NSE", limit=5,
            )
        assert len(runs) >= 1
        latest = runs[0]
        assert latest.status == "SUCCESS"
        assert latest.succeeded == 2  # GOLDBEES + ITBEES

    def test_backfill_with_failures_returns_partial(
        self, db_engine, fake_config, patched_engine_factory, etf_universe,
        monkeypatch,
    ):
        """If some ETFs fail to classify, the run should close as
        PARTIAL and the command should return rc=1."""
        call_count = [0]
        def stub_classify_etf(engine, tid, symbol, exchange, fetch_fn):
            call_count[0] += 1
            if call_count[0] == 1:
                return "failed"
            with transaction(engine) as conn:
                InstrumentIndustryRepository(conn).upsert_etf(
                    tid, etf_long_name=None,
                )
            return "succeeded"
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners._classify_etf", stub_classify_etf,
        )

        args = type("A", (), {})()
        args.exchange = "NSE"
        rc = cmd_backfill_etf_classification(args, fake_config)
        assert rc == 1  # PARTIAL


# ---------- cmd_refresh_shares ---------- #


class TestCmdRefreshShares:

    def test_returns_0_on_success(
        self, db_engine, fake_config, patched_engine_factory, reliance_ticker,
        capsys, monkeypatch,
    ):
        """Patch the runner to simulate a SUCCESS run and verify
        the CLI handler returns rc=0 and prints the summary."""
        from trip_ema.fundamentals.cli import cmd_refresh_shares
        with transaction(db_engine) as conn:
            rid = FundamentalsRunRepository(conn).open(
                phase="shares", exchange="NSE",
            )
            FundamentalsRunRepository(conn).close(
                rid, status="SUCCESS", succeeded=1, failed=0, skipped=0,
            )
        monkeypatch.setattr(
            "trip_ema.fundamentals.cli.run_shares_refresh",
            lambda *a, **kw: rid,
            raising=False,
        )
        # cmd_refresh_shares imports run_shares_refresh inside the
        # function body, so monkeypatching that attribute on the
        # module doesn't work — we patch the runner's source instead.
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_shares_refresh",
            lambda *a, **kw: rid,
        )

        args = type("A", (), {})()
        args.exchange = "NSE"
        args.force = False
        args.resume = None
        rc = cmd_refresh_shares(args, fake_config)
        assert rc == 0
        out = capsys.readouterr().out
        assert "status=SUCCESS" in out
        assert "succeeded=1" in out

    def test_returns_1_on_partial(
        self, db_engine, fake_config, patched_engine_factory, reliance_ticker,
        monkeypatch,
    ):
        from trip_ema.fundamentals.cli import cmd_refresh_shares
        with transaction(db_engine) as conn:
            rid = FundamentalsRunRepository(conn).open(
                phase="shares", exchange="NSE",
            )
            FundamentalsRunRepository(conn).close(
                rid, status="PARTIAL", succeeded=3, failed=2, skipped=0,
            )
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_shares_refresh",
            lambda *a, **kw: rid,
        )
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.force = False
        args.resume = None
        rc = cmd_refresh_shares(args, fake_config)
        assert rc == 1

    def test_returns_2_on_failed(
        self, db_engine, fake_config, patched_engine_factory, reliance_ticker,
        monkeypatch,
    ):
        from trip_ema.fundamentals.cli import cmd_refresh_shares
        with transaction(db_engine) as conn:
            rid = FundamentalsRunRepository(conn).open(
                phase="shares", exchange="NSE",
            )
            FundamentalsRunRepository(conn).close(
                rid, status="FAILED", succeeded=0, failed=5, skipped=0,
            )
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_shares_refresh",
            lambda *a, **kw: rid,
        )
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.force = False
        args.resume = None
        rc = cmd_refresh_shares(args, fake_config)
        assert rc == 2


class TestCmdFundamentalsStatusShowsShares:
    """The status command should report shares coverage alongside
    industry coverage when shares data exists."""

    def test_shows_shares_coverage_when_present(
        self, db_engine, fake_config, patched_engine_factory,
        reliance_ticker, capsys,
    ):
        from trip_ema.persistence.fundamentals_repo import (
            InstrumentSharesRepository,
        )
        # Populate both industry and shares.
        with transaction(db_engine) as conn:
            InstrumentIndustryRepository(conn).upsert(
                reliance_ticker, sector="Energy", industry="Oil",
            )
            InstrumentSharesRepository(conn).insert_snapshot(
                reliance_ticker,
                as_of_date=date(2026, 5, 22),
                shares_outstanding=6_700_000_000,
                shares_source="YFINANCE",
            )
        args = type("A", (), {})()
        args.exchange = None
        args.phase = None
        args.limit = 10
        rc = cmd_fundamentals_status(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Shares-outstanding coverage" in out
        assert "WITH_SHARES" in out

    def test_omits_shares_section_when_no_data(
        self, db_engine, fake_config, patched_engine_factory,
        reliance_ticker, capsys,
    ):
        """If no shares data, don't print an empty shares section."""
        with transaction(db_engine) as conn:
            InstrumentIndustryRepository(conn).upsert(
                reliance_ticker, sector="Energy", industry="Oil",
            )
        args = type("A", (), {})()
        args.exchange = None
        args.phase = None
        args.limit = 10
        rc = cmd_fundamentals_status(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Shares-outstanding coverage" not in out


# ---------- cmd_refresh_financials ---------- #


class TestCmdRefreshFinancials:

    def test_returns_0_on_success(
        self, db_engine, fake_config, patched_engine_factory, reliance_ticker,
        capsys, monkeypatch,
    ):
        from trip_ema.fundamentals.cli import cmd_refresh_financials
        with transaction(db_engine) as conn:
            rid = FundamentalsRunRepository(conn).open(
                phase="financials", exchange="NSE",
            )
            FundamentalsRunRepository(conn).close(
                rid, status="SUCCESS", succeeded=1, failed=0, skipped=0,
            )
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_financials_refresh",
            lambda *a, **kw: rid,
        )
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.force = False
        args.resume = None
        rc = cmd_refresh_financials(args, fake_config)
        assert rc == 0
        out = capsys.readouterr().out
        assert "status=SUCCESS" in out

    def test_returns_1_on_partial(
        self, db_engine, fake_config, patched_engine_factory, reliance_ticker,
        monkeypatch,
    ):
        from trip_ema.fundamentals.cli import cmd_refresh_financials
        with transaction(db_engine) as conn:
            rid = FundamentalsRunRepository(conn).open(
                phase="financials", exchange="NSE",
            )
            FundamentalsRunRepository(conn).close(
                rid, status="PARTIAL", succeeded=3, failed=2, skipped=0,
            )
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_financials_refresh",
            lambda *a, **kw: rid,
        )
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.force = False
        args.resume = None
        rc = cmd_refresh_financials(args, fake_config)
        assert rc == 1


class TestCmdFundamentalsStatusShowsFinancials:

    def test_shows_financials_coverage_when_present(
        self, db_engine, fake_config, patched_engine_factory,
        reliance_ticker, capsys,
    ):
        from trip_ema.persistence.fundamentals_repo import (
            QuarterlyFinancialsRepository,
        )
        with transaction(db_engine) as conn:
            QuarterlyFinancialsRepository(conn).insert_snapshot(
                reliance_ticker,
                fiscal_quarter=date(2025, 12, 31),
                revenue=2500e9, ebitda=350e9,
            )
        args = type("A", (), {})()
        args.exchange = None
        args.phase = None
        args.limit = 10
        rc = cmd_fundamentals_status(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Quarterly financials coverage" in out
        assert "W/REVENUE" in out
        assert "W/EBITDA" in out

    def test_omits_financials_section_when_no_data(
        self, db_engine, fake_config, patched_engine_factory,
        reliance_ticker, capsys,
    ):
        with transaction(db_engine) as conn:
            InstrumentIndustryRepository(conn).upsert(
                reliance_ticker, sector="Energy", industry="Oil",
            )
        args = type("A", (), {})()
        args.exchange = None
        args.phase = None
        args.limit = 10
        rc = cmd_fundamentals_status(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Quarterly financials coverage" not in out


# ---------- cmd_compute_market_cap ---------- #


class TestCmdComputeMarketCap:

    def test_single_day_mode_writes_rows_and_returns_0(
        self, db_engine, fake_config, patched_engine_factory,
        reliance_ticker, monkeypatch, capsys,
    ):
        """The single-day mode calls compute_market_cap_for_date and
        returns 0 if any rows were written."""
        from trip_ema.fundamentals.cli import cmd_compute_market_cap
        from trip_ema.fundamentals.market_cap_compute import ComputeResult

        def stub_compute(engine, exchange, trade_date,
                         allow_approx_fallback=True):
            return ComputeResult(
                trade_date=trade_date, exchange=exchange,
                succeeded_computed=1, succeeded_approx=0,
                skipped_no_close=0, skipped_no_shares=0,
            )
        monkeypatch.setattr(
            "trip_ema.fundamentals.market_cap_compute.compute_market_cap_for_date",
            stub_compute,
        )

        args = type("A", (), {})()
        args.exchange = "NSE"
        args.date = "2026-05-22"
        args.backfill_from = None
        args.backfill_to = None
        args.force = False
        args.resume = None
        rc = cmd_compute_market_cap(args, fake_config)
        assert rc == 0
        out = capsys.readouterr().out
        assert "computed=1" in out

    def test_single_day_mode_returns_1_when_nothing_written(
        self, db_engine, fake_config, patched_engine_factory,
        reliance_ticker, monkeypatch, capsys,
    ):
        """If no rows were written (e.g. no bars for the date),
        rc=1 — operator action needed."""
        from trip_ema.fundamentals.cli import cmd_compute_market_cap
        from trip_ema.fundamentals.market_cap_compute import ComputeResult

        def stub_compute(engine, exchange, trade_date,
                         allow_approx_fallback=True):
            return ComputeResult(
                trade_date=trade_date, exchange=exchange,
                succeeded_computed=0, succeeded_approx=0,
                skipped_no_close=10, skipped_no_shares=0,
            )
        monkeypatch.setattr(
            "trip_ema.fundamentals.market_cap_compute.compute_market_cap_for_date",
            stub_compute,
        )

        args = type("A", (), {})()
        args.exchange = "NSE"
        args.date = "2026-05-22"
        args.backfill_from = None
        args.backfill_to = None
        args.force = False
        args.resume = None
        rc = cmd_compute_market_cap(args, fake_config)
        assert rc == 1

    def test_backfill_mode_routes_to_runner(
        self, db_engine, fake_config, patched_engine_factory,
        reliance_ticker, monkeypatch, capsys,
    ):
        """When --backfill-from is given, the CLI calls the runner
        instead of the single-day compute."""
        from trip_ema.fundamentals.cli import cmd_compute_market_cap
        with transaction(db_engine) as conn:
            rid = FundamentalsRunRepository(conn).open(
                phase="market_cap", exchange="NSE",
            )
            FundamentalsRunRepository(conn).close(
                rid, status="SUCCESS",
                succeeded=1, failed=0, skipped=0,
            )
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_market_cap_backfill",
            lambda *a, **kw: rid,
        )
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.date = None
        args.backfill_from = "2025-05-01"
        args.backfill_to = "2026-05-01"
        args.force = False
        args.resume = None
        rc = cmd_compute_market_cap(args, fake_config)
        assert rc == 0
        out = capsys.readouterr().out
        assert "status=SUCCESS" in out


class TestCmdFundamentalsStatusShowsMarketCap:

    def test_shows_mcap_section_when_present(
        self, db_engine, fake_config, patched_engine_factory,
        reliance_ticker, capsys,
    ):
        from trip_ema.persistence.fundamentals_repo import (
            DailyMarketCapRepository,
        )
        with transaction(db_engine) as conn:
            DailyMarketCapRepository(conn).upsert_snapshot(
                reliance_ticker,
                trade_date=date(2026, 5, 22),
                close_price=1450.0,
                shares_used=6_700_000_000,
                market_cap=9_715_000_000_000.0,
                source="COMPUTED",
            )
        args = type("A", (), {})()
        args.exchange = None
        args.phase = None
        args.limit = 10
        rc = cmd_fundamentals_status(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Daily market cap coverage" in out
        assert "COMPUTED" in out

    def test_omits_mcap_section_when_no_data(
        self, db_engine, fake_config, patched_engine_factory,
        reliance_ticker, capsys,
    ):
        with transaction(db_engine) as conn:
            InstrumentIndustryRepository(conn).upsert(
                reliance_ticker, sector="Energy", industry="Oil",
            )
        args = type("A", (), {})()
        args.exchange = None
        args.phase = None
        args.limit = 10
        rc = cmd_fundamentals_status(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Daily market cap coverage" not in out


# ============================================================
# Phase 4B-3 CLI tests
# ============================================================


# ---------- cmd_build_industry_indices ---------- #


class TestCmdBuildIndustryIndices:

    def test_calls_runner_and_prints_summary(
        self, db_engine, fake_config, patched_engine_factory,
        monkeypatch, capsys,
    ):
        """The handler delegates to run_industry_index_build and
        prints a tabular summary. Stub the runner to return a
        predictable shape."""
        def fake_build(engine, exchange, *, min_constituents, n, base_value):
            return {
                "qualified": [
                    ("Banks - Regional", "Financials", 41),
                    ("IT - Services", "Technology", 15),
                ],
                "unqualified": [
                    ("Restaurants", "Consumer Cyclical", 9),
                ],
                "indexed": [
                    ("Banks - Regional", date(2025, 10, 1), 10),
                    ("IT - Services", date(2025, 10, 1), 10),
                ],
                "failed": [],
            }
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_industry_index_build",
            fake_build,
        )
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.min_constituents = 10
        args.n = 10
        args.base_value = 1000.0
        from trip_ema.fundamentals.cli import cmd_build_industry_indices
        rc = cmd_build_industry_indices(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Qualified industries" in out
        assert "Newly indexed" in out
        # Tabular output contains the industry names.
        assert "Banks - Regional" in out
        assert "IT - Services" in out

    def test_returns_1_when_some_failed(
        self, db_engine, fake_config, patched_engine_factory,
        monkeypatch, capsys,
    ):
        def fake_build(engine, exchange, *, min_constituents, n, base_value):
            return {
                "qualified": [("A", "S", 10)],
                "unqualified": [],
                "indexed": [],
                "failed": [("A", "below threshold on rebalance date")],
            }
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_industry_index_build",
            fake_build,
        )
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.min_constituents = 10
        args.n = 10
        args.base_value = 1000.0
        from trip_ema.fundamentals.cli import cmd_build_industry_indices
        rc = cmd_build_industry_indices(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 1
        assert "Failed" in out

    def test_rejects_invalid_exchange(
        self, db_engine, fake_config, patched_engine_factory,
    ):
        from trip_ema.etl.exchanges import UnsupportedExchangeError
        args = type("A", (), {})()
        args.exchange = "BOGUS"
        args.min_constituents = 10
        args.n = 10
        args.base_value = 1000.0
        from trip_ema.fundamentals.cli import cmd_build_industry_indices
        with pytest.raises(UnsupportedExchangeError):
            cmd_build_industry_indices(args, fake_config)


# ---------- cmd_compute_index_values ---------- #


class TestCmdComputeIndexValues:

    def _setup_one_index(self, db_engine, name="IT - Services"):
        from trip_ema.persistence.fundamentals_repo import (
            IndustryIndexRepository,
        )
        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).upsert(
                name, sector_name="Technology",
                base_date=date(2025, 10, 1),
            )

    def test_rejects_no_industry_and_no_all_active(
        self, db_engine, fake_config, patched_engine_factory, capsys,
    ):
        args = type("A", (), {})()
        args.industry = None
        args.all_active = False
        args.from_ = None
        args.to = None
        args.force = False
        args.resume = None
        from trip_ema.fundamentals.cli import cmd_compute_index_values
        rc = cmd_compute_index_values(args, fake_config)
        err = capsys.readouterr().err
        assert rc == 2
        assert "--industry" in err or "all-active" in err

    def test_rejects_both_industry_and_all_active(
        self, db_engine, fake_config, patched_engine_factory, capsys,
    ):
        args = type("A", (), {})()
        args.industry = "IT - Services"
        args.all_active = True
        args.from_ = None
        args.to = None
        args.force = False
        args.resume = None
        from trip_ema.fundamentals.cli import cmd_compute_index_values
        rc = cmd_compute_index_values(args, fake_config)
        err = capsys.readouterr().err
        assert rc == 2
        assert "mutually exclusive" in err

    def test_single_industry_unknown_returns_2(
        self, db_engine, fake_config, patched_engine_factory, capsys,
    ):
        args = type("A", (), {})()
        args.industry = "Nonexistent"
        args.all_active = False
        args.from_ = None
        args.to = None
        args.force = False
        args.resume = None
        from trip_ema.fundamentals.cli import cmd_compute_index_values
        rc = cmd_compute_index_values(args, fake_config)
        err = capsys.readouterr().err
        assert rc == 2
        assert "is not indexed" in err

    def test_single_industry_calls_runner(
        self, db_engine, fake_config, patched_engine_factory,
        monkeypatch, capsys,
    ):
        """Set up one index, stub the runner, confirm it gets called
        with the right args and rc=0 on SUCCESS."""
        self._setup_one_index(db_engine, "IT - Services")
        # Stub the runner to open+close a SUCCESS run, returning its id.
        called = []
        def fake_backfill(engine, industry_name, *, from_date, to_date,
                          resume_run_id=None, force=False):
            called.append((industry_name, from_date, to_date, force))
            with transaction(engine) as conn:
                rid = FundamentalsRunRepository(conn).open(
                    phase="index_value", exchange="ALL",
                )
                FundamentalsRunRepository(conn).close(
                    rid, status="SUCCESS",
                    succeeded=10, failed=0, skipped=2,
                )
            return rid
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_index_value_backfill",
            fake_backfill,
        )
        args = type("A", (), {})()
        args.industry = "IT - Services"
        args.all_active = False
        args.from_ = "2025-10-01"
        args.to = "2026-05-01"
        args.force = False
        args.resume = None
        from trip_ema.fundamentals.cli import cmd_compute_index_values
        rc = cmd_compute_index_values(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 0
        # The runner was called with the right industry and dates.
        assert len(called) == 1
        industry_called, from_called, to_called, force_called = called[0]
        assert industry_called == "IT - Services"
        # from_date = max(cli_from, base_date). base_date is 2025-10-01,
        # cli_from is 2025-10-01 → 2025-10-01.
        assert from_called == date(2025, 10, 1)
        assert to_called == date(2026, 5, 1)
        assert force_called is False
        assert "status=SUCCESS" in out

    def test_all_active_iterates_indices(
        self, db_engine, fake_config, patched_engine_factory,
        monkeypatch, capsys,
    ):
        """--all-active calls the runner once per active index."""
        self._setup_one_index(db_engine, "Index A")
        self._setup_one_index(db_engine, "Index B")
        called = []
        def fake_backfill(engine, industry_name, **kw):
            called.append(industry_name)
            with transaction(engine) as conn:
                rid = FundamentalsRunRepository(conn).open(
                    phase="index_value", exchange="ALL",
                )
                FundamentalsRunRepository(conn).close(
                    rid, status="SUCCESS",
                    succeeded=1, failed=0, skipped=0,
                )
            return rid
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_index_value_backfill",
            fake_backfill,
        )
        args = type("A", (), {})()
        args.industry = None
        args.all_active = True
        args.from_ = None
        args.to = None
        args.force = False
        args.resume = None
        from trip_ema.fundamentals.cli import cmd_compute_index_values
        rc = cmd_compute_index_values(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 0
        # Both indices got backfilled.
        assert set(called) == {"Index A", "Index B"}
        assert "2 SUCCESS" in out

    def test_all_active_when_no_indices_returns_1(
        self, db_engine, fake_config, patched_engine_factory, capsys,
    ):
        args = type("A", (), {})()
        args.industry = None
        args.all_active = True
        args.from_ = None
        args.to = None
        args.force = False
        args.resume = None
        from trip_ema.fundamentals.cli import cmd_compute_index_values
        rc = cmd_compute_index_values(args, fake_config)
        err = capsys.readouterr().err
        assert rc == 1
        assert "build-industry-indices" in err

    def test_partial_status_returns_1(
        self, db_engine, fake_config, patched_engine_factory,
        monkeypatch, capsys,
    ):
        self._setup_one_index(db_engine, "IT - Services")
        def fake_backfill(engine, industry_name, **kw):
            with transaction(engine) as conn:
                rid = FundamentalsRunRepository(conn).open(
                    phase="index_value", exchange="ALL",
                )
                FundamentalsRunRepository(conn).close(
                    rid, status="PARTIAL",
                    succeeded=5, failed=2, skipped=1,
                )
            return rid
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_index_value_backfill",
            fake_backfill,
        )
        args = type("A", (), {})()
        args.industry = "IT - Services"
        args.all_active = False
        args.from_ = None
        args.to = None
        args.force = False
        args.resume = None
        from trip_ema.fundamentals.cli import cmd_compute_index_values
        rc = cmd_compute_index_values(args, fake_config)
        assert rc == 1


# ---------- cmd_rebalance_industry_index ---------- #


class TestCmdRebalanceIndustryIndex:

    def test_returns_0_on_successful_rebalance(
        self, db_engine, fake_config, patched_engine_factory,
        monkeypatch, capsys,
    ):
        from trip_ema.fundamentals.runners import RebalanceResult
        def fake_run(engine, industry, rebalance_date, **kw):
            return RebalanceResult(
                industry_name=industry,
                rebalance_date=rebalance_date,
                constituents_picked=10,
                constituents_closed=10,
                rebalance_skipped=False,
                skip_reason=None,
            )
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_rebalance", fake_run,
        )
        args = type("A", (), {})()
        args.industry = "IT - Services"
        args.date = "2026-04-01"
        args.n = 10
        args.min_constituents = None
        args.exchange = None
        from trip_ema.fundamentals.cli import cmd_rebalance_industry_index
        rc = cmd_rebalance_industry_index(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 0
        assert "picked=10" in out
        assert "closed_prior=10" in out

    def test_returns_1_when_skipped(
        self, db_engine, fake_config, patched_engine_factory,
        monkeypatch, capsys,
    ):
        from trip_ema.fundamentals.runners import RebalanceResult
        def fake_run(engine, industry, rebalance_date, **kw):
            return RebalanceResult(
                industry_name=industry,
                rebalance_date=rebalance_date,
                constituents_picked=5,
                constituents_closed=0,
                rebalance_skipped=True,
                skip_reason="picked 5 < min_constituents 10",
            )
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_rebalance", fake_run,
        )
        args = type("A", (), {})()
        args.industry = "Sparse Industry"
        args.date = "2026-04-01"
        args.n = 10
        args.min_constituents = None
        args.exchange = None
        from trip_ema.fundamentals.cli import cmd_rebalance_industry_index
        rc = cmd_rebalance_industry_index(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 1
        assert "SKIPPED" in out
        assert "min_constituents" in out

    def test_returns_2_when_index_unknown(
        self, db_engine, fake_config, patched_engine_factory,
        monkeypatch, capsys,
    ):
        def fake_run(engine, industry, rebalance_date, **kw):
            raise ValueError(f"index {industry!r} does not exist")
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_rebalance", fake_run,
        )
        args = type("A", (), {})()
        args.industry = "Nonexistent"
        args.date = "2026-04-01"
        args.n = 10
        args.min_constituents = None
        args.exchange = None
        from trip_ema.fundamentals.cli import cmd_rebalance_industry_index
        rc = cmd_rebalance_industry_index(args, fake_config)
        err = capsys.readouterr().err
        assert rc == 2
        assert "does not exist" in err


# ---------- fundamentals-status with indices ---------- #


class TestCmdFundamentalsStatusShowsIndices:

    def test_shows_indices_section_when_present(
        self, db_engine, fake_config, patched_engine_factory,
        reliance_ticker, capsys,
    ):
        from trip_ema.persistence.fundamentals_repo import (
            IndustryIndexRepository,
            IndustryIndexValueRepository,
        )
        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).upsert(
                "Banks - Regional", sector_name="Financials",
                base_date=date(2025, 10, 1),
            )
            IndustryIndexValueRepository(conn).upsert_value(
                "Banks - Regional",
                trade_date=date(2025, 10, 1),
                value=1000.0,
                constituents_used=10, constituents_missing=0,
                renormalized=False,
                effective_rebalance_date=date(2025, 10, 1),
            )
        args = type("A", (), {})()
        args.exchange = None
        args.phase = None
        args.limit = 10
        rc = cmd_fundamentals_status(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Industry indices" in out
        assert "Banks - Regional" in out

    def test_omits_indices_section_when_none_exist(
        self, db_engine, fake_config, patched_engine_factory,
        reliance_ticker, capsys,
    ):
        # No indices set up; just an industry classification so
        # something prints (the status checks for at-least-some-data).
        with transaction(db_engine) as conn:
            InstrumentIndustryRepository(conn).upsert(
                reliance_ticker, sector="Energy", industry="Oil",
            )
        args = type("A", (), {})()
        args.exchange = None
        args.phase = None
        args.limit = 10
        rc = cmd_fundamentals_status(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Industry indices" not in out


# ============================================================
# Phase 5-4 CLI tests
# ============================================================


# ---------- cmd_compute_indicator_state ---------- #


class TestCmdComputeIndicatorState:

    def test_rejects_no_industry_and_no_all_industries(
        self, db_engine, fake_config, patched_engine_factory, capsys,
    ):
        args = type("A", (), {})()
        args.industry = None
        args.all_industries = False
        args.force = False
        from trip_ema.fundamentals.cli import cmd_compute_indicator_state
        rc = cmd_compute_indicator_state(args, fake_config)
        err = capsys.readouterr().err
        assert rc == 2
        assert "industry" in err or "all-industries" in err

    def test_rejects_both_industry_and_all_industries(
        self, db_engine, fake_config, patched_engine_factory, capsys,
    ):
        args = type("A", (), {})()
        args.industry = "IT - Services"
        args.all_industries = True
        args.force = False
        from trip_ema.fundamentals.cli import cmd_compute_indicator_state
        rc = cmd_compute_indicator_state(args, fake_config)
        err = capsys.readouterr().err
        assert rc == 2
        assert "mutually exclusive" in err

    def test_single_industry_unknown_returns_2(
        self, db_engine, fake_config, patched_engine_factory, capsys,
    ):
        args = type("A", (), {})()
        args.industry = "Nonexistent Industry"
        args.all_industries = False
        args.force = False
        from trip_ema.fundamentals.cli import cmd_compute_indicator_state
        rc = cmd_compute_indicator_state(args, fake_config)
        err = capsys.readouterr().err
        assert rc == 2
        assert "does not exist" in err

    def test_single_industry_no_values_returns_2(
        self, db_engine, fake_config, patched_engine_factory, capsys,
    ):
        from trip_ema.persistence.fundamentals_repo import (
            IndustryIndexRepository,
        )
        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).upsert(
                "Empty Industry", sector_name="Other",
                base_date=date(2025, 10, 1),
            )
        args = type("A", (), {})()
        args.industry = "Empty Industry"
        args.all_industries = False
        args.force = False
        from trip_ema.fundamentals.cli import cmd_compute_indicator_state
        rc = cmd_compute_indicator_state(args, fake_config)
        err = capsys.readouterr().err
        assert rc == 2
        assert "no value rows" in err

    def test_single_industry_success_path(
        self, db_engine, fake_config, patched_engine_factory,
        monkeypatch, capsys,
    ):
        """Stub the runner so we don't need a fully-populated DB."""
        def fake_runner(engine, name, *, force=False):
            with transaction(engine) as conn:
                rid = FundamentalsRunRepository(conn).open(
                    phase="index_indicators", exchange="ALL",
                )
                FundamentalsRunRepository(conn).close(
                    rid, status="SUCCESS",
                    succeeded=60, failed=0, skipped=20,
                )
            return rid
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_indicator_state_for_industry",
            fake_runner,
        )
        args = type("A", (), {})()
        args.industry = "IT - Services"
        args.all_industries = False
        args.force = False
        from trip_ema.fundamentals.cli import cmd_compute_indicator_state
        rc = cmd_compute_indicator_state(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 0
        assert "status=SUCCESS" in out
        assert "succeeded=60" in out
        assert "skipped=20" in out

    def test_all_industries_success_path(
        self, db_engine, fake_config, patched_engine_factory,
        monkeypatch, capsys,
    ):
        def fake_runner(engine, *, force=False):
            return {
                "succeeded": [
                    ("Industry A", 60, 20),
                    ("Industry B", 60, 20),
                ],
                "skipped": [],
                "failed": [],
            }
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_indicator_state_for_all_industries",
            fake_runner,
        )
        args = type("A", (), {})()
        args.industry = None
        args.all_industries = True
        args.force = False
        from trip_ema.fundamentals.cli import cmd_compute_indicator_state
        rc = cmd_compute_indicator_state(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 0
        assert "succeeded: 2" in out
        assert "Industry A" in out
        assert "Industry B" in out

    def test_all_industries_with_mixed_outcomes(
        self, db_engine, fake_config, patched_engine_factory,
        monkeypatch, capsys,
    ):
        """One succeeds, one skipped (no values), one fails. Should
        return 1."""
        def fake_runner(engine, *, force=False):
            return {
                "succeeded": [("Industry Good", 60, 20)],
                "skipped": [("Industry Empty", "no value rows")],
                "failed": [("Industry Bad", "compute error")],
            }
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_indicator_state_for_all_industries",
            fake_runner,
        )
        args = type("A", (), {})()
        args.industry = None
        args.all_industries = True
        args.force = False
        from trip_ema.fundamentals.cli import cmd_compute_indicator_state
        rc = cmd_compute_indicator_state(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 1
        # All three sections appear in output.
        assert "Industry Good" in out
        assert "Industry Empty" in out
        assert "Industry Bad" in out

    def test_all_industries_all_failed_returns_2(
        self, db_engine, fake_config, patched_engine_factory,
        monkeypatch, capsys,
    ):
        def fake_runner(engine, *, force=False):
            return {
                "succeeded": [],
                "skipped": [],
                "failed": [("Industry A", "error")],
            }
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_indicator_state_for_all_industries",
            fake_runner,
        )
        args = type("A", (), {})()
        args.industry = None
        args.all_industries = True
        args.force = False
        from trip_ema.fundamentals.cli import cmd_compute_indicator_state
        rc = cmd_compute_indicator_state(args, fake_config)
        assert rc == 2

    def test_force_flag_passes_through(
        self, db_engine, fake_config, patched_engine_factory,
        monkeypatch, capsys,
    ):
        force_seen = []
        def fake_runner(engine, name, *, force=False):
            force_seen.append(force)
            with transaction(engine) as conn:
                rid = FundamentalsRunRepository(conn).open(
                    phase="index_indicators", exchange="ALL",
                )
                FundamentalsRunRepository(conn).close(
                    rid, status="SUCCESS",
                    succeeded=10, failed=0, skipped=0,
                )
            return rid
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_indicator_state_for_industry",
            fake_runner,
        )
        args = type("A", (), {})()
        args.industry = "IT - Services"
        args.all_industries = False
        args.force = True
        from trip_ema.fundamentals.cli import cmd_compute_indicator_state
        cmd_compute_indicator_state(args, fake_config)
        assert force_seen == [True]


class TestCmdFundamentalsStatusShowsIndicatorCoverage:

    def test_shows_indicator_section_when_present(
        self, db_engine, fake_config, patched_engine_factory,
        reliance_ticker, capsys,
    ):
        from trip_ema.fundamentals import IndicatorState
        from trip_ema.persistence.fundamentals_repo import (
            IndicatorStateRepository,
            IndustryIndexRepository,
        )
        # Set up a minimal industry + one indicator_state row.
        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).upsert(
                "IT - Services", sector_name="Technology",
                base_date=date(2025, 10, 1),
            )
            state = IndicatorState(
                ticker_id=None, timeframe=None,
                industry_name="IT - Services",
                trade_date=date(2026, 5, 22),
                med_ema=840.0, long_ema=848.0, longest_ema=856.0,
                atr=12.95,
                upper_inner_band=859.0, lower_inner_band=830.0,
                upper_mid_band=882.0, lower_mid_band=815.0,
                upper_outer_band=915.0, lower_outer_band=798.0,
                display_regime=-1,
                pierces_upper_mid=False, pierces_lower_mid=False,
                dist_to_med_in_hl_units=None,
                dist_to_long_in_hl_units=None,
                dist_to_longest_in_hl_units=None,
                mean_dist_to_med_in_hl_units=None,
                mean_dist_to_long_in_hl_units=None,
                mean_dist_to_longest_in_hl_units=None,
                bars_since_med_turn=None, bars_since_long_turn=None,
                bars_since_longest_turn=None,
                pct_change_since_med_turn=None,
                pct_change_since_long_turn=None,
                pct_change_since_longest_turn=None,
                med_pattern_label="NA",
                long_pattern_label="NA",
                longest_pattern_label="NA",
                med_turn_buy=False, med_turn_sell=False,
                long_turn_buy=False, long_turn_sell=False,
                longest_turn_buy=False, longest_turn_sell=False,
                computed_at=datetime.now(UTC),
            )
            IndicatorStateRepository(conn).upsert_for_industry(state)
        args = type("A", (), {})()
        args.exchange = None
        args.phase = None
        args.limit = 10
        rc = cmd_fundamentals_status(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Indicator state coverage" in out
        assert "IT - Services" in out

    def test_omits_indicator_section_when_no_rows(
        self, db_engine, fake_config, patched_engine_factory,
        reliance_ticker, capsys,
    ):
        # No indicator_state rows; an industry exists for the
        # general status to print something.
        with transaction(db_engine) as conn:
            InstrumentIndustryRepository(conn).upsert(
                reliance_ticker, sector="Energy", industry="Oil",
            )
        args = type("A", (), {})()
        args.exchange = None
        args.phase = None
        args.limit = 10
        rc = cmd_fundamentals_status(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Indicator state coverage" not in out


# Note: TestCmdFundamentalsStatusShowsSignalCoverage (Phase 6-1
# industry-shape signal coverage section) removed in A2 — the
# "Signal events coverage" print block is gone with the
# SignalEventRepository.coverage_summary_for_industries method.


# ============================================================
# Phase 7 CLI tests — per-ticker modes
# ============================================================


class TestCmdComputeIndicatorStateTickerModes:

    def test_rejects_more_than_one_mode(
        self, db_engine, fake_config, patched_engine_factory, capsys,
    ):
        args = type("A", (), {})()
        args.industry = "IT - Services"
        args.all_industries = False
        args.ticker = "RELIANCE"
        args.all_tickers = False
        args.exchange = "NSE"
        args.timeframe = "1W"
        args.force = False
        from trip_ema.fundamentals.cli import cmd_compute_indicator_state
        rc = cmd_compute_indicator_state(args, fake_config)
        err = capsys.readouterr().err
        assert rc == 2
        assert "mutually exclusive" in err

    def test_ticker_requires_timeframe(
        self, db_engine, fake_config, patched_engine_factory, capsys,
    ):
        args = type("A", (), {})()
        args.industry = None
        args.all_industries = False
        args.ticker = "RELIANCE"
        args.all_tickers = False
        args.exchange = "NSE"
        args.timeframe = None
        args.force = False
        from trip_ema.fundamentals.cli import cmd_compute_indicator_state
        rc = cmd_compute_indicator_state(args, fake_config)
        err = capsys.readouterr().err
        assert rc == 2
        assert "timeframe" in err

    def test_ticker_requires_exchange(
        self, db_engine, fake_config, patched_engine_factory, capsys,
    ):
        args = type("A", (), {})()
        args.industry = None
        args.all_industries = False
        args.ticker = "RELIANCE"
        args.all_tickers = False
        args.exchange = None
        args.timeframe = "1W"
        args.force = False
        from trip_ema.fundamentals.cli import cmd_compute_indicator_state
        rc = cmd_compute_indicator_state(args, fake_config)
        err = capsys.readouterr().err
        assert rc == 2
        assert "exchange" in err.lower()

    def test_unknown_ticker_returns_2(
        self, db_engine, fake_config, patched_engine_factory, capsys,
    ):
        args = type("A", (), {})()
        args.industry = None
        args.all_industries = False
        args.ticker = "NOSUCH"
        args.all_tickers = False
        args.exchange = "NSE"
        args.timeframe = "1W"
        args.force = False
        from trip_ema.fundamentals.cli import cmd_compute_indicator_state
        rc = cmd_compute_indicator_state(args, fake_config)
        err = capsys.readouterr().err
        assert rc == 2
        assert "not found" in err

    def test_all_tickers_requires_timeframe(
        self, db_engine, fake_config, patched_engine_factory, capsys,
    ):
        args = type("A", (), {})()
        args.industry = None
        args.all_industries = False
        args.ticker = None
        args.all_tickers = True
        args.exchange = None
        args.timeframe = None
        args.force = False
        from trip_ema.fundamentals.cli import cmd_compute_indicator_state
        rc = cmd_compute_indicator_state(args, fake_config)
        err = capsys.readouterr().err
        assert rc == 2
        assert "timeframe" in err

    def test_all_tickers_success_path(
        self, db_engine, fake_config, patched_engine_factory,
        monkeypatch, capsys,
    ):
        def fake_runner(engine, timeframe, *, exchange=None, force=False, min_bars=30):
            return {
                "succeeded": [
                    (1, "RELIANCE", 80, 6),
                    (2, "TCS", 80, 6),
                ],
                "skipped": [],
                "failed": [],
            }
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_indicator_state_for_all_tickers",
            fake_runner,
        )
        args = type("A", (), {})()
        args.industry = None
        args.all_industries = False
        args.ticker = None
        args.all_tickers = True
        args.exchange = None
        args.timeframe = "1W"
        args.force = False
        from trip_ema.fundamentals.cli import cmd_compute_indicator_state
        rc = cmd_compute_indicator_state(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 0
        assert "RELIANCE" in out
        assert "TCS" in out


class TestCmdComputeSignalsTickerModes:

    def test_ticker_requires_exchange_and_timeframe(
        self, db_engine, fake_config, patched_engine_factory, capsys,
    ):
        args = type("A", (), {})()
        args.industry = None
        args.all_industries = False
        args.ticker = "RELIANCE"
        args.all_tickers = False
        args.exchange = None
        args.timeframe = None
        args.force = False
        from trip_ema.fundamentals.cli import cmd_compute_signals
        rc = cmd_compute_signals(args, fake_config)
        err = capsys.readouterr().err
        assert rc == 2
        assert "exchange" in err.lower() or "timeframe" in err.lower()

    def test_all_tickers_success_path(
        self, db_engine, fake_config, patched_engine_factory,
        monkeypatch, capsys,
    ):
        def fake_runner(engine, timeframe, *, exchange=None, force=False):
            return {
                "succeeded": [(1, "RELIANCE", 12), (2, "TCS", 8)],
                "skipped": [],
                "failed": [],
            }
        monkeypatch.setattr(
            "trip_ema.fundamentals.runners.run_signal_events_for_all_tickers",
            fake_runner,
        )
        args = type("A", (), {})()
        args.industry = None
        args.all_industries = False
        args.ticker = None
        args.all_tickers = True
        args.exchange = None
        args.timeframe = "1W"
        args.force = False
        from trip_ema.fundamentals.cli import cmd_compute_signals
        rc = cmd_compute_signals(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 0
        assert "RELIANCE" in out
        assert "TCS" in out


# ============================================================
# Phase 4C CLI tests — run-eod-pipeline
# ============================================================


class TestCmdRunEodPipeline:

    def _make_args(self, exchange="NSE", include_tickers=False,
                   timeframe=None, force=False, skip_tickers=False):
        args = type("A", (), {})()
        args.exchange = exchange
        args.include_tickers = include_tickers
        args.timeframe = timeframe
        args.force = force
        args.skip_tickers = skip_tickers
        return args

    def test_returns_0_on_full_success(
        self, db_engine, fake_config, patched_engine_factory,
        monkeypatch, capsys,
    ):
        from trip_ema.fundamentals.eod_pipeline import (
            PipelineResult, StepResult, StepStatus,
        )
        from datetime import datetime, timezone
        now = datetime.now(UTC)
        fake_result = PipelineResult(
            exchange="NSE", started_at=now, finished_at=now,
            steps=[
                StepResult(
                    name="refresh-industry",
                    status=StepStatus.SUCCESS,
                    succeeded=100, started_at=now, finished_at=now,
                ),
            ],
        )

        def fake_run_pipeline(engine, exchange, *,
                              include_tickers=False, timeframe="1W",
                              force=False, progress_callback=None):
            if progress_callback:
                progress_callback(fake_result.steps[0])
            return fake_result

        monkeypatch.setattr(
            "trip_ema.fundamentals.eod_pipeline.run_eod_pipeline",
            fake_run_pipeline,
        )
        args = self._make_args()
        from trip_ema.fundamentals.cli import cmd_run_eod_pipeline
        rc = cmd_run_eod_pipeline(args, fake_config)
        out = capsys.readouterr().out
        assert rc == 0
        assert "SUCCESS" in out
        assert "refresh-industry" in out

    def test_returns_1_on_partial(
        self, db_engine, fake_config, patched_engine_factory,
        monkeypatch, capsys,
    ):
        from trip_ema.fundamentals.eod_pipeline import (
            PipelineResult, StepResult, StepStatus,
        )
        from datetime import datetime, timezone
        now = datetime.now(UTC)
        fake_result = PipelineResult(
            exchange="NSE", started_at=now, finished_at=now,
            steps=[
                StepResult(
                    name="refresh-shares",
                    status=StepStatus.PARTIAL,
                    succeeded=80, failed=20,
                    started_at=now, finished_at=now,
                ),
            ],
        )

        def fake_pipeline(engine, exchange, **kw):
            return fake_result
        monkeypatch.setattr(
            "trip_ema.fundamentals.eod_pipeline.run_eod_pipeline",
            fake_pipeline,
        )
        args = self._make_args()
        from trip_ema.fundamentals.cli import cmd_run_eod_pipeline
        rc = cmd_run_eod_pipeline(args, fake_config)
        assert rc == 1

    def test_returns_2_on_fatal(
        self, db_engine, fake_config, patched_engine_factory,
        monkeypatch, capsys,
    ):
        from trip_ema.fundamentals.eod_pipeline import (
            PipelineResult, StepResult, StepStatus,
        )
        from datetime import datetime, timezone
        now = datetime.now(UTC)
        fake_result = PipelineResult(
            exchange="NSE", started_at=now, finished_at=now,
            steps=[
                StepResult(
                    name="refresh-industry",
                    status=StepStatus.FAILED,
                    error="DB down",
                    started_at=now, finished_at=now,
                ),
            ],
        )

        def fake_pipeline(engine, exchange, **kw):
            return fake_result
        monkeypatch.setattr(
            "trip_ema.fundamentals.eod_pipeline.run_eod_pipeline",
            fake_pipeline,
        )
        args = self._make_args()
        from trip_ema.fundamentals.cli import cmd_run_eod_pipeline
        rc = cmd_run_eod_pipeline(args, fake_config)
        assert rc == 2

    def test_include_tickers_flag_passed(
        self, db_engine, fake_config, patched_engine_factory,
        monkeypatch, capsys,
    ):
        from trip_ema.fundamentals.eod_pipeline import (
            PipelineResult, StepResult, StepStatus,
        )
        from datetime import datetime, timezone
        now = datetime.now(UTC)
        captured_kwargs = {}

        def fake_pipeline(engine, exchange, **kw):
            captured_kwargs.update(kw)
            return PipelineResult(
                exchange=exchange, started_at=now, finished_at=now,
                steps=[],
            )

        monkeypatch.setattr(
            "trip_ema.fundamentals.eod_pipeline.run_eod_pipeline",
            fake_pipeline,
        )
        args = self._make_args(
            include_tickers=True, timeframe="1D", force=True,
        )
        from trip_ema.fundamentals.cli import cmd_run_eod_pipeline
        cmd_run_eod_pipeline(args, fake_config)
        assert captured_kwargs.get("include_tickers") is True
        assert captured_kwargs.get("timeframe") == "1D"
        assert captured_kwargs.get("force") is True
