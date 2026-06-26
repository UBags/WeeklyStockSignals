"""V1 signal rotation primitive: current_signals → historical_signals.

A single CTE-driven SQL statement moves every row from
`current_signals` into `historical_signals`. Atomic by virtue of being
ONE statement — both DELETE and INSERT succeed together or neither
runs.

`archived_at` on each new historical row is set to NOW(). Because
PostgreSQL's NOW() returns `transaction_timestamp()` (fixed for the
whole transaction), every row inserted in this single rotation
statement shares the SAME `archived_at` — useful for audit queries
like "show me all signals archived in cycle 20260626-1500."

Caller controls the surrounding transaction.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection


_ROTATE_SQL = """
    WITH moved AS (
        DELETE FROM current_signals
        RETURNING
            ticker_id, timeframe, bar_timestamp, signal_type,
            regime_before, regime_after, price_at_signal,
            computed_at
    )
    INSERT INTO historical_signals (
        ticker_id, timeframe, bar_timestamp, signal_type,
        regime_before, regime_after, price_at_signal,
        computed_at, archived_at
    )
    SELECT
        ticker_id, timeframe, bar_timestamp, signal_type,
        regime_before, regime_after, price_at_signal,
        computed_at, NOW()
    FROM moved
"""


def rotate_current_to_historical(conn: Connection) -> int:
    """Atomically move all rows from current_signals to
    historical_signals, stamping each archived_at with NOW().

    Returns the number of rows moved. Zero if current_signals was
    already empty (no error, just a no-op).

    Implementation is a single statement (CTE with
    DELETE...RETURNING + INSERT...SELECT). Atomicity is guaranteed
    at the SQL level — both ops land or neither does.

    Caller controls the surrounding transaction. This function does
    not commit.
    """
    result = conn.execute(text(_ROTATE_SQL))
    # rowcount on the outer INSERT = number of rows inserted = number
    # of rows that came out of the DELETE...RETURNING CTE.
    return result.rowcount
