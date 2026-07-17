"""Migration 0055 must add exactly the 79 forward-walk columns, name them once,
and drop them all (plus restore quality_class) on downgrade.

This is a code-vs-migration tripwire: it imports the migration module and reads
its ``ALL_COLUMNS`` plan directly. It runs in milliseconds, needs no database,
and fails the moment the rung lists or the extra columns drift from the spec
without the migration moving with them. (The migration-vs-DATABASE check is a
different, live-schema test.)
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MIGRATION = (
    REPO / "alembic_pkg" / "versions" / "0055_signal_label_forward_walk.py"
)


def _mig():
    spec = importlib.util.spec_from_file_location("mig_0055", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_exists():
    assert MIGRATION.is_file(), f"{MIGRATION} is missing"


def test_revision_chain():
    m = _mig()
    assert m.revision == "0055_signal_label_forward_walk"
    assert m.down_revision == "0054_nse_corporate_actions"


def test_rung_counts():
    m = _mig()
    assert len(m.GAIN_RUNGS) == 21
    assert len(m.LOSS_RUNGS) == 6
    # fractional loss rungs spell legal identifiers
    assert "7_and_half" in m.LOSS_RUNGS and "12_and_half" in m.LOSS_RUNGS


def test_column_counts():
    m = _mig()
    assert len(m.BOOKKEEPING_COLUMNS) == 10
    assert len(m.GAIN_COLUMNS) == 42          # 21 rungs x (hit + bars_to)
    assert len(m.LOSS_COLUMNS) == 12          # 6 rungs x (hit + bars_to)
    assert len(m.EXTRA_COLUMNS) == 15
    assert len(m.ALL_COLUMNS) == 79


def test_no_duplicate_column_names():
    m = _mig()
    names = [n for n, _k in m.ALL_COLUMNS]
    assert len(names) == len(set(names))


def test_quality_class_is_not_added():
    m = _mig()
    names = {n for n, _k in m.ALL_COLUMNS}
    assert "quality_class" not in names


def test_every_name_fits_postgres():
    m = _mig()
    for name, _kind in m.ALL_COLUMNS:
        assert len(name) <= 63, name
        assert name == name.lower(), name
        assert name.replace("_", "").isalnum(), name


def test_kinds_are_known():
    m = _mig()
    known = {"bool", "bar", "float", "date", "str5", "str16"}
    for name, kind in m.ALL_COLUMNS:
        assert kind in known, (name, kind)


def test_rungs_generate_expected_names():
    m = _mig()
    names = {n for n, _k in m.ALL_COLUMNS}
    for n in m.GAIN_RUNGS:
        assert f"hit_gain_{n}_pct" in names
        assert f"bars_to_gain_{n}_pct" in names
    for lbl in m.LOSS_RUNGS:
        assert f"hit_loss_{lbl}_pct" in names
        assert f"bars_to_loss_{lbl}_pct" in names


def test_migration_does_not_import_live_code():
    """A migration is a historical fact; importing trip_ema would make it
    mean whatever the code says today."""
    src = MIGRATION.read_text(encoding="utf-8")
    for line in src.splitlines():
        s = line.strip()
        if s.startswith(("import ", "from ")):
            assert "trip_ema" not in s, f"0055 imports live code: {s!r}"


def test_upgrade_drops_quality_class_first():
    src = MIGRATION.read_text(encoding="utf-8")
    up = src.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    assert 'drop_index("ix_signal_labels_quality"' in up
    assert 'drop_constraint("sl_quality_class_chk"' in up
    assert 'drop_column(_TABLE, "quality_class")' in up


def test_downgrade_reverses_everything():
    src = MIGRATION.read_text(encoding="utf-8")
    dn = src.split("def downgrade()", 1)[1]
    # drops the whole plan and restores quality_class + its check + index
    assert "reversed(ALL_COLUMNS)" in dn
    assert '"quality_class"' in dn
    assert "sl_quality_class_chk" in dn
    assert "ix_signal_labels_quality" in dn
