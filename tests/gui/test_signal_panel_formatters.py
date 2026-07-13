"""The signal-generation panel renders each exchange's mtime in ITS OWN clock.

_format_mtime used `.astimezone()` with no argument — the machine's zone. The panel
is laid out per exchange, so on a NUC in Bangalore a NYSE file written at 17:05 ET
rendered as 02:35 the next morning. Nothing corrupts; an operator is simply told
the wrong thing about their own system.

Nothing tested these formatters at all, which is how it survived.
"""
from __future__ import annotations

import pytest

from trip_ema.gui.views.signal_generation_panel import (
    _format_metadata_line,
    _format_mtime,
)


# 2026-07-13 21:05 UTC = 17:05 ET (EDT) = 2026-07-14 02:35 IST.
# Deliberately an instant that lands on a DIFFERENT DAY in the two zones, so a
# wrong zone cannot produce the right string by accident.
MTIME = "2026-07-13T21:05:00Z"


class TestFormatMtime:

    def test_nyse_renders_in_new_york(self):
        assert _format_mtime(MTIME, "NYSE") == "2026-07-13 17:05"

    def test_nse_renders_in_kolkata(self):
        assert _format_mtime(MTIME, "NSE") == "2026-07-14 02:35"

    def test_the_two_disagree(self):
        """If these ever match, the exchange argument is being ignored."""
        assert _format_mtime(MTIME, "NSE") != _format_mtime(MTIME, "NYSE")

    def test_no_exchange_says_utc_out_loud(self):
        out = _format_mtime(MTIME, None)
        assert out == "2026-07-13 21:05 UTC"

    def test_unknown_exchange_degrades_to_utc_rather_than_crashing(self):
        """A display fallback, never a compute one: a label may say UTC; it may
        not take the panel down with it."""
        assert _format_mtime(MTIME, "ATLANTIS").endswith("UTC")

    def test_missing_mtime(self):
        assert _format_mtime(None, "NSE") == "\u2014"

    def test_garbage_mtime_is_returned_verbatim(self):
        assert _format_mtime("not-a-timestamp", "NSE") == "not-a-timestamp"[:16]


class TestFormatMetadataLine:

    def test_threads_the_exchange_through(self):
        payload = {
            "exists": True, "size_bytes": 2048, "mtime": MTIME,
            "counts": {"signals_total": 1234},
        }
        nse = _format_metadata_line(payload, "NSE")
        nyse = _format_metadata_line(payload, "NYSE")
        assert "2026-07-14 02:35" in nse
        assert "2026-07-13 17:05" in nyse
        assert "1,234 signals" in nse

    def test_no_file(self):
        assert _format_metadata_line({"exists": False}, "NSE") == "no file yet"
