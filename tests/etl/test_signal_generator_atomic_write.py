"""Tests for the atomic JSON write helper in signal_generator.

`_atomic_write_json` writes to `{path}.tmp` and renames to `{path}`.
A reader opening `path` mid-write must see either the old version
or the new version, never a half-written one.

These tests use `tmp_path` (a real filesystem path) because
faithfully exercising the rename semantics requires real I/O.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from trip_ema.etl.signal_generator import _atomic_write_json


class TestAtomicWriteJson:
    def test_writes_file_with_expected_content(self, tmp_path):
        """Happy path: the data lands on disk and the file parses."""
        target = tmp_path / "NSE.json"
        data = {"schema_version": "1.0", "exchange": "NSE", "by_date": {}}
        _atomic_write_json(target, data)

        assert target.exists()
        roundtrip = json.loads(target.read_text())
        assert roundtrip == data

    def test_temp_file_is_removed_after_successful_write(self, tmp_path):
        """After a successful write, the .tmp file must not linger
        on disk — `os.replace` consumes it."""
        target = tmp_path / "NSE.json"
        _atomic_write_json(target, {"exchange": "NSE"})

        tmp_file = target.with_suffix(target.suffix + ".tmp")
        assert not tmp_file.exists()
        assert target.exists()

    def test_existing_file_is_replaced_atomically(self, tmp_path):
        """Writing over an existing file leaves only the new content
        — no half-written or partial-overwrite state."""
        target = tmp_path / "NSE.json"
        target.write_text(json.dumps({"old": "version"}))

        new_data = {"new": "version", "more": [1, 2, 3]}
        _atomic_write_json(target, new_data)

        roundtrip = json.loads(target.read_text())
        assert roundtrip == new_data
        assert "old" not in roundtrip

    def test_crash_between_temp_write_and_rename_leaves_real_file_untouched(
        self, tmp_path,
    ):
        """If os.replace fails (simulated crash), the real file must
        be the OLD version, not corrupted."""
        target = tmp_path / "NSE.json"
        original = {"exchange": "NSE", "old": True}
        target.write_text(json.dumps(original))

        new_data = {"exchange": "NSE", "new": True}

        with patch("trip_ema.etl.signal_generator.os.replace",
                   side_effect=OSError("simulated crash")):
            with pytest.raises(OSError, match="simulated crash"):
                _atomic_write_json(target, new_data)

        # Real file is untouched. (The .tmp file may exist; that's
        # acceptable per the design — the next successful run will
        # overwrite it.)
        roundtrip = json.loads(target.read_text())
        assert roundtrip == original

    def test_crash_mid_temp_write_leaves_real_file_untouched(self, tmp_path):
        """If the write to the .tmp file itself fails partway, the
        real file must be the OLD version. The .tmp file may exist
        but is irrelevant — `os.replace` was never reached."""
        target = tmp_path / "NSE.json"
        original = {"exchange": "NSE", "old": True}
        target.write_text(json.dumps(original))

        # Make json.dump raise mid-stream.
        with patch("trip_ema.etl.signal_generator.json.dump",
                   side_effect=IOError("simulated mid-write crash")):
            with pytest.raises(IOError, match="simulated mid-write crash"):
                _atomic_write_json(target, {"new": True})

        # Real file is untouched.
        roundtrip = json.loads(target.read_text())
        assert roundtrip == original

    def test_creates_parent_directory_if_missing(self, tmp_path):
        """If the output directory doesn't exist yet, the helper
        creates it. The CLI's default `generated_signals/` directory
        is created on first run via this path."""
        target = tmp_path / "subdir" / "NSE.json"
        assert not target.parent.exists()

        _atomic_write_json(target, {"exchange": "NSE"})

        assert target.parent.is_dir()
        assert target.exists()
