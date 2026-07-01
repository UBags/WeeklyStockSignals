"""UpdateRunInProgressRepository — thin wrapper around the
`update_run_in_progress` marker table.

Row presence == in-progress. There is deliberately no `finish()`
method that sets a `finished_at` column — completion is a DELETE.
See migration 0031 for the design rationale.

Public API:
  - start(exchange, kind, pid=None) -> int
      INSERT a row. Returns the new id. Caller (RunDispatcher) sets
      pid after Popen.pid is known.
  - set_pid(id, pid)
      Backfill the pid once the subprocess has started.
  - delete(exchange)
      DELETE the row for the given exchange. No-op if absent.
  - list_all() -> list[UpdateRunInProgressRecord]
      Enumerate current rows. Used by GUI + /queue-snapshot API.
  - get(exchange) -> UpdateRunInProgressRecord | None
      Single-row lookup for the reset-in-progress API.
  - is_any_in_progress() -> bool
      SELECT count(*) > 0. Used by RunDispatcher as its gate.
  - clear_all()
      DELETE all rows. Called once at service startup so orphan
      rows from a previous crash don't wedge the dispatcher.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection


@dataclass(frozen=True, slots=True)
class UpdateRunInProgressRecord:
    """Immutable snapshot of one row."""
    id: int
    exchange: str
    kind: str            # 'intraday' | 'eod'
    started_at: datetime
    pid: int | None


class UpdateRunInProgressRepository:
    """Reads + writes for the update_run_in_progress marker table."""

    def __init__(self, conn: Connection):
        self._conn = conn

    # ---------- writes ---------- #

    def start(
        self, *,
        exchange: str,
        kind: str,
        pid: int | None = None,
    ) -> int:
        """INSERT a marker row. Returns the new id.

        Raises sqlalchemy.exc.IntegrityError (unique violation on
        ux_uripc_exchange) if another row already exists for the
        same exchange. Caller must ensure the dispatcher's gating
        logic prevents this — if it fires, it's a bug worth
        surfacing loud rather than silently swallowing.
        """
        result = self._conn.execute(
            text("""
                INSERT INTO update_run_in_progress
                    (exchange, kind, pid)
                VALUES
                    (:exchange, :kind, :pid)
                RETURNING id
            """),
            {"exchange": exchange, "kind": kind, "pid": pid},
        )
        row_id: int = result.scalar_one()
        return row_id

    def set_pid(self, row_id: int, pid: int) -> None:
        """Backfill the pid column once Popen.pid is available."""
        self._conn.execute(
            text("""
                UPDATE update_run_in_progress
                   SET pid = :pid
                 WHERE id = :id
            """),
            {"pid": pid, "id": row_id},
        )

    def delete(self, exchange: str) -> int:
        """DELETE the row for `exchange`. Returns number of rows
        deleted (0 or 1)."""
        result = self._conn.execute(
            text("""
                DELETE FROM update_run_in_progress
                 WHERE exchange = :exchange
            """),
            {"exchange": exchange},
        )
        return result.rowcount or 0

    def clear_all(self) -> int:
        """DELETE all rows. Returns count. Called at service startup
        so orphan rows from a crashed prior process don't wedge the
        dispatcher's gate."""
        result = self._conn.execute(
            text("DELETE FROM update_run_in_progress"),
        )
        return result.rowcount or 0

    # ---------- reads ---------- #

    def is_any_in_progress(self) -> bool:
        """The dispatcher's gate — is any run in progress right now?"""
        result = self._conn.execute(
            text("SELECT COUNT(*) FROM update_run_in_progress"),
        )
        n: int = result.scalar_one()
        return n > 0

    def get(self, exchange: str) -> UpdateRunInProgressRecord | None:
        """Single-row lookup by exchange. Returns None if no row."""
        result = self._conn.execute(
            text("""
                SELECT id, exchange, kind, started_at, pid
                  FROM update_run_in_progress
                 WHERE exchange = :exchange
            """),
            {"exchange": exchange},
        )
        row = result.mappings().first()
        if row is None:
            return None
        return UpdateRunInProgressRecord(
            id=row["id"], exchange=row["exchange"],
            kind=row["kind"], started_at=row["started_at"],
            pid=row["pid"],
        )

    def list_all(self) -> list[UpdateRunInProgressRecord]:
        """Enumerate current rows. Ordered by started_at ASC."""
        result = self._conn.execute(
            text("""
                SELECT id, exchange, kind, started_at, pid
                  FROM update_run_in_progress
                 ORDER BY started_at ASC
            """),
        )
        return [
            UpdateRunInProgressRecord(
                id=row["id"], exchange=row["exchange"],
                kind=row["kind"], started_at=row["started_at"],
                pid=row["pid"],
            )
            for row in result.mappings().all()
        ]
