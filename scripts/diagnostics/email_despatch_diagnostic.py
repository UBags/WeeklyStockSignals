#!/usr/bin/env python3
"""Why did / didn't subscriber N get an email?

REWRITTEN for V1. The previous version was built on a flow that no longer
exists, and would have MISLED you on every run:

  * it read `signal_events`, dropped in migration 0030. The V1 notifier reads
    `current_signals` (the live set) -- see orchestrator/notify_subscribers.py.

  * it deduped with
        NOT EXISTS (SELECT 1 FROM notification_log WHERE event_id = e.event_id)
    but notification_log.event_id is LEGACY and NULL for every V1 write
    (NotificationLogEntry: "event_id (legacy, NULL for V1 writes)"). That
    predicate is therefore ALWAYS TRUE -- the old tool reported EVERY signal as
    un-notified, whether or not the email had gone out.
    The real V1 dedup key is (signal_ticker_id, signal_timeframe,
    signal_bar_timestamp).

  * it filtered `bar_timestamp >= NOW() - INTERVAL '7 days'`. bar_timestamp is
    the IST-Monday WEEK anchor shared by every day of the week, so that means
    "signals whose WEEK started in the last 7 days", not "signals from the last
    7 days". historical_signals.signal_date is the daily grain -- use it.

Usage:  python email_despatch_diagnostic.py [SUBSCRIBER_ID]
"""
from __future__ import annotations

import sys

from sqlalchemy import text

from trip_ema.config.loader import load_config
from trip_ema.persistence.connection import create_engine_from_config

SUB = int(sys.argv[1]) if len(sys.argv) > 1 else 1


def main() -> None:
    engine = create_engine_from_config(load_config("config.yaml"))
    with engine.connect() as conn:
        print("=" * 72)
        print("1. current_signals — what the notifier would send RIGHT NOW")
        print("=" * 72)
        r = conn.execute(text("""
            SELECT COUNT(*) AS n,
                   MIN(bar_timestamp) AS min_bar,
                   MAX(bar_timestamp) AS max_bar,
                   MAX(computed_at)   AS last_computed
              FROM current_signals
        """)).first()
        print(f"  rows in current_signals : {r.n:>8,}")
        print(f"  week anchor range       : {r.min_bar} .. {r.max_bar}")
        print(f"  last computed_at        : {r.last_computed}")
        if r.n == 0:
            print("  -> nothing to send. The cycle emits current_signals only")
            print("     on a regime FLIP; a quiet day legitimately has zero.")

        print()
        print("=" * 72)
        print("2. current_signals by type")
        print("=" * 72)
        for row in conn.execute(text("""
            SELECT signal_type, COUNT(*) AS n
              FROM current_signals
             GROUP BY signal_type
             ORDER BY n DESC
        """)):
            print(f"  {row.signal_type:<18} {row.n:>6}")

        print()
        print("=" * 72)
        print("3. historical_signals — recent activity (by signal_date)")
        print("=" * 72)
        # signal_date, NOT bar_timestamp: the latter is the week anchor.
        h = conn.execute(text("""
            SELECT
              COUNT(*) FILTER (
                  WHERE signal_date >= CURRENT_DATE - 7)  AS last_7d,
              COUNT(*) FILTER (
                  WHERE signal_date >= CURRENT_DATE - 30) AS last_30d,
              COUNT(*)          AS total,
              MAX(signal_date)  AS latest_signal_date
              FROM historical_signals
        """)).first()
        print(f"  signal_date >= 7d ago   : {h.last_7d:>8,}")
        print(f"  signal_date >= 30d ago  : {h.last_30d:>8,}")
        print(f"  total                   : {h.total:>8,}")
        print(f"  latest signal_date      : {h.latest_signal_date}")

        print()
        print("=" * 72)
        print(f"4. Subscriber {SUB}")
        print("=" * 72)
        s = conn.execute(text("""
            SELECT subscriber_id, email, is_active, last_notified_at
              FROM subscribers WHERE subscriber_id = :s
        """), {"s": SUB}).first()
        if s is None:
            print(f"  !! no subscriber with subscriber_id = {SUB}")
            return
        print(f"  email            : {s.email}")
        print(f"  is_active        : {s.is_active}"
              f"{'   <-- INACTIVE: no email will be sent' if not s.is_active else ''}")
        print(f"  last_notified_at : {s.last_notified_at}")

        print()
        print("=" * 72)
        print(f"5. What subscriber {SUB} has already been sent (V1 dedup key)")
        print("=" * 72)
        # The V1 key is (signal_ticker_id, signal_timeframe,
        # signal_bar_timestamp). event_id is legacy and NULL on every V1 row --
        # joining on it (as the old diagnostic did) matches nothing, ever.
        sent = conn.execute(text("""
            SELECT COUNT(*) AS n, MAX(last_sent_at) AS last_sent
              FROM notification_log
             WHERE subscriber_id = :s
        """), {"s": SUB}).first()
        print(f"  notification_log rows : {sent.n:>8,}")
        print(f"  last_sent_at          : {sent.last_sent}")

        print()
        print("=" * 72)
        print(f"6. current_signals NOT yet logged for subscriber {SUB}")
        print("=" * 72)
        pending = conn.execute(text("""
            SELECT t.symbol, t.exchange, cs.signal_type,
                   cs.bar_timestamp, cs.computed_at
              FROM current_signals cs
              JOIN tickers t ON t.ticker_id = cs.ticker_id
             WHERE NOT EXISTS (
                       SELECT 1 FROM notification_log nl
                        WHERE nl.subscriber_id        = :s
                          AND nl.signal_ticker_id     = cs.ticker_id
                          AND nl.signal_timeframe     = cs.timeframe
                          AND nl.signal_bar_timestamp = cs.bar_timestamp
                   )
             ORDER BY cs.computed_at DESC
             LIMIT 30
        """), {"s": SUB}).all()
        print(f"  {len(pending)} signal(s) not yet in notification_log:")
        for p in pending[:20]:
            print(f"    {p.exchange}:{p.symbol:<14} {p.signal_type:<16}"
                  f"  week={p.bar_timestamp.date()}"
                  f"  computed={p.computed_at.date()}")
        if len(pending) > 20:
            print(f"    ... and {len(pending) - 20} more")


if __name__ == "__main__":
    main()
