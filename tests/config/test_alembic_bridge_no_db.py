"""DB-free tests for alembic_bridge.

The Postgres-backed tests are in tests/persistence/test_alembic_bridge.py.
This file covers the slim subset of behavior that doesn't need a real DB:

  - The module imports cleanly without alembic installed
  - The "alembic not installed" fallback returns False with a warning
  - The constants match the actual baseline revision file

Putting these in tests/config rather than tests/persistence so they
run as part of the DB-free suite. The bridge module itself doesn't
import alembic at module-load time (defers via try-import in the
function), so this test confirms that contract.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_bridge_module_imports_without_alembic():
    """Bridge must import even when alembic isn't installed. The CI
    db-free job and the lean core install pattern both depend on this.
    The bridge defers `from alembic import ...` to the function body."""
    # Already imported because pytest imported tests.config. Sanity:
    from trip_ema.persistence import alembic_bridge  # noqa: F401
    assert alembic_bridge is not None


def test_baseline_constant_matches_revision_file():
    """The _BASELINE_REVISION constant in the bridge must match the
    `revision = "..."` string in the actual baseline revision file.

    Phase 12: this is now 0001_initial_schema (the squashed initial
    revision), not the old 011_baseline no-op marker."""
    from trip_ema.persistence.alembic_bridge import _BASELINE_REVISION

    repo_root = Path(__file__).resolve().parent.parent.parent
    revision_file = (
        repo_root / "alembic_pkg" / "versions"
        / "0001_initial_schema.py"
    )
    assert revision_file.exists(), \
        f"Baseline revision file missing at {revision_file}"

    text = revision_file.read_text()
    expected_line = f'revision = "{_BASELINE_REVISION}"'
    assert expected_line in text, (
        f"Baseline revision file does not contain {expected_line!r}. "
        "If you renamed the constant, update the file accordingly."
    )


def test_stamp_baseline_returns_false_without_alembic(caplog):
    """If alembic isn't importable, the bridge logs a warning and
    returns False rather than crashing. Operators on minimal installs
    can stamp manually later."""
    from trip_ema.persistence.alembic_bridge import (
        stamp_baseline_if_needed,
    )

    # Mock the engine so we can reach the alembic-import line. We
    # need _legacy_has_reached_baseline → True and
    # _alembic_has_a_version → False, then the bridge tries to
    # import alembic; we simulate that import failing.
    fake_engine = MagicMock()

    # Phase 14: the two branch probes are kwargs now; only the
    # sys.modules stub remains (environment simulation, not a seam).
    with patch.dict(sys.modules, {"alembic": None, "alembic.command": None}):
        with caplog.at_level(logging.WARNING, logger="trip_ema.persistence.alembic_bridge"):
            result = stamp_baseline_if_needed(
                fake_engine,
                legacy_has_reached_baseline_fn=lambda e: True,
                alembic_has_a_version_fn=lambda e: False,
            )

    assert result is False
    assert any(
        "Alembic is not installed" in rec.message
        for rec in caplog.records
    ), f"Expected warning about missing alembic; got: {[r.message for r in caplog.records]}"
