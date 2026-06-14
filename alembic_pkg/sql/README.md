# `alembic_pkg/sql/` — Raw SQL bodies executed by Alembic revisions

Optional helper directory. When an Alembic revision is large enough that
inlining the DDL into the `upgrade()` function would obscure it, the SQL
can live in a `.sql` file here and the revision can do:

```python
from pathlib import Path
from alembic import op
from sqlalchemy import text

def upgrade():
    sql = (Path(__file__).parent.parent / "sql" / "0001_initial_schema.sql").read_text()
    op.execute(text(sql))
```

## Contents

| File | Used by |
|---|---|
| `0001_initial_schema.sql` | Body of the squashed initial revision (`versions/0001_initial_schema.py`) — reproduces what legacy migrations 001-010 collectively produced |

## Conventions

- File name matches its consuming revision's prefix (`0001_*`).
- File is bit-equivalent to the consuming revision's effect — if you
  edit one, edit the other in the same commit.
- This is **not** the legacy SQL archive; see `../../migrations/legacy/`
  for that. The two trees serve different purposes:
  - `alembic_pkg/sql/` is *runtime* — Alembic actually executes these.
  - `migrations/legacy/` is *parity reference* — applied only by the
    legacy-parity test fixture, never by the production service.
