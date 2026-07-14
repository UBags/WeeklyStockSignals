"""CLI tests for Phase 1 fundamentals commands.

The CLI handlers thinly wrap the runner and repos, so these tests focus on the
command parsing, output formatting, and exit codes.

Handlers take an AppContext and their runner as a keyword-only argument, so a
test hands them a Spy rather than rewriting a module attribute. The one
surviving monkeypatch is a test OF the dispatch table, where the module
attribute is the thing under test.
"""
from __future__ import annotations

from datetime import date, datetime, timezone, UTC
from io import StringIO

import pytest

from trip_ema.app_context import AppContext
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


class Spy:
    """A recording stand-in for a runner. Replaces `unittest.mock.patch` of the
    runner's module path: the command takes the runner as a keyword argument
    now, so a test hands it one instead of rewriting a module attribute."""

    def __init__(self, return_value=None):
        self.return_value = return_value
        self.calls: list[tuple] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if isinstance(self.return_value, BaseException):
            raise self.return_value
        return self.return_value

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def call_args(self):
        return self.calls[-1] if self.calls else None


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
def ctx(fake_config, db_engine):
    """The command's collaborators, handed to it.

    This replaces patching create_engine_from_config AND patching the engine's
    own `dispose` — the latter was needed because every command disposed the
    engine it built, which would have torn down the fixture engine mid-test.
    Commands no longer own the engine, so neither problem exists."""
    return AppContext(config=fake_config, db_engine=db_engine)


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
        self, db_engine, fake_config, ctx, reliance_ticker, capsys,
    ):
        mock_runner = Spy()
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
        rc = cmd_refresh_industry(args, ctx, refresh_fn=mock_runner)
        assert rc == 0
        out = capsys.readouterr().out
        assert "status=SUCCESS" in out
        assert "succeeded=1" in out

    def test_returns_1_on_partial(
        self, db_engine, fake_config, ctx, reliance_ticker,
    ):
        mock_runner = Spy()
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
        rc = cmd_refresh_industry(args, ctx, refresh_fn=mock_runner)
        assert rc == 1

    def test_returns_2_on_failed(
        self, db_engine, fake_config, ctx, reliance_ticker,
    ):
        mock_runner = Spy()
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
        rc = cmd_refresh_industry(args, ctx, refresh_fn=mock_runner)
        assert rc == 2


# ---------- cmd_set_industry_override ---------- #


class TestCmdSetIndustryOverride:

    def test_writes_override_and_classification(
        self, db_engine, fake_config, ctx, reliance_ticker, capsys,
    ):
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.symbol = "RELIANCE"
        args.industry = "Hand-set Industry"
        args.sector = "Energy"
        args.note = "test"
        args.set_by = "test-runner"
        rc = cmd_set_industry_override(args, ctx)
        assert rc == 0
        with db_engine.connect() as conn:
            ov = IndustryOverrideRepository(conn).get(reliance_ticker)
            ind = InstrumentIndustryRepository(conn).get(reliance_ticker)
        assert ov.industry == "Hand-set Industry"
        assert ind.industry == "Hand-set Industry"
        assert ind.classification_source == "MANUAL_OVERRIDE"

    def test_sector_defaults_to_unclassified(
        self, db_engine, fake_config, ctx, reliance_ticker,
    ):
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.symbol = "RELIANCE"
        args.industry = "X"
        args.sector = None
        args.note = None
        args.set_by = None
        rc = cmd_set_industry_override(args, ctx)
        assert rc == 0
        with db_engine.connect() as conn:
            ov = IndustryOverrideRepository(conn).get(reliance_ticker)
        assert ov.sector == "Unclassified"

    def test_returns_2_for_unknown_symbol(
        self, db_engine, fake_config, ctx, capsys,
    ):
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.symbol = "DOES_NOT_EXIST"
        args.industry = "X"
        args.sector = "Y"
        args.note = None
        args.set_by = None
        rc = cmd_set_industry_override(args, ctx)
        assert rc == 2
        assert "not found" in capsys.readouterr().err


# ---------- cmd_delete_industry_override ---------- #


class TestCmdDeleteIndustryOverride:

    def test_removes_existing_override(
        self, db_engine, fake_config, ctx, reliance_ticker,
    ):
        with transaction(db_engine) as conn:
            IndustryOverrideRepository(conn).upsert(
                reliance_ticker, sector="X", industry="Y",
            )
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.symbol = "RELIANCE"
        rc = cmd_delete_industry_override(args, ctx)
        assert rc == 0
        with db_engine.connect() as conn:
            assert IndustryOverrideRepository(conn).get(reliance_ticker) is None

    def test_returns_1_when_no_override(
        self, db_engine, fake_config, ctx, reliance_ticker,
    ):
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.symbol = "RELIANCE"
        rc = cmd_delete_industry_override(args, ctx)
        assert rc == 1


# ---------- cmd_fundamentals_status ---------- #


class TestCmdFundamentalsStatus:

    def test_prints_coverage_and_recent_runs(
        self, db_engine, fake_config, ctx,
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
        rc = cmd_fundamentals_status(args, ctx)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Industry classification coverage" in out
        assert "NSE" in out
        # Coverage line: 1 total, 1 classified, 100.0%
        assert "100.0%" in out
        assert "Recent fundamentals_run rows" in out
        assert "industry" in out

    def test_empty_db_doesnt_crash(
        self, db_engine, fake_config, ctx, capsys,
    ):
        args = type("A", (), {})()
        args.exchange = None
        args.phase = None
        args.limit = 10
        rc = cmd_fundamentals_status(args, ctx)
        out = capsys.readouterr().out
        assert rc == 0
        # Either "No active tickers" or "No fundamentals_run rows" — both fine.
        assert "No" in out


# ---------- end-to-end via main() ---------- #


def test_main_dispatches_refresh_industry():
    """Static wire-up test: the ETL CLI's parser resolves the
    `refresh-industry` sub-command to `cmd_refresh_industry` from
    trip_ema.fundamentals.cli.

    Previously this test invoked etl_cli.main() with a real config
    path and monkeypatched cmd_refresh_industry to intercept the call.
    Both cost were symptomatic of the same problem: main() was the
    only reachable dispatch surface, so a test HAD to run it. It
    doesn't — _build_parser() stashes the fundamentals handler map
    on the parser object as `_fundamentals_handlers`, and that's the
    exact resolution main() performs. Asserting on it directly needs
    no config file, no monkeypatch, and no wall-clock time.

    Phase 9 of the de-monkeypatching programme — matches the pattern
    of every other seam closed by giving the thing under test a
    handle you can inspect without breaking the encapsulation.
    """
    from trip_ema.etl import cli as etl_cli
    from trip_ema.fundamentals.cli import cmd_refresh_industry

    parser = etl_cli._build_parser()
    handlers = getattr(parser, "_fundamentals_handlers", None)
    assert handlers is not None, (
        "_build_parser must stash the fundamentals handler map on "
        "the parser; if this attribute is gone, main()'s dispatch "
        "is broken."
    )
    assert handlers.get("refresh-industry") is cmd_refresh_industry, (
        "parser wire-up for 'refresh-industry' does not resolve to "
        "cmd_refresh_industry — a rename or a missing entry in "
        "fundamentals.cli.register_subparsers."
    )


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
        self, db_engine, fake_config, ctx, etf_universe,
        capsys,
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
        _classify_fn = stub_classify_etf

        args = type("A", (), {})()
        args.exchange = "NSE"
        rc = cmd_backfill_etf_classification(args, ctx, classify_fn=_classify_fn)
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
        self, db_engine, fake_config, ctx, capsys,
    ):
        """No ETF tickers in DB → command exits cleanly with rc=0."""
        args = type("A", (), {})()
        args.exchange = "NSE"
        rc = cmd_backfill_etf_classification(args, ctx)
        assert rc == 0
        assert "Nothing to do" in capsys.readouterr().out

    def test_backfill_writes_fundamentals_run_row(
        self, db_engine, fake_config, ctx, etf_universe,
    ):
        """An audit row should exist in fundamentals_run after the
        backfill completes."""
        def stub_classify_etf(engine, tid, symbol, exchange, fetch_fn):
            with transaction(engine) as conn:
                InstrumentIndustryRepository(conn).upsert_etf(
                    tid, etf_long_name=None,
                )
            return "succeeded"
        _classify_fn = stub_classify_etf

        args = type("A", (), {})()
        args.exchange = "NSE"
        cmd_backfill_etf_classification(args, ctx, classify_fn=_classify_fn)
        with db_engine.connect() as conn:
            runs = FundamentalsRunRepository(conn).list_recent(
                phase="industry", exchange="NSE", limit=5,
            )
        assert len(runs) >= 1
        latest = runs[0]
        assert latest.status == "SUCCESS"
        assert latest.succeeded == 2  # GOLDBEES + ITBEES

    def test_backfill_with_failures_returns_partial(
        self, db_engine, fake_config, ctx, etf_universe,
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
        _classify_fn = stub_classify_etf

        args = type("A", (), {})()
        args.exchange = "NSE"
        rc = cmd_backfill_etf_classification(args, ctx, classify_fn=_classify_fn)
        assert rc == 1  # PARTIAL


# ---------- cmd_refresh_shares ---------- #


class TestCmdRefreshShares:

    def test_returns_0_on_success(
        self, db_engine, fake_config, ctx, reliance_ticker,
        capsys,
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
        _refresh_fn = lambda *a, **kw: rid

        args = type("A", (), {})()
        args.exchange = "NSE"
        args.force = False
        args.resume = None
        rc = cmd_refresh_shares(args, ctx, refresh_fn=_refresh_fn)
        assert rc == 0
        out = capsys.readouterr().out
        assert "status=SUCCESS" in out
        assert "succeeded=1" in out

    def test_returns_1_on_partial(
        self, db_engine, fake_config, ctx, reliance_ticker,
    ):
        from trip_ema.fundamentals.cli import cmd_refresh_shares
        with transaction(db_engine) as conn:
            rid = FundamentalsRunRepository(conn).open(
                phase="shares", exchange="NSE",
            )
            FundamentalsRunRepository(conn).close(
                rid, status="PARTIAL", succeeded=3, failed=2, skipped=0,
            )
        _refresh_fn = lambda *a, **kw: rid
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.force = False
        args.resume = None
        rc = cmd_refresh_shares(args, ctx, refresh_fn=_refresh_fn)
        assert rc == 1

    def test_returns_2_on_failed(
        self, db_engine, fake_config, ctx, reliance_ticker,
    ):
        from trip_ema.fundamentals.cli import cmd_refresh_shares
        with transaction(db_engine) as conn:
            rid = FundamentalsRunRepository(conn).open(
                phase="shares", exchange="NSE",
            )
            FundamentalsRunRepository(conn).close(
                rid, status="FAILED", succeeded=0, failed=5, skipped=0,
            )
        _refresh_fn = lambda *a, **kw: rid
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.force = False
        args.resume = None
        rc = cmd_refresh_shares(args, ctx, refresh_fn=_refresh_fn)
        assert rc == 2


class TestCmdFundamentalsStatusShowsShares:
    """The status command should report shares coverage alongside
    industry coverage when shares data exists."""

    def test_shows_shares_coverage_when_present(
        self, db_engine, fake_config, ctx,
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
        rc = cmd_fundamentals_status(args, ctx)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Shares-outstanding coverage" in out
        assert "WITH_SHARES" in out

    def test_omits_shares_section_when_no_data(
        self, db_engine, fake_config, ctx,
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
        rc = cmd_fundamentals_status(args, ctx)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Shares-outstanding coverage" not in out


# ---------- cmd_refresh_financials ---------- #


class TestCmdRefreshFinancials:

    def test_returns_0_on_success(
        self, db_engine, fake_config, ctx, reliance_ticker,
        capsys,
    ):
        from trip_ema.fundamentals.cli import cmd_refresh_financials
        with transaction(db_engine) as conn:
            rid = FundamentalsRunRepository(conn).open(
                phase="financials", exchange="NSE",
            )
            FundamentalsRunRepository(conn).close(
                rid, status="SUCCESS", succeeded=1, failed=0, skipped=0,
            )
        _refresh_fn = lambda *a, **kw: rid
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.force = False
        args.resume = None
        rc = cmd_refresh_financials(args, ctx, refresh_fn=_refresh_fn)
        assert rc == 0
        out = capsys.readouterr().out
        assert "status=SUCCESS" in out

    def test_returns_1_on_partial(
        self, db_engine, fake_config, ctx, reliance_ticker,
    ):
        from trip_ema.fundamentals.cli import cmd_refresh_financials
        with transaction(db_engine) as conn:
            rid = FundamentalsRunRepository(conn).open(
                phase="financials", exchange="NSE",
            )
            FundamentalsRunRepository(conn).close(
                rid, status="PARTIAL", succeeded=3, failed=2, skipped=0,
            )
        _refresh_fn = lambda *a, **kw: rid
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.force = False
        args.resume = None
        rc = cmd_refresh_financials(args, ctx, refresh_fn=_refresh_fn)
        assert rc == 1


class TestCmdFundamentalsStatusShowsFinancials:

    def test_shows_financials_coverage_when_present(
        self, db_engine, fake_config, ctx,
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
        rc = cmd_fundamentals_status(args, ctx)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Quarterly financials coverage" in out
        assert "W/REVENUE" in out
        assert "W/EBITDA" in out

    def test_omits_financials_section_when_no_data(
        self, db_engine, fake_config, ctx,
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
        rc = cmd_fundamentals_status(args, ctx)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Quarterly financials coverage" not in out


# ---------- cmd_compute_market_cap ---------- #


class TestCmdComputeMarketCap:

    def test_single_day_mode_writes_rows_and_returns_0(
        self, db_engine, fake_config, ctx,
        reliance_ticker, capsys,
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
        _compute_fn = stub_compute

        args = type("A", (), {})()
        args.exchange = "NSE"
        args.date = "2026-05-22"
        args.backfill_from = None
        args.backfill_to = None
        args.force = False
        args.resume = None
        rc = cmd_compute_market_cap(args, ctx, compute_fn=_compute_fn)
        assert rc == 0
        out = capsys.readouterr().out
        assert "computed=1" in out

    def test_single_day_mode_returns_1_when_nothing_written(
        self, db_engine, fake_config, ctx,
        reliance_ticker, capsys,
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
        _compute_fn = stub_compute

        args = type("A", (), {})()
        args.exchange = "NSE"
        args.date = "2026-05-22"
        args.backfill_from = None
        args.backfill_to = None
        args.force = False
        args.resume = None
        rc = cmd_compute_market_cap(args, ctx, compute_fn=_compute_fn)
        assert rc == 1

    def test_backfill_mode_routes_to_runner(
        self, db_engine, fake_config, ctx,
        reliance_ticker, capsys,
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
        _backfill_fn = lambda *a, **kw: rid
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.date = None
        args.backfill_from = "2025-05-01"
        args.backfill_to = "2026-05-01"
        args.force = False
        args.resume = None
        rc = cmd_compute_market_cap(args, ctx, backfill_fn=_backfill_fn)
        assert rc == 0
        out = capsys.readouterr().out
        assert "status=SUCCESS" in out


class TestCmdFundamentalsStatusShowsMarketCap:

    def test_shows_mcap_section_when_present(
        self, db_engine, fake_config, ctx,
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
        rc = cmd_fundamentals_status(args, ctx)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Daily market cap coverage" in out
        assert "COMPUTED" in out

    def test_omits_mcap_section_when_no_data(
        self, db_engine, fake_config, ctx,
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
        rc = cmd_fundamentals_status(args, ctx)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Daily market cap coverage" not in out


# ============================================================
# Phase 4B-3 CLI tests
# ============================================================


# ---------- cmd_build_industry_indices ---------- #


class TestCmdBuildIndustryIndices:

    def test_calls_runner_and_prints_summary(
        self, db_engine, fake_config, ctx,
        capsys,
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
        _build_fn = fake_build
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.min_constituents = 10
        args.n = 10
        args.base_value = 1000.0
        from trip_ema.fundamentals.cli import cmd_build_industry_indices
        rc = cmd_build_industry_indices(args, ctx, build_fn=_build_fn)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Qualified industries" in out
        assert "Newly indexed" in out
        # Tabular output contains the industry names.
        assert "Banks - Regional" in out
        assert "IT - Services" in out

    def test_returns_1_when_some_failed(
        self, db_engine, fake_config, ctx,
        capsys,
    ):
        def fake_build(engine, exchange, *, min_constituents, n, base_value):
            return {
                "qualified": [("A", "S", 10)],
                "unqualified": [],
                "indexed": [],
                "failed": [("A", "below threshold on rebalance date")],
            }
        _build_fn = fake_build
        args = type("A", (), {})()
        args.exchange = "NSE"
        args.min_constituents = 10
        args.n = 10
        args.base_value = 1000.0
        from trip_ema.fundamentals.cli import cmd_build_industry_indices
        rc = cmd_build_industry_indices(args, ctx, build_fn=_build_fn)
        out = capsys.readouterr().out
        assert rc == 1
        assert "Failed" in out

    def test_rejects_invalid_exchange(
        self, db_engine, fake_config, ctx,
    ):
        from trip_ema.etl.exchanges import UnsupportedExchangeError
        args = type("A", (), {})()
        args.exchange = "BOGUS"
        args.min_constituents = 10
        args.n = 10
        args.base_value = 1000.0
        from trip_ema.fundamentals.cli import cmd_build_industry_indices
        with pytest.raises(UnsupportedExchangeError):
            cmd_build_industry_indices(args, ctx)


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
        self, db_engine, fake_config, ctx, capsys,
    ):
        args = type("A", (), {})()
        args.industry = None
        args.all_active = False
        args.from_ = None
        args.to = None
        args.force = False
        args.resume = None
        from trip_ema.fundamentals.cli import cmd_compute_index_values
        rc = cmd_compute_index_values(args, ctx)
        err = capsys.readouterr().err
        assert rc == 2
        assert "--industry" in err or "all-active" in err

    def test_rejects_both_industry_and_all_active(
        self, db_engine, fake_config, ctx, capsys,
    ):
        args = type("A", (), {})()
        args.industry = "IT - Services"
        args.all_active = True
        args.from_ = None
        args.to = None
        args.force = False
        args.resume = None
        from trip_ema.fundamentals.cli import cmd_compute_index_values
        rc = cmd_compute_index_values(args, ctx)
        err = capsys.readouterr().err
        assert rc == 2
        assert "mutually exclusive" in err

    def test_single_industry_unknown_returns_2(
        self, db_engine, fake_config, ctx, capsys,
    ):
        args = type("A", (), {})()
        args.industry = "Nonexistent"
        args.all_active = False
        args.from_ = None
        args.to = None
        args.force = False
        args.resume = None
        from trip_ema.fundamentals.cli import cmd_compute_index_values
        rc = cmd_compute_index_values(args, ctx)
        err = capsys.readouterr().err
        assert rc == 2
        assert "is not indexed" in err

    def test_single_industry_calls_runner(
        self, db_engine, fake_config, ctx,
        capsys,
    ):
        """Set up one index, stub the runner, confirm it gets called
        with the right args and rc=0 on SUCCESS."""
        self._setup_one_index(db_engine, "IT - Services")
        # Stub the runner to open+close a SUCCESS run, returning its id.
        called = []
        def fake_backfill(engine, industry_name, *, from_date, to_date,
                          resume_run_id=None):
            called.append((industry_name, from_date, to_date))
            with transaction(engine) as conn:
                rid = FundamentalsRunRepository(conn).open(
                    phase="index_value", exchange="ALL",
                )
                FundamentalsRunRepository(conn).close(
                    rid, status="SUCCESS",
                    succeeded=10, failed=0, skipped=2,
                )
            return rid
        _populate_fn = fake_backfill
        args = type("A", (), {})()
        args.industry = "IT - Services"
        args.all_active = False
        args.from_ = "2025-10-01"
        args.to = "2026-05-01"
        args.force = False
        args.resume = None
        from trip_ema.fundamentals.cli import cmd_compute_index_values
        rc = cmd_compute_index_values(args, ctx, populate_fn=_populate_fn)
        out = capsys.readouterr().out
        assert rc == 0
        # The runner was called with the right industry and dates.
        assert len(called) == 1
        industry_called, from_called, to_called = called[0]
        assert industry_called == "IT - Services"
        # from_date = max(cli_from, base_date). base_date is 2025-10-01,
        # cli_from is 2025-10-01 → 2025-10-01.
        assert from_called == date(2025, 10, 1)
        assert to_called == date(2026, 5, 1)
        assert "status=SUCCESS" in out

    def test_all_active_iterates_indices(
        self, db_engine, fake_config, ctx,
        capsys, reliance_ticker,
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
        _populate_fn = fake_backfill
        args = type("A", (), {})()
        args.industry = None
        args.all_active = True
        args.from_ = None
        args.to = None
        args.force = False
        args.resume = None
        from trip_ema.fundamentals.cli import cmd_compute_index_values
        rc = cmd_compute_index_values(args, ctx, populate_fn=_populate_fn)
        out = capsys.readouterr().out
        assert rc == 0
        # Both indices got backfilled.
        assert set(called) == {"Index A", "Index B"}
        assert "2 SUCCESS" in out

    def test_all_active_when_no_indices_returns_1(
        self, db_engine, fake_config, ctx, capsys,
    ):
        args = type("A", (), {})()
        args.industry = None
        args.all_active = True
        args.from_ = None
        args.to = None
        args.force = False
        args.resume = None
        from trip_ema.fundamentals.cli import cmd_compute_index_values
        rc = cmd_compute_index_values(args, ctx)
        err = capsys.readouterr().err
        assert rc == 1
        assert "build-industry-indices" in err

    def test_partial_status_returns_1(
        self, db_engine, fake_config, ctx,
        capsys, reliance_ticker,
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
        _populate_fn = fake_backfill
        args = type("A", (), {})()
        args.industry = "IT - Services"
        args.all_active = False
        args.from_ = None
        args.to = None
        args.force = False
        args.resume = None
        from trip_ema.fundamentals.cli import cmd_compute_index_values
        rc = cmd_compute_index_values(args, ctx, populate_fn=_populate_fn)
        assert rc == 1


# ---------- cmd_rebalance_industry_index ---------- #


class TestCmdRebalanceIndustryIndex:

    def test_returns_0_on_successful_rebalance(
        self, db_engine, fake_config, ctx,
        capsys,
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
        _rebalance_fn = fake_run
        args = type("A", (), {})()
        args.industry = "IT - Services"
        args.date = "2026-04-01"
        args.n = 10
        args.min_constituents = None
        args.exchange = None
        from trip_ema.fundamentals.cli import cmd_rebalance_industry_index
        rc = cmd_rebalance_industry_index(args, ctx, rebalance_fn=_rebalance_fn)
        out = capsys.readouterr().out
        assert rc == 0
        assert "picked=10" in out
        assert "closed_prior=10" in out

    def test_returns_1_when_skipped(
        self, db_engine, fake_config, ctx,
        capsys,
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
        _rebalance_fn = fake_run
        args = type("A", (), {})()
        args.industry = "Sparse Industry"
        args.date = "2026-04-01"
        args.n = 10
        args.min_constituents = None
        args.exchange = None
        from trip_ema.fundamentals.cli import cmd_rebalance_industry_index
        rc = cmd_rebalance_industry_index(args, ctx, rebalance_fn=_rebalance_fn)
        out = capsys.readouterr().out
        assert rc == 1
        assert "SKIPPED" in out
        assert "min_constituents" in out

    def test_returns_2_when_index_unknown(
        self, db_engine, fake_config, ctx,
        capsys,
    ):
        def fake_run(engine, industry, rebalance_date, **kw):
            raise ValueError(f"index {industry!r} does not exist")
        _rebalance_fn = fake_run
        args = type("A", (), {})()
        args.industry = "Nonexistent"
        args.date = "2026-04-01"
        args.n = 10
        args.min_constituents = None
        args.exchange = None
        from trip_ema.fundamentals.cli import cmd_rebalance_industry_index
        rc = cmd_rebalance_industry_index(args, ctx, rebalance_fn=_rebalance_fn)
        err = capsys.readouterr().err
        assert rc == 2
        assert "does not exist" in err


# ---------- fundamentals-status with indices ---------- #


class TestCmdFundamentalsStatusShowsIndices:

    def test_shows_indices_section_when_present(
        self, db_engine, fake_config, ctx,
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
        rc = cmd_fundamentals_status(args, ctx)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Industry indices" in out
        assert "Banks - Regional" in out

    def test_omits_indices_section_when_none_exist(
        self, db_engine, fake_config, ctx,
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
        rc = cmd_fundamentals_status(args, ctx)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Industry indices" not in out


# ============================================================
# Phase 5-4 CLI tests
# ============================================================


# === STEP 14A-FIXUP: legacy cmd tests truncated ===
