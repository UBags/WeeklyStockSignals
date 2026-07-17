"""0055 signal_label forward walk

Retire the weekly ``quality_class`` label (drop the column, its CHECK
``sl_quality_class_chk`` and index ``ix_signal_labels_quality``) and add the
per-signal forward-walk outcome columns — see spec
``02_signal_forward_walk_labels_spec.md``.

``quality_class`` is superseded: the great/good/bad target ``y`` is derived
downstream from the measured facts this table now stores, not persisted here.

The weekly ``fwd_ret_4w/8w/13w`` / ``fwd_max_dd`` / ``fwd_rr`` columns are
retained (feature-only); retiring the weekly labeler is a separate commit.

Ordering: a preceding commit must stop the labeler writing ``quality_class``
(domain dataclass, ``compute_label``, runner, repo INSERT, tests) before this
migration drops the column, else the labeler INSERT breaks.

The added column names live in ONE module-level list (``ALL_COLUMNS``) that both
``upgrade`` and ``downgrade`` iterate — a single source of truth, so the two
directions cannot drift. ``tests/architecture/test_migration_0055_columns.py``
is the code-vs-migration tripwire over that list (no DB needed). The migration
does NOT import live code — a migration is a historical fact, not whatever the
code says today.

Revision ID: 0055_signal_label_forward_walk
Revises: 0054_nse_corporate_actions
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0055_signal_label_forward_walk"
down_revision = "0054_nse_corporate_actions"
branch_labels = None
depends_on = None

_TABLE = "signal_labels"

# Gain rungs (21) and loss rungs (6). Loss rungs carry label-strings, not
# numbers, so the fractional rungs spell legal identifiers.
GAIN_RUNGS = [
    1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25, 27, 29, 31, 33, 35,
    50, 75, 100,
]
LOSS_RUNGS = ["5", "7_and_half", "10", "12_and_half", "15", "20"]

# --- column plan: (name, kind). kinds map to types + defaults in _col() ------
# bool  -> Boolean NOT NULL default false
# bar   -> Integer NOT NULL default -1  (bar distance / count; -1 = never/none)
# float -> Float NULL                    (NULL = genuinely no value)
# date/str5/str16 -> nullable (only meaningful once forward_walk_done)

BOOKKEEPING_COLUMNS: list[tuple[str, str]] = [
    ("forward_walk_done", "bool"),
    ("side", "str5"),
    ("entry_date", "date"),
    ("entry_open", "float"),
    ("exit_date", "date"),
    ("exit_close", "float"),
    ("exit_reason", "str16"),
    ("bars_in_window", "bar"),
    ("realized_ret", "float"),
    ("window_censored", "bool"),
]

GAIN_COLUMNS: list[tuple[str, str]] = []
for _n in GAIN_RUNGS:
    GAIN_COLUMNS.append((f"hit_gain_{_n}_pct", "bool"))
    GAIN_COLUMNS.append((f"bars_to_gain_{_n}_pct", "bar"))

LOSS_COLUMNS: list[tuple[str, str]] = []
for _l in LOSS_RUNGS:
    LOSS_COLUMNS.append((f"hit_loss_{_l}_pct", "bool"))
    LOSS_COLUMNS.append((f"bars_to_loss_{_l}_pct", "bar"))

EXTRA_COLUMNS: list[tuple[str, str]] = [
    ("ever_in_profit", "bool"),
    ("mae_pct", "float"),
    ("mae_bar", "bar"),
    ("mfe_pct", "float"),
    ("mfe_bar", "bar"),
    ("mae_before_mfe", "bool"),
    ("excess_ret_industry", "float"),
    ("excess_ret_market", "float"),
    ("mfe_atr20", "float"),
    ("mae_atr20", "float"),
    ("time_underwater_bars", "bar"),
    ("slope_per_bar", "float"),
    ("realized_vol", "float"),
    ("sharpe_in_window", "float"),
    ("capture_ratio", "float"),
]

ALL_COLUMNS: list[tuple[str, str]] = (
    BOOKKEEPING_COLUMNS + GAIN_COLUMNS + LOSS_COLUMNS + EXTRA_COLUMNS
)


def _col(name: str, kind: str) -> sa.Column:
    if kind == "bool":
        return sa.Column(name, sa.Boolean, nullable=False,
                         server_default=sa.text("false"))
    if kind == "bar":
        return sa.Column(name, sa.Integer, nullable=False,
                         server_default=sa.text("-1"))
    if kind == "float":
        return sa.Column(name, sa.Float, nullable=True)
    if kind == "date":
        return sa.Column(name, sa.Date, nullable=True)
    if kind == "str5":
        return sa.Column(name, sa.String(5), nullable=True)
    if kind == "str16":
        return sa.Column(name, sa.String(16), nullable=True)
    raise ValueError(f"unknown column kind: {kind!r}")


def upgrade() -> None:
    # 1. Retire the weekly quality_class label. Postgres would cascade the
    #    index/constraint on DROP COLUMN; drop them explicitly first so the
    #    downgrade is an exact mirror.
    op.drop_index("ix_signal_labels_quality", table_name=_TABLE)
    op.drop_constraint("sl_quality_class_chk", _TABLE, type_="check")
    op.drop_column(_TABLE, "quality_class")

    # 2. Add every forward-walk column from the single plan.
    for name, kind in ALL_COLUMNS:
        op.add_column(_TABLE, _col(name, kind))


def downgrade() -> None:
    for name, _kind in reversed(ALL_COLUMNS):
        op.drop_column(_TABLE, name)

    op.add_column(_TABLE, sa.Column("quality_class", sa.String(16),
                                    nullable=True))
    op.create_check_constraint(
        "sl_quality_class_chk", _TABLE,
        "quality_class IS NULL OR "
        "quality_class IN ('BAD', 'GOOD', 'VERY_GOOD')",
    )
    op.create_index("ix_signal_labels_quality", _TABLE, ["quality_class"])
