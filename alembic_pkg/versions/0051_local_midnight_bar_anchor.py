"""ohlcv_bars: re-stamp 1D bars to the local-midnight anchor.

A daily bar's timestamp was `datetime.combine(trading_date, time.min, tzinfo=UTC)`
— midnight UTC, the same instant for every exchange on earth. But every consumer
reads it back in the MARKET's zone (`.astimezone(zone).date()`,
`_monday_of(ts, tz=session.tz)`), and midnight-UTC only survives that round trip
for zones EAST of UTC:

    NSE  : 2026-07-13 00:00Z -> 05:30 IST, 2026-07-13   ✓ same day
    NYSE : 2026-07-13 00:00Z -> 20:00 ET,  2026-07-12   ✗ the Sunday before

So on NYSE every daily bar shifts back a day and every Monday bar files into the
previous week. Wrong week, wrong weekly OHLC, wrong EMA, wrong signal — with no
exception, no gap and no audit finding. The code fix (see the anchor rule in
domain/market_session.py) makes the timestamp the LOCAL midnight of the trading
date, which is invertible in both hemispheres. This migration brings the rows that
already exist into line with it.

IT IS A RE-STAMP, NOT A REBUILD. For NSE both conventions name the same local
trading date, so every derived layer — indicator state, enrichment, signals,
industry indices — is bit-identical either side of this migration. Nothing
downstream is recomputed because nothing downstream changes.

The guard `(bar_timestamp AT TIME ZONE 'UTC')::time = '00:00:00'` makes this
idempotent: rows already carrying a local-midnight anchor (18:30Z for IST) do not
match, and re-running is a no-op.

The exchange -> zone map is FROZEN here rather than imported from
domain.market_session. A migration is a statement about the database at one moment
in history; if it imports live application code, a future refactor silently
rewrites the past. The duplication is the point.
"""
from alembic import op
import sqlalchemy as sa


revision = "0051_local_midnight_bar_anchor"
down_revision = "0050_drop_tickers_tz"
branch_labels = None
depends_on = None


# Frozen snapshot of domain.market_session's specs, deliberately not imported.
_EXCHANGE_TZ = {
    "NSE": "Asia/Kolkata",
    "BSE": "Asia/Kolkata",
    "NYSE": "America/New_York",
    "NASDAQ": "America/New_York",
    "LSE": "Europe/London",
}


def upgrade() -> None:
    conn = op.get_bind()
    for exchange, tz in _EXCHANGE_TZ.items():
        res = conn.execute(
            sa.text("""
                UPDATE ohlcv_bars o
                   SET bar_timestamp =
                       ((o.bar_timestamp AT TIME ZONE 'UTC')::date)::timestamp
                       AT TIME ZONE :tz
                  FROM tickers t
                 WHERE t.ticker_id = o.ticker_id
                   AND t.exchange = :ex
                   AND o.timeframe = '1D'
                   AND (o.bar_timestamp AT TIME ZONE 'UTC')::time = '00:00:00'
            """),
            {"ex": exchange, "tz": tz},
        )
        if res.rowcount:
            print(f"  {exchange}: re-stamped {res.rowcount} daily bar(s) to {tz} midnight")


def downgrade() -> None:
    conn = op.get_bind()
    for exchange, tz in _EXCHANGE_TZ.items():
        conn.execute(
            sa.text("""
                UPDATE ohlcv_bars o
                   SET bar_timestamp =
                       ((o.bar_timestamp AT TIME ZONE :tz)::date)::timestamp
                       AT TIME ZONE 'UTC'
                  FROM tickers t
                 WHERE t.ticker_id = o.ticker_id
                   AND t.exchange = :ex
                   AND o.timeframe = '1D'
                   AND (o.bar_timestamp AT TIME ZONE :tz)::time = '00:00:00'
            """),
            {"ex": exchange, "tz": tz},
        )
