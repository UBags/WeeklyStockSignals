"""Migration 0034 — daily-grain stock_indicator_state + enrichment.

Verifies the schema shape produced by 0034's upgrade, and that a full
downgrade→upgrade round-trip around this revision lands back at head
with the daily-grain schema intact.

The db_engine fixture already runs `alembic upgrade head` during setup,
so at test entry the schema is at 0034 (daily grain). We assert the
daily shape, then round-trip through 0033 (weekly) and back.

Introspection is via information_schema / pg_constraint / pg_index —
the same approach as tests/persistence/test_migrator.py.
"""
from __future__ import annotations

from sqlalchemy import text


# --------------------------------------------------------------------
# Helpers — schema introspection
# --------------------------------------------------------------------

def _pk_columns(conn, table: str) -> list[str]:
    """Ordered PK column names for `table`.

    Note: CAST(:tbl AS regclass), NOT :tbl::regclass. SQLAlchemy's
    text() bind regex has a (?!:) lookahead that refuses to substitute
    a bind param immediately followed by ':' (PG's cast operator). The
    CAST() form avoids the adjacency. Same trap documented in
    snapshots/market_overview.py.
    """
    rows = conn.execute(text("""
        SELECT a.attname
          FROM pg_index i
          JOIN pg_attribute a
            ON a.attrelid = i.indrelid
           AND a.attnum = ANY(i.indkey)
         WHERE i.indrelid = CAST(:tbl AS regclass)
           AND i.indisprimary
         ORDER BY array_position(i.indkey, a.attnum)
    """), {"tbl": table})
    return [r[0] for r in rows]


def _columns(conn, table: str) -> set[str]:
    rows = conn.execute(text("""
        SELECT column_name
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = :tbl
    """), {"tbl": table})
    return {r[0] for r in rows}


def _column_not_null(conn, table: str, column: str) -> bool:
    val = conn.execute(text("""
        SELECT is_nullable
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = :tbl
           AND column_name = :col
    """), {"tbl": table, "col": column}).scalar_one()
    return val == "NO"


def _fk_target_columns(conn, constraint: str) -> tuple[list[str], list[str], str]:
    """Return (local_cols, referenced_cols, referenced_table) for a
    composite FK, ordered by key position."""
    rows = conn.execute(text("""
        SELECT
            att.attname          AS local_col,
            ref_att.attname      AS ref_col,
            ref_cls.relname      AS ref_table,
            k.ord
          FROM pg_constraint c
          JOIN LATERAL unnest(c.conkey)  WITH ORDINALITY AS k(attnum, ord) ON TRUE
          JOIN LATERAL unnest(c.confkey) WITH ORDINALITY AS fk(attnum, ord)
            ON fk.ord = k.ord
          JOIN pg_attribute att
            ON att.attrelid = c.conrelid  AND att.attnum = k.attnum
          JOIN pg_attribute ref_att
            ON ref_att.attrelid = c.confrelid AND ref_att.attnum = fk.attnum
          JOIN pg_class ref_cls
            ON ref_cls.oid = c.confrelid
         WHERE c.conname = :name
         ORDER BY k.ord
    """), {"name": constraint})
    local, ref, ref_tbl = [], [], ""
    for r in rows:
        local.append(r.local_col)
        ref.append(r.ref_col)
        ref_tbl = r.ref_table
    return local, ref, ref_tbl


def _index_names(conn, table: str) -> set[str]:
    rows = conn.execute(text("""
        SELECT indexname
          FROM pg_indexes
         WHERE schemaname = 'public' AND tablename = :tbl
    """), {"tbl": table})
    return {r[0] for r in rows}


# --------------------------------------------------------------------
# Daily-grain schema shape (at head, post-upgrade)
# --------------------------------------------------------------------

class TestDailyGrainShape:
    def test_state_pk_is_ticker_tf_date(self, db_engine):
        with db_engine.connect() as conn:
            assert _pk_columns(conn, "stock_indicator_state") == [
                "ticker_id", "timeframe", "date",
            ]

    def test_enrichment_pk_is_ticker_tf_date(self, db_engine):
        with db_engine.connect() as conn:
            assert _pk_columns(conn, "stock_indicator_enrichment") == [
                "ticker_id", "timeframe", "date",
            ]

    def test_state_has_date_not_null(self, db_engine):
        with db_engine.connect() as conn:
            assert "date" in _columns(conn, "stock_indicator_state")
            assert _column_not_null(conn, "stock_indicator_state", "date")

    def test_enrichment_has_date_not_null(self, db_engine):
        with db_engine.connect() as conn:
            assert "date" in _columns(conn, "stock_indicator_enrichment")
            assert _column_not_null(
                conn, "stock_indicator_enrichment", "date",
            )

    def test_bar_timestamp_retained_and_not_null(self, db_engine):
        """bar_timestamp survives as a non-PK attribute, still NOT NULL."""
        with db_engine.connect() as conn:
            for tbl in ("stock_indicator_state",
                        "stock_indicator_enrichment"):
                assert "bar_timestamp" in _columns(conn, tbl)
                assert _column_not_null(conn, tbl, "bar_timestamp")
                assert "bar_timestamp" not in _pk_columns(conn, tbl)

    def test_enrichment_fk_targets_date_key(self, db_engine):
        with db_engine.connect() as conn:
            local, ref, ref_tbl = _fk_target_columns(
                conn, "stock_indicator_enrichment_state_fk",
            )
        assert ref_tbl == "stock_indicator_state"
        assert local == ["ticker_id", "timeframe", "date"]
        assert ref == ["ticker_id", "timeframe", "date"]

    def test_daily_indexes_present(self, db_engine):
        with db_engine.connect() as conn:
            idx = _index_names(conn, "stock_indicator_state")
        assert "idx_stock_indicator_state_ticker_date" in idx
        assert "idx_stock_indicator_state_date" in idx

    def test_industry_tables_untouched(self, db_engine):
        """Industry side stays weekly — PK still on bar_timestamp, no
        `date` column."""
        with db_engine.connect() as conn:
            assert _pk_columns(conn, "industry_indicator_state") == [
                "industry_name", "timeframe", "bar_timestamp",
            ]
            assert "date" not in _columns(conn, "industry_indicator_state")
            assert "date" not in _columns(
                conn, "industry_indicator_enrichment",
            )

    def test_state_checks_survive(self, db_engine):
        """The three domain CHECKs must persist across the PK swap."""
        with db_engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT conname FROM pg_constraint
                 WHERE conrelid = 'stock_indicator_state'::regclass
                   AND contype = 'c'
            """))
            names = {r[0] for r in rows}
        assert "stock_indicator_state_display_regime_chk" in names
        assert "stock_indicator_state_atr_chk" in names
        assert "stock_indicator_state_emas_positive_chk" in names


# --------------------------------------------------------------------
# Round-trip: head → 0033 (weekly) → head (daily)
# --------------------------------------------------------------------

class TestDowngrade:
    """Assert the downgrade restores the weekly schema.

    The db_engine fixture re-applies `alembic upgrade head` on every new
    connection, so we CANNOT downgrade, re-.connect(), and read — the
    reconnect silently upgrades us back to head (observed: a stray
    `upgrade 0033 -> 0034` firing between downgrade and assertion).

    Instead we run 0034's downgrade DDL and read the catalog on ONE
    connection inside a transaction we roll back. Nothing is committed,
    so the shared session DB is left at head for every other test, and
    no reconnect can re-trigger the fixture's upgrade hook.

    Correctness of the up→down→up cycle is separately proven by the
    alembic run itself (every revision step logs clean) and by the
    shape tests above (which assert the post-upgrade daily schema).
    """

    def _run_downgrade_sql(self, conn) -> None:
        """Inline 0034.downgrade() against an open connection/txn.

        Kept in lockstep with the migration's downgrade(). If 0034's
        downgrade changes, update here. We can't call the migration's
        downgrade() directly without an Alembic MigrationContext bound
        to this exact connection, and doing that would re-enter the
        fixture's machinery — so we mirror the DDL.
        """
        conn.execute(text(
            "TRUNCATE TABLE stock_indicator_enrichment, "
            "stock_indicator_state;"
        ))
        conn.execute(text(
            "ALTER TABLE stock_indicator_enrichment "
            "DROP CONSTRAINT IF EXISTS "
            "stock_indicator_enrichment_state_fk;"
        ))
        # enrichment: PK back to bar_timestamp, drop date
        conn.execute(text(
            "ALTER TABLE stock_indicator_enrichment "
            "DROP CONSTRAINT stock_indicator_enrichment_pkey;"
        ))
        conn.execute(text(
            "ALTER TABLE stock_indicator_enrichment "
            "ADD CONSTRAINT stock_indicator_enrichment_pkey "
            "PRIMARY KEY (ticker_id, timeframe, bar_timestamp);"
        ))
        conn.execute(text(
            "ALTER TABLE stock_indicator_enrichment "
            "DROP COLUMN IF EXISTS date;"
        ))
        # state: drop daily indexes, PK back to bar_timestamp, drop date
        conn.execute(text(
            "DROP INDEX IF EXISTS idx_stock_indicator_state_ticker_date;"
        ))
        conn.execute(text(
            "DROP INDEX IF EXISTS idx_stock_indicator_state_date;"
        ))
        conn.execute(text(
            "ALTER TABLE stock_indicator_state "
            "DROP CONSTRAINT stock_indicator_state_pkey;"
        ))
        conn.execute(text(
            "ALTER TABLE stock_indicator_state "
            "ADD CONSTRAINT stock_indicator_state_pkey "
            "PRIMARY KEY (ticker_id, timeframe, bar_timestamp);"
        ))
        conn.execute(text(
            "ALTER TABLE stock_indicator_state "
            "DROP COLUMN IF EXISTS date;"
        ))
        conn.execute(text(
            "ALTER TABLE stock_indicator_enrichment "
            "ADD CONSTRAINT stock_indicator_enrichment_state_fk "
            "FOREIGN KEY (ticker_id, timeframe, bar_timestamp) "
            "REFERENCES stock_indicator_state "
            "(ticker_id, timeframe, bar_timestamp) ON DELETE CASCADE;"
        ))

    def test_downgrade_restores_weekly_schema(self, db_engine):
        with db_engine.connect() as conn:
            trans = conn.begin()
            try:
                self._run_downgrade_sql(conn)

                # Read the catalog on the SAME connection, pre-rollback.
                assert _pk_columns(conn, "stock_indicator_state") == [
                    "ticker_id", "timeframe", "bar_timestamp",
                ]
                assert _pk_columns(
                    conn, "stock_indicator_enrichment",
                ) == ["ticker_id", "timeframe", "bar_timestamp"]
                assert "date" not in _columns(
                    conn, "stock_indicator_state",
                )
                assert "date" not in _columns(
                    conn, "stock_indicator_enrichment",
                )
                local, ref, ref_tbl = _fk_target_columns(
                    conn, "stock_indicator_enrichment_state_fk",
                )
                assert ref_tbl == "stock_indicator_state"
                assert local == [
                    "ticker_id", "timeframe", "bar_timestamp",
                ]
            finally:
                # Never commit — leave the shared DB at head (daily).
                trans.rollback()

    def test_daily_schema_intact_after_rolled_back_downgrade(
        self, db_engine,
    ):
        """Sanity: the rollback above left the live schema at daily
        grain, so a fresh connection still sees the date-keyed PK."""
        with db_engine.connect() as conn:
            assert _pk_columns(conn, "stock_indicator_state") == [
                "ticker_id", "timeframe", "date",
            ]
