#!/usr/bin/env python3
"""
Follow-up patcher: fix the parametrize block in
tests/gui/test_tickers_signal_coloring.py that the main patcher missed.

TestSignalBackgroundColors.test_known_signal_returns_expected_color
hard-coded the old shared blue hex for both EXIT_FROM_BUY and
EXIT_FROM_SELL. Two failures resulted. This patcher swaps them for
yellow (#fff9c4) and gray (#e0e0e0) to match the new color map.

Idempotent: safe to re-run.

Run from repo root:
    python scripts/patches/patch_tickers_signal_grouping_followup.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests/gui/test_tickers_signal_coloring.py"


# The parametrize block, verbatim.
PARAMETRIZE_OLD = '''    @pytest.mark.parametrize("signal,expected_hex", [
        ("BUY",            "#c8e6c9"),
        ("SELL",           "#ffcdd2"),
        ("EXIT_FROM_BUY",  "#bbdefb"),
        ("EXIT_FROM_SELL", "#bbdefb"),
    ])'''

# Sentinel — the new yellow hex — is a substring of the replacement,
# so a second run recognises "already applied" and exits idempotent.
PARAMETRIZE_NEW = '''    @pytest.mark.parametrize("signal,expected_hex", [
        ("BUY",            "#c8e6c9"),
        ("SELL",           "#ffcdd2"),
        ("EXIT_FROM_BUY",  "#fff9c4"),  # Step 12c: light yellow
        ("EXIT_FROM_SELL", "#e0e0e0"),  # Step 12c: light gray
    ])'''

SENTINEL = '"EXIT_FROM_BUY",  "#fff9c4"'


def main() -> int:
    if not TESTS.exists():
        print(f"FATAL: missing file: {TESTS}", file=sys.stderr)
        return 2

    text = TESTS.read_text()

    if SENTINEL in text:
        print("· idempotent  parametrize block already updated")
        return 0

    assert PARAMETRIZE_OLD in text, (
        "Anchor not found. The parametrize block has drifted from "
        "expected content — inspect the file and re-cut the anchor."
    )
    assert text.count(PARAMETRIZE_OLD) == 1, (
        f"Anchor appears {text.count(PARAMETRIZE_OLD)} times; must be unique."
    )

    TESTS.write_text(text.replace(PARAMETRIZE_OLD, PARAMETRIZE_NEW, 1))
    print("✓ applied     parametrize block updated (yellow + gray)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
