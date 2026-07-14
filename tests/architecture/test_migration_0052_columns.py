"""The migration and the code must name the same 90 columns.

trip_ema.fundamentals.signal_bands.band_keys() is the single source of truth
for the band column names. Migration 0052 spells them out literally instead
of importing it — deliberately, because a migration that imports live code is
not a historical fact, it is whatever the code says today.

That leaves two copies of one truth, which is the failure mode that has cost
this codebase the most. So: a tripwire. It runs in milliseconds, needs no
database, and fails the moment someone edits ATR_PERIODS or a key name
without writing the migration that carries the schema with it.

(The code-vs-DATABASE check is a different test, in C4b, and it needs a live
schema. This one is code-vs-migration and can run anywhere.)
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from trip_ema.fundamentals.signal_bands import ATR_PERIODS, band_keys

REPO = Path(__file__).resolve().parents[2]
MIGRATION = (
    REPO / "alembic_pkg" / "versions" / "0052_signal_enrichment_bands.py"
)

SCALARS = {
    "atr10", "atr15", "atr20", "atr30", "atr40",
    "seq_in_week", "seq_in_2w",
    "signal_price", "band_polarity",
}


def _mig():
    spec = importlib.util.spec_from_file_location("mig_0052", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_exists():
    assert MIGRATION.is_file(), f"{MIGRATION} is missing"


def test_band_and_dist_columns_are_exactly_band_keys():
    m = _mig()
    assert set(m.BAND_COLUMNS) | set(m.DIST_COLUMNS) == set(band_keys()), (
        "migration 0052 and signal_bands.band_keys() have drifted. If you "
        "changed ATR_PERIODS or a key name, the schema must move with it — "
        "in a NEW migration, not by editing 0052."
    )


def test_counts():
    m = _mig()
    assert len(m.BAND_COLUMNS) == 75
    assert len(m.DIST_COLUMNS) == 15
    assert len(m.ATR_COLUMNS) == len(ATR_PERIODS) == 5
    assert len(m.SEQ_COLUMNS) == 2
    total = (len(m.BAND_COLUMNS) + len(m.DIST_COLUMNS) + len(m.ATR_COLUMNS)
             + len(m.SEQ_COLUMNS) + 2)          # signal_price, band_polarity
    assert total == 99


def test_no_duplicate_column_names():
    m = _mig()
    all_cols = (list(m.BAND_COLUMNS) + list(m.DIST_COLUMNS)
                + list(m.ATR_COLUMNS) + list(m.SEQ_COLUMNS)
                + ["signal_price", "band_polarity"])
    assert len(all_cols) == len(set(all_cols))


def test_the_atr_columns_match_the_period_set():
    m = _mig()
    assert set(m.ATR_COLUMNS) == {f"atr{n}" for n in ATR_PERIODS}


def test_every_name_fits_postgres():
    m = _mig()
    for name in (list(m.BAND_COLUMNS) + list(m.DIST_COLUMNS)
                 + list(m.ATR_COLUMNS) + list(m.SEQ_COLUMNS)):
        assert len(name) <= 63, name
        assert name == name.lower()
        assert name.replace("_", "").isalnum()


def test_the_migration_does_not_import_live_code():
    """A migration that imports signal_bands would silently change what it
    did in the past the day ATR_PERIODS is edited. It must not."""
    src = MIGRATION.read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "trip_ema" not in stripped, (
                f"0052 imports live code: {stripped!r}"
            )


def test_downgrade_drops_everything_it_added():
    src = MIGRATION.read_text(encoding="utf-8")
    body = src.split("def downgrade()", 1)[1]
    for tup in ("BAND_COLUMNS", "DIST_COLUMNS", "ATR_COLUMNS", "SEQ_COLUMNS"):
        assert tup in body, f"downgrade does not drop {tup}"
    for scalar in ("signal_price", "band_polarity"):
        assert scalar in body, f"downgrade does not drop {scalar}"
