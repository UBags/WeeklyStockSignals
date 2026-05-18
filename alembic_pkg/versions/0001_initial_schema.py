"""initial schema — squash of legacy migrations 001-010

Squashed equivalent of `migrations/legacy/*.sql` (Phase 1-11). The
actual DDL lives in `alembic_pkg/sql/0001_initial_schema.sql` and is
executed verbatim from there; we do it this way rather than
hand-translating to the `op.*` API because:

  - The raw SQL is human-readable; the op.* form would obscure the
    Postgres-specific features (JSONB, partitioning, NULLS NOT
    DISTINCT) that the legacy migrations rely on.
  - When something breaks in production, "go read the SQL" is a
    more direct debugging path than "read the Alembic ops then
    look up what they emit."
  - Preserves the exact CHECK constraints, comments, and column
    ordering from the working production schema.

Coexistence with legacy migrator (transitional, until all deployed
databases are upgraded):

  - A pre-Phase-12 database has `schema_migrations` populated with
    versions 001-010. On first startup, the alembic_bridge detects
    this and stamps `0001_initial_schema` WITHOUT running the
    upgrade() function. See trip_ema.persistence.alembic_bridge.
  - A fresh database has neither table. `alembic upgrade head`
    creates everything via upgrade() below, then any future
    revisions (0002+) chain off this one.

Revision ID: 0001_initial_schema
Revises: (none — this is the base)
Create Date: 2026-05-18
"""

from __future__ import annotations

from pathlib import Path

from alembic import op


# revision identifiers, used by Alembic.
revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


_SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "0001_initial_schema.sql"


def upgrade() -> None:
    """Apply the full Phase 1-11 schema in one transaction.

    Reads the SQL from a sibling file rather than embedding it
    inline; the file is checked in alongside this revision so
    `alembic upgrade head` against a fresh DB produces the same
    schema as the legacy migrator's 001-010 produced.

    Note: this function MUST NOT run against a database where the
    legacy migrator has already applied 001-010. The alembic_bridge
    enforces that by stamping without calling upgrade(); the only
    way to reach this function is via `alembic upgrade head` on a
    fresh DB, which has neither `schema_migrations` nor any of the
    tables we're about to create.
    """
    sql = _SQL_FILE.read_text(encoding="utf-8")
    # Use raw connection cursor — same pattern as the legacy
    # migrator — so `%` in modulus expressions doesn't get
    # interpreted as a parameter placeholder.
    conn = op.get_bind()
    raw_conn = conn.connection
    with raw_conn.cursor() as cur:
        cur.execute(sql)


def downgrade() -> None:
    """No-op. Downgrading the initial schema means dropping every
    table the system relies on. If you genuinely need to do that,
    drop the database; don't try to do it through Alembic.

    The Alembic CLI will accept `alembic downgrade base` and report
    success without altering the schema — intentional, so CI flows
    that exercise full upgrade/downgrade cycles don't fail."""
    pass
