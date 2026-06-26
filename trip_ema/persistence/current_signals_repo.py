"""Repository for current_signals — V1 this-cycle's signals.

PK is `ticker_id` (max one row per ticker). Rotated wholesale into
historical_signals at the start of each cycle's signal stage; then
this cycle's derived signals are inserted fresh.

App-level validation:
  - signal_type in SIGNAL_TYPES
  - regime_before, regime_after in REGIME_VALUES
  - regime_before != regime_after
  - price_at_signal > 0

These are also enforced by DB CHECK constraints; the repo-side
checks give better error messages and fail fast before hitting
the DB.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..domain.signal_event import (
    REGIME_VALUES,
    SIGNAL_TYPES,
    SignalEvent,
)


# Column order — must match _row_to_signal unpacking.
_SELECT_COLS = (
    "ticker_id, timeframe, bar_timestamp, signal_type, "
    "regime_before, regime_after, price_at_signal, computed_at"
)


_UPSERT_SQL = """
    INSERT INTO current_signals (
        ticker_id, timeframe, bar_timestamp, signal_type,
        regime_before, regime_after, price_at_signal, computed_at
    ) VALUES (
        :tid, :tf, :bt, :st,
        :rb, :ra, :p, :ca
    )
    ON CONFLICT (ticker_id)
    DO UPDATE SET
        timeframe = EXCLUDED.timeframe,
        bar_timestamp = EXCLUDED.bar_timestamp,
        signal_type = EXCLUDED.signal_type,
        regime_before = EXCLUDED.regime_before,
        regime_after = EXCLUDED.regime_after,
        price_at_signal = EXCLUDED.price_at_signal,
        computed_at = EXCLUDED.computed_at
"""


class CurrentSignalsRepository:
    """V1 current_signals CRUD.

    `insert_many` is idempotent on `ticker_id` — re-running with the
    same ticker_id replaces the row (this matches the "max one signal
    per ticker per cycle" invariant, and is robust to per-ticker
    retries inside a cycle).
    """

    def __init__(self, conn: Connection):
        self._conn = conn

    # ---------- writes ---------- #

    def insert_many(self, signals: list[SignalEvent]) -> int:
        """Upsert N signals in one round-trip. Validates each first;
        raises on the first invalid row before any write. Returns
        the count written (== len(signals)).

        Idempotent on `ticker_id` PK — repeated ticker_ids in the
        input list result in only the last one being kept (later
        executemany rows overwrite earlier ones at the DB level).
        """
        if not signals:
            return 0
        for s in signals:
            self._validate(s)
        self._conn.execute(
            text(_UPSERT_SQL),
            [self._params(s) for s in signals],
        )
        return len(signals)

    def truncate(self) -> None:
        """Wipe all rows. Used by rotation (Step 4) after the
        INSERT...SELECT into historical_signals lands.

        TRUNCATE is faster than DELETE for whole-table wipes and
        resets the underlying storage; current_signals has no
        SERIAL so there's no sequence to RESTART.
        """
        self._conn.execute(text("TRUNCATE TABLE current_signals"))

    # ---------- reads ---------- #

    def get_for_ticker(self, ticker_id: int) -> SignalEvent | None:
        """Get this cycle's signal for one ticker, if any.
        Returns None when the ticker didn't change regime this cycle.
        """
        row = self._conn.execute(
            text(f"""
                SELECT {_SELECT_COLS}
                FROM current_signals
                WHERE ticker_id = :tid
            """),
            {"tid": ticker_id},
        ).fetchone()
        return _row_to_signal(row) if row else None

    def list_all(self) -> list[SignalEvent]:
        """All current-cycle signals. Used by the email composer
        (Step 7) and the SignalCache rebuild (Step 5). Typically
        < 100 rows."""
        rows = self._conn.execute(text(f"""
            SELECT {_SELECT_COLS}
            FROM current_signals
            ORDER BY ticker_id
        """))
        return [_row_to_signal(r) for r in rows]

    def count(self) -> int:
        """Total rows. Cheap COUNT(*) — used by notify_subscribers
        (Step 9) to short-circuit when current_signals is empty."""
        return self._conn.execute(
            text("SELECT COUNT(*) FROM current_signals"),
        ).scalar_one()

    # ---------- internals ---------- #

    @staticmethod
    def _validate(s: SignalEvent) -> None:
        if s.signal_type not in SIGNAL_TYPES:
            raise ValueError(
                f"signal_type must be in {SIGNAL_TYPES}, "
                f"got {s.signal_type!r}"
            )
        if s.regime_before not in REGIME_VALUES:
            raise ValueError(
                f"regime_before must be in {REGIME_VALUES}, "
                f"got {s.regime_before}"
            )
        if s.regime_after not in REGIME_VALUES:
            raise ValueError(
                f"regime_after must be in {REGIME_VALUES}, "
                f"got {s.regime_after}"
            )
        if s.regime_before == s.regime_after:
            raise ValueError(
                f"regime_before == regime_after ({s.regime_before}) "
                f"— a signal is a regime CHANGE by definition"
            )
        if not (s.price_at_signal > 0):
            raise ValueError(
                f"price_at_signal must be > 0, "
                f"got {s.price_at_signal}"
            )

    @staticmethod
    def _params(s: SignalEvent) -> dict:
        return {
            "tid": s.ticker_id,
            "tf": s.timeframe,
            "bt": s.bar_timestamp,
            "st": s.signal_type,
            "rb": s.regime_before,
            "ra": s.regime_after,
            "p": s.price_at_signal,
            "ca": s.computed_at,
        }


def _row_to_signal(row) -> SignalEvent:
    """Unpack one SELECT row into a SignalEvent. Column order
    matches `_SELECT_COLS`."""
    return SignalEvent(
        ticker_id=int(row[0]),
        timeframe=row[1],
        bar_timestamp=row[2],
        signal_type=row[3],
        regime_before=int(row[4]),
        regime_after=int(row[5]),
        price_at_signal=float(row[6]),
        computed_at=row[7],
    )
