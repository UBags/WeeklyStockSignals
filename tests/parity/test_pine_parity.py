"""Pine-parity harness.

Loads CSV fixtures from `tests/parity/fixtures/` and verifies that the
Python indicator output matches the expected values bar-for-bar. See
the README in this directory for the fixture format and how to
regenerate from TradingView.
"""

from __future__ import annotations

import csv
import math
from datetime import datetime
from pathlib import Path

import pytest

from trip_ema.domain import (
    BarState,
    IndicatorParams,
    OHLCVBar,
    TickerRef,
    Timeframe,
)
from trip_ema.engine import TickerEngine


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _discover_fixtures():
    """List all CSV fixtures. Each fixture parameterises one test run."""
    return sorted(FIXTURES_DIR.glob("*.csv"))


def _parse_float_or_nan(s: str) -> float:
    """Empty cells in expected_* columns mean NaN (warmup phase)."""
    s = s.strip()
    if not s:
        return float("nan")
    return float(s)


def _load_fixture(path: Path) -> list[dict]:
    """Parse a CSV fixture into a list of row dicts."""
    rows = []
    with path.open() as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append({
                "timestamp": datetime.fromisoformat(r["timestamp"]),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"]),
                "expected_med_ema": _parse_float_or_nan(r["expected_med_ema"]),
                "expected_long_ema": _parse_float_or_nan(r["expected_long_ema"]),
                "expected_longest_ema": _parse_float_or_nan(r["expected_longest_ema"]),
                "expected_atr": _parse_float_or_nan(r["expected_atr"]),
            })
    return rows


def _approx_or_nan(expected: float, actual: float, tol: float = 1e-9) -> bool:
    """Both-NaN matches; finite-vs-finite uses abs tolerance.
    Mixed NaN/finite is a mismatch."""
    if math.isnan(expected) and math.isnan(actual):
        return True
    if math.isnan(expected) or math.isnan(actual):
        return False
    return abs(expected - actual) <= tol


FIXTURE_FILES = _discover_fixtures()


@pytest.mark.skipif(not FIXTURE_FILES, reason="No parity fixtures present")
@pytest.mark.parametrize(
    "fixture_path",
    FIXTURE_FILES,
    ids=lambda p: p.name,
)
def test_pine_parity(fixture_path):
    """For each fixture, feed the OHLCV bars to a fresh engine and
    verify each BarState matches the expected values."""
    rows = _load_fixture(fixture_path)
    assert rows, f"Empty fixture: {fixture_path.name}"

    eng = TickerEngine(
        TickerRef(exchange="NSE", symbol="NIFTY", instrument="INDEX"),
        Timeframe.WEEKLY,
        IndicatorParams(),
    )

    mismatches: list[str] = []
    for i, row in enumerate(rows):
        bar = OHLCVBar(
            timestamp=row["timestamp"],
            open=row["open"], high=row["high"],
            low=row["low"], close=row["close"], volume=row["volume"],
        )
        state = eng.ingest(bar)
        # Compare each indicator column
        for col, getter in [
            ("expected_med_ema", lambda s: s.med_ema),
            ("expected_long_ema", lambda s: s.long_ema),
            ("expected_longest_ema", lambda s: s.longest_ema),
            ("expected_atr", lambda s: s.atr),
        ]:
            expected = row[col]
            actual = getter(state)
            if not _approx_or_nan(expected, actual):
                mismatches.append(
                    f"bar {i} ({row['timestamp']}) {col}: "
                    f"expected {expected!r}, got {actual!r}"
                )

    if mismatches:
        pytest.fail(
            f"{len(mismatches)} parity mismatch(es) in {fixture_path.name}:\n"
            + "\n".join(mismatches[:20])
            + (f"\n... and {len(mismatches) - 20} more" if len(mismatches) > 20 else "")
        )


# ============================================================
# Self-pinning fixture generator
#
# Run with:
#   python -m tests.parity.test_pine_parity --regenerate
#
# This produces a fixture by RUNNING the current Python implementation
# and writing its output as the "expected" values. The resulting
# fixture protects against future drift in the Python code but does
# NOT verify parity with Pine — it's a regression net, not a fidelity
# check.
#
# To upgrade to a real Pine-parity fixture, replace the generated
# CSV with an export from TradingView (see README).
# ============================================================


def _regenerate_self_pin_fixture(out_path: Path, n_bars: int = 120):
    """Generate a deterministic fixture from the current Python impl.

    The bar values are a synthetic series (close = 100 + i*0.5 with a
    sine wiggle) so the EMAs and ATR have something interesting to
    track. Output written to `out_path` in the harness's CSV format.
    """
    import math as _m
    from datetime import timedelta, timezone

    eng = TickerEngine(
        TickerRef(exchange="NSE", symbol="NIFTY", instrument="INDEX"),
        Timeframe.WEEKLY,
        IndicatorParams(),
    )
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    rows_out: list[list[str]] = [[
        "timestamp", "open", "high", "low", "close", "volume",
        "expected_med_ema", "expected_long_ema",
        "expected_longest_ema", "expected_atr",
    ]]
    for i in range(n_bars):
        ts = start + timedelta(days=7 * i)
        base = 100.0 + i * 0.5 + _m.sin(i / 6.0) * 3.0
        bar = OHLCVBar(
            timestamp=ts,
            open=base, high=base + 1.5,
            low=base - 1.2, close=base + 0.4,
            volume=1000.0 + (i % 7) * 50.0,
        )
        state = eng.ingest(bar)

        def _fmt(v: float) -> str:
            return "" if math.isnan(v) else repr(v)

        rows_out.append([
            ts.isoformat(),
            repr(bar.open), repr(bar.high),
            repr(bar.low), repr(bar.close), repr(bar.volume),
            _fmt(state.med_ema), _fmt(state.long_ema),
            _fmt(state.longest_ema), _fmt(state.atr),
        ])

    with out_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerows(rows_out)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--regenerate",
        action="store_true",
        help="Regenerate the self-pin fixture from current Python implementation",
    )
    args = parser.parse_args()
    if args.regenerate:
        out = FIXTURES_DIR / "self_pin_synthetic_weekly.csv"
        _regenerate_self_pin_fixture(out)
        print(f"Regenerated: {out}")
