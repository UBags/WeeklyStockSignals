"""Tests for the `generate-signals` CLI subcommand (Phase 15a).

Pure argparse + dispatch tests — no DB, no engine. The signal
generator itself is tested end-to-end in
`test_signal_generator.py`; here we verify the CLI plumbing:
  - flag parsing (timeframe repeatable, --force-full toggle)
  - default timeframe is ['1W'] when --timeframe is omitted
  - bad timeframe rejected by argparse `choices=`
  - the dispatch invokes `generate_historical_signals` with the
    right kwargs
  - the summary line is printed to stdout on success

The signal generator itself is mocked so this stays pytest-fast
(no Postgres needed in this file).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trip_ema.domain import Timeframe
from trip_ema.etl.cli import _build_parser, cmd_generate_signals
from trip_ema.etl.signal_generator import SignalGenerationResult


# ---------- argparse layer ---------- #


class TestGenerateSignalsArgparse:
    def test_default_timeframe_applied_when_omitted(self):
        """After parse_args, args.timeframe should be applied by
        main()'s post-parse logic. We exercise that by going
        through main()-equivalent behavior."""
        # Direct parse_args check: argparse leaves it as None.
        parser = _build_parser()
        args = parser.parse_args([
            "generate-signals", "--exchange", "NSE",
        ])
        assert args.command == "generate-signals"
        assert args.exchange == "NSE"
        # The default-application happens in main(); not here.
        assert args.timeframe is None
        assert args.force_full is False

    def test_repeatable_timeframe(self):
        parser = _build_parser()
        args = parser.parse_args([
            "generate-signals",
            "--exchange", "NSE",
            "--timeframe", "1D",
            "--timeframe", "1W",
        ])
        assert args.timeframe == ["1D", "1W"]

    def test_force_full_flag(self):
        parser = _build_parser()
        args = parser.parse_args([
            "generate-signals", "--exchange", "NSE", "--force-full",
        ])
        assert args.force_full is True

    def test_bad_timeframe_rejected_by_argparse(self, capsys):
        """argparse `choices=("1D", "1W")` rejects '1h' at parse
        time. SystemExit propagates with exit code 2."""
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([
                "generate-signals", "--exchange", "NSE",
                "--timeframe", "1h",
            ])

    def test_exchange_required(self):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["generate-signals"])


# ---------- dispatch via cmd_generate_signals ---------- #


class _StubArgs:
    """Minimal args object — what argparse would produce."""
    def __init__(self, exchange, timeframe, force_full):
        self.exchange = exchange
        self.timeframe = timeframe
        self.force_full = force_full


def _fake_result() -> SignalGenerationResult:
    now = datetime.now(timezone.utc)
    return SignalGenerationResult(
        exchange="NSE",
        run_id=42,
        output_path=Path("generated_signals/NSE.json"),
        timeframes_computed=["1W"],
        tickers_processed=3,
        tickers_skipped=0,
        signals_emitted={"BUY": 5, "SELL": 4, "EXIT_FROM_BUY": 3, "EXIT_FROM_SELL": 2},
        signals_deleted=0,
        started_at=now,
        finished_at=now,
    )


class TestGenerateSignalsDispatch:
    def test_passes_timeframes_through(self, capsys):
        """The dispatch translates `--timeframe 1D --timeframe 1W`
        into a list[Timeframe] passed to generate_historical_signals.

        Patch targets:
          - `create_engine_from_config` is imported at module-level
            in cli.py, so patch where it's bound: `trip_ema.etl.cli`.
          - `generate_historical_signals` is lazy-imported INSIDE the
            handler (to keep CLI startup fast for other subcommands).
            Until the handler runs, the name doesn't exist on
            `trip_ema.etl.cli` — so patch the source module instead.
            The lazy `from .signal_generator import ...` then picks
            up the patched binding.
        """
        config = MagicMock()
        args = _StubArgs("NSE", ["1D", "1W"], False)
        with patch(
            "trip_ema.etl.cli.create_engine_from_config",
        ) as mock_engine_factory, patch(
            "trip_ema.etl.signal_generator.generate_historical_signals",
            return_value=_fake_result(),
        ) as mock_gen:
            mock_engine_factory.return_value = MagicMock()
            rc = cmd_generate_signals(args, config)
        assert rc == 0
        call_kwargs = mock_gen.call_args.kwargs
        assert call_kwargs["exchange"] == "NSE"
        assert call_kwargs["timeframes"] == [Timeframe.DAILY, Timeframe.WEEKLY]
        assert call_kwargs["force_full"] is False

    def test_force_full_flag_propagates(self, capsys):
        config = MagicMock()
        args = _StubArgs("NSE", ["1W"], True)
        with patch(
            "trip_ema.etl.cli.create_engine_from_config",
        ), patch(
            "trip_ema.etl.signal_generator.generate_historical_signals",
            return_value=_fake_result(),
        ) as mock_gen:
            rc = cmd_generate_signals(args, config)
        assert rc == 0
        assert mock_gen.call_args.kwargs["force_full"] is True

    def test_summary_line_printed_to_stdout(self, capsys):
        config = MagicMock()
        args = _StubArgs("NSE", ["1W"], False)
        with patch(
            "trip_ema.etl.cli.create_engine_from_config",
        ), patch(
            "trip_ema.etl.signal_generator.generate_historical_signals",
            return_value=_fake_result(),
        ):
            cmd_generate_signals(args, config)
        out, err = capsys.readouterr()
        assert "signal_backtest complete" in out
        assert "exchange=NSE" in out
        assert "run_id=42" in out
        assert "tickers_processed=3" in out

    def test_disposes_db_engine_even_on_failure(self):
        """Failure path: if the runner raises, the engine still gets
        disposed via the finally block. Important — otherwise a
        repeated CLI failure could leak connections."""
        config = MagicMock()
        args = _StubArgs("NSE", ["1W"], False)
        mock_engine = MagicMock()
        with patch(
            "trip_ema.etl.cli.create_engine_from_config",
            return_value=mock_engine,
        ), patch(
            "trip_ema.etl.signal_generator.generate_historical_signals",
            side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                cmd_generate_signals(args, config)
        mock_engine.dispose.assert_called_once()


# ---------- main() top-level integration ---------- #


class TestMainAppliesDefaultTimeframe:
    """The default-when-omitted logic lives in main(), not in the
    handler. Verify it from the main() seam."""

    def test_main_applies_1w_default_when_timeframe_omitted(self, capsys, monkeypatch, tmp_path):
        """If --timeframe is omitted, args.timeframe should be ['1W']
        by the time the handler is invoked."""
        # Write a minimal config the loader will accept
        config_path = tmp_path / "config.yaml"
        config_path.write_text("""
database:
  driver: postgresql
  host: localhost
  port: 5432
  database: market_data
  user: app
  password_env: TEST_DB_PWD
ema:
  med: {base: 8, s1: 4, s2: 4, final: null}
  long: {base: 15, s1: 4, s2: 4, final: 3}
  longest: {base: 28, s1: 7, s2: 5, final: 3}
atr:
  length: 14
  inner_mult: 0.2
  mid_mult: 2.0
  outer_mult: 4.0
market_hours:
  - exchange: NSE
    open: "09:15"
    close: "15:30"
    tz: Asia/Kolkata
    weekdays: [1, 2, 3, 4, 5]
""")
        monkeypatch.setenv("TEST_DB_PWD", "x")

        from trip_ema.etl.cli import main

        captured_args = {}

        def _capture(args, cfg):
            # Snapshot the args.timeframe at handler-call time
            captured_args["timeframe"] = list(args.timeframe)
            return 0

        with patch.dict(
            "trip_ema.etl.cli.__dict__",
            {"cmd_generate_signals": _capture},
        ):
            rc = main([
                "--config", str(config_path),
                "generate-signals", "--exchange", "NSE",
            ])
        assert rc == 0
        assert captured_args["timeframe"] == ["1W"]
