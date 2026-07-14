"""Add the §20 band columns to signal_enrichment.

99 new columns, all NULLABLE, no server defaults — so this is a catalog-only
ALTER in PG11+ and takes milliseconds on the full signal history.

    75  band_<ema>_atr<n>_<k>   boolean          which ATR slab the signal-bar
                                                 price sat in, per EMA x ATR
    15  dist_<ema>_atr<n>       double precision signed, polarity-adjusted
                                                 (price - ema) / atr_n
     5  atr10..atr40            double precision the band rulers themselves
     2  seq_in_week, seq_in_2w  integer          causal, per signal type
     1  signal_price            double precision the close the bands were cut against
     1  band_polarity           varchar(4)       UP | DOWN

WHY NULLABLE, AND WHY THAT MATTERS MORE THAN IT LOOKS

Every signal_enrichment row written before feature_spec_version v0.5 has no
band data. If the 75 booleans were NOT NULL DEFAULT false, those rows would
read as "all slabs false" — which in this schema is a MEANINGFUL value: it
says the price was on the wrong side of the EMA when the signal fired. The
old rows would be indistinguishable from real, unusual signals, and would be
silently eaten by anything trained on this table. NULL is the only encoding
that says "not computed".

0053 tightens the booleans, the two sequence ints and band_polarity to NOT
NULL — but only AFTER the v0.5 recompute has populated them. Tightening
before the backfill would fail on every historical row, which is the point.

The distances and the ATRs stay nullable forever: NaN is a legitimate outcome
at warm-up (no true range yet) and NULL is how a NaN must land in a double
precision column. A literal NaN in float8 is valid Postgres and it silently
poisons AVG, every `> 0`, and every model that reads it.

WHY THE COLUMN NAMES ARE SPELLED OUT HERE

They are also derivable from trip_ema.fundamentals.signal_bands.band_keys().
This migration does NOT import it. A migration is a frozen historical fact;
one that imports live code would silently mean something different the day
someone edits ATR_PERIODS. The two copies are held together by
tests/architecture/test_migration_0052_columns.py, which fails if they drift.

Revision ID: 0052_signal_enrichment_bands
Revises:     0051_local_midnight_bar_anchor
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0052_signal_enrichment_bands"
down_revision = "0051_local_midnight_bar_anchor"
branch_labels = None
depends_on = None


TABLE = "signal_enrichment"

# 3 EMAs x 5 ATR periods x 5 slabs. FROZEN — see the module docstring.
BAND_COLUMNS: tuple[str, ...] = (
    "band_med_atr10_1", "band_med_atr10_2", "band_med_atr10_3",
    "band_med_atr10_4", "band_med_atr10_5", "band_med_atr15_1",
    "band_med_atr15_2", "band_med_atr15_3", "band_med_atr15_4",
    "band_med_atr15_5", "band_med_atr20_1", "band_med_atr20_2",
    "band_med_atr20_3", "band_med_atr20_4", "band_med_atr20_5",
    "band_med_atr30_1", "band_med_atr30_2", "band_med_atr30_3",
    "band_med_atr30_4", "band_med_atr30_5", "band_med_atr40_1",
    "band_med_atr40_2", "band_med_atr40_3", "band_med_atr40_4",
    "band_med_atr40_5", "band_long_atr10_1", "band_long_atr10_2",
    "band_long_atr10_3", "band_long_atr10_4", "band_long_atr10_5",
    "band_long_atr15_1", "band_long_atr15_2", "band_long_atr15_3",
    "band_long_atr15_4", "band_long_atr15_5", "band_long_atr20_1",
    "band_long_atr20_2", "band_long_atr20_3", "band_long_atr20_4",
    "band_long_atr20_5", "band_long_atr30_1", "band_long_atr30_2",
    "band_long_atr30_3", "band_long_atr30_4", "band_long_atr30_5",
    "band_long_atr40_1", "band_long_atr40_2", "band_long_atr40_3",
    "band_long_atr40_4", "band_long_atr40_5", "band_longest_atr10_1",
    "band_longest_atr10_2", "band_longest_atr10_3", "band_longest_atr10_4",
    "band_longest_atr10_5", "band_longest_atr15_1", "band_longest_atr15_2",
    "band_longest_atr15_3", "band_longest_atr15_4", "band_longest_atr15_5",
    "band_longest_atr20_1", "band_longest_atr20_2", "band_longest_atr20_3",
    "band_longest_atr20_4", "band_longest_atr20_5", "band_longest_atr30_1",
    "band_longest_atr30_2", "band_longest_atr30_3", "band_longest_atr30_4",
    "band_longest_atr30_5", "band_longest_atr40_1", "band_longest_atr40_2",
    "band_longest_atr40_3", "band_longest_atr40_4", "band_longest_atr40_5",
)

# 3 EMAs x 5 ATR periods, signed, polarity-adjusted.
DIST_COLUMNS: tuple[str, ...] = (
    "dist_med_atr10", "dist_med_atr15", "dist_med_atr20",
    "dist_med_atr30", "dist_med_atr40", "dist_long_atr10",
    "dist_long_atr15", "dist_long_atr20", "dist_long_atr30",
    "dist_long_atr40", "dist_longest_atr10", "dist_longest_atr15",
    "dist_longest_atr20", "dist_longest_atr30", "dist_longest_atr40",
)

ATR_COLUMNS: tuple[str, ...] = (
    "atr10", "atr15", "atr20", "atr30", "atr40",
)

SEQ_COLUMNS: tuple[str, ...] = (
    "seq_in_week", "seq_in_2w",
)

POLARITY_CHK = "se_band_polarity_chk"


def upgrade() -> None:
    for name in BAND_COLUMNS:
        op.add_column(TABLE, sa.Column(name, sa.Boolean, nullable=True))
    for name in DIST_COLUMNS + ATR_COLUMNS + ("signal_price",):
        op.add_column(TABLE, sa.Column(name, sa.Float, nullable=True))
    for name in SEQ_COLUMNS:
        op.add_column(TABLE, sa.Column(name, sa.Integer, nullable=True))
    op.add_column(TABLE, sa.Column("band_polarity", sa.String(4),
                                   nullable=True))

    # NULL is permitted until the v0.5 recompute lands; the vocabulary is not.
    op.create_check_constraint(
        POLARITY_CHK, TABLE,
        "band_polarity IS NULL OR band_polarity IN ('UP', 'DOWN')",
    )

    op.execute(
        "COMMENT ON COLUMN signal_enrichment.band_polarity IS "
        "'Geometry the bands were cut with: UP for BUY and EXIT_FROM_SELL, "
        "DOWN for SELL and EXIT_FROM_BUY. Two rows on the same bar with the "
        "same price can carry different flags — read the flags THROUGH this.'"
    )
    op.execute(
        "COMMENT ON COLUMN signal_enrichment.seq_in_week IS "
        "'1-based ordinal of this signal among signals of the SAME TYPE in "
        "its Monday-anchored week, counting only those on or before it. "
        "Causal: a Tuesday signal is not told about Friday.'"
    )
    op.execute(
        "COMMENT ON COLUMN signal_enrichment.seq_in_2w IS "
        "'As seq_in_week, over [previous Monday, this signal_date].'"
    )
    op.execute(
        "COMMENT ON COLUMN signal_enrichment.signal_price IS "
        "'The close the bands were cut against: the forming daily bar''s "
        "close on signal_date, the same close the signal fired from.'"
    )
    op.execute(
        "COMMENT ON COLUMN signal_enrichment.dist_med_atr20 IS "
        "'Signed, polarity-adjusted (price - med_ema) / atr20. Positive is "
        "in the signal''s favour. NULL means the pair was not computable "
        "(no ATR yet) — which is also why the 75 band booleans may be all-"
        "false for a reason OTHER than the price being behind the EMA. The "
        "distance is the validity marker; the flags are the convenience.'"
    )


def downgrade() -> None:
    op.drop_constraint(POLARITY_CHK, TABLE, type_="check")
    for name in (
        ("band_polarity", "signal_price")
        + SEQ_COLUMNS + ATR_COLUMNS + DIST_COLUMNS + BAND_COLUMNS
    ):
        op.drop_column(TABLE, name)
