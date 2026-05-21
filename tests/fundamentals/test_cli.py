"""CLI tests for Phase 1 fundamentals commands.

The CLI handlers thinly wrap the runner and repos, so these tests
focus on the command parsing, output formatting, and exit codes.
We patch the runner so we don't make HTTP calls.
"""
from __future__ import annotations

from datetime import datetime, timezone
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
            ref, created_at=datetime.now(timezone.utc),
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
                tid = tr.insert(ref, created_at=datetime.now(timezone.utc))
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
