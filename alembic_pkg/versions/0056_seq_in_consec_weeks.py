"""0056 seq_in_2w -> seq_in_consec_weeks

Rename ``signal_enrichment.seq_in_2w`` and change what it means.

``seq_in_2w`` counted signals of a type over a FIXED two-week window. On run
69's 193,379 walked labels it separated nothing: BUY means of 5.68 / 5.32 /
5.56 and win rates of 23.4 / 23.8 / 25.1 across its three buckets, flat within
noise. A fixed window cannot tell "fired twice in a fortnight" from "has fired
every week for two months", and it is the second that a persistence feature is
for.

``seq_in_consec_weeks`` is a run length: contiguous Monday-anchored weeks,
ending with this signal's own week, each containing at least one signal of the
same type for this ticker. A missed week resets it. Minimum 1 -- the signal's
own week always counts, so there is no zero.

A rename keeps the 0052 history honest: that migration really did add a column
called ``seq_in_2w``, and this one records that it was superseded rather than
pretending it never existed.

VALUES ARE NOT MIGRATED. The old numbers are two-week window counts and are
not run lengths; carrying them across would silently mislabel every row. The
column is set to NULL and repopulated by the next signal_enrichment rebuild,
which is the only thing that can compute the new quantity.

Revision ID: 0056_seq_in_consec_weeks
Revises: 0055_signal_label_forward_walk
"""
from alembic import op

revision = "0056_seq_in_consec_weeks"
down_revision = "0055_signal_label_forward_walk"
branch_labels = None
depends_on = None

TABLE = "signal_enrichment"
OLD = "seq_in_2w"
NEW = "seq_in_consec_weeks"

NEW_COMMENT = (
    "Run length of CONTIGUOUS Monday-anchored weeks, ending with this "
    "signal''s own week, each containing at least one signal of the SAME "
    "TYPE for this ticker. A missed week resets it. Minimum 1 -- the "
    "signal''s own week always counts. Causal: earlier weeks lie entirely "
    "before signal_date, and the current week counts only signals on or "
    "before it. Replaced seq_in_2w, a fixed two-week window whose buckets "
    "were statistically indistinguishable."
)

OLD_COMMENT = "As seq_in_week, over [previous Monday, this signal_date]."


def upgrade() -> None:
    op.alter_column(TABLE, OLD, new_column_name=NEW)
    # The stored values are window counts, not run lengths. Keeping them would
    # be worse than having none: they look plausible and are wrong.
    op.execute(f"UPDATE {TABLE} SET {NEW} = NULL")
    op.execute(f"COMMENT ON COLUMN {TABLE}.{NEW} IS '{NEW_COMMENT}'")


def downgrade() -> None:
    op.alter_column(TABLE, NEW, new_column_name=OLD)
    op.execute(f"UPDATE {TABLE} SET {OLD} = NULL")
    op.execute(f"COMMENT ON COLUMN {TABLE}.{OLD} IS '{OLD_COMMENT}'")
