"""Email dispatch diagnostic. Answers:
  1. How many signal_events match the new 7d bar_timestamp filter?
  2. What's the most recent bar_timestamp in signal_events?
  3. Has the notification_log received anything recently?
  4. What does find_pending_for_subscriber actually return RIGHT NOW for sub 1?
  5. Subscriber 1's transport config — Brevo or SMTP or null?
"""
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine, text
import yaml

cfg = yaml.safe_load(open("../../config.yaml"))
db_cfg = cfg["database"]
url = (
    f"postgresql://{db_cfg.get('user', 'uddipan')}:"
    f"{db_cfg.get('password', '')}@"
    f"{db_cfg.get('host', 'localhost')}:"
    f"{db_cfg.get('port', 5432)}/"
    f"{db_cfg['database']}"
)
print(f"Connecting to: {url.replace(db_cfg.get('password',''), '***')}\n")
eng = create_engine(url)

now = datetime.now(timezone.utc)

with eng.connect() as conn:
    # ---- 1. signal_events distribution by bar_timestamp recency ----
    print("=" * 70)
    print("1. signal_events distribution by bar_timestamp")
    print("=" * 70)
    rows = conn.execute(text("""
        SELECT
          COUNT(*) FILTER (WHERE bar_timestamp >= NOW() - INTERVAL '7 days')  AS last_7d,
          COUNT(*) FILTER (WHERE bar_timestamp >= NOW() - INTERVAL '14 days') AS last_14d,
          COUNT(*) FILTER (WHERE bar_timestamp >= NOW() - INTERVAL '30 days') AS last_30d,
          COUNT(*) AS total,
          MAX(bar_timestamp) AS max_bar_ts,
          MIN(bar_timestamp) AS min_bar_ts
        FROM signal_events
    """)).first()
    print(f"  Total events:               {rows.total:>10,}")
    print(f"  bar_timestamp >= 7d ago:    {rows.last_7d:>10,}   <-- new filter")
    print(f"  bar_timestamp >= 14d ago:   {rows.last_14d:>10,}")
    print(f"  bar_timestamp >= 30d ago:   {rows.last_30d:>10,}")
    print(f"  Max bar_timestamp: {rows.max_bar_ts}")
    print(f"  Min bar_timestamp: {rows.min_bar_ts}")

    # ---- 2. notification_log activity ----
    print()
    print("=" * 70)
    print("2. notification_log activity")
    print("=" * 70)
    rows = conn.execute(text("""
        SELECT
          COUNT(*) AS total_rows,
          MAX(last_sent_at) AS last_sent_at,
          COUNT(DISTINCT subscriber_id) AS n_subscribers,
          COUNT(DISTINCT event_id) AS n_events
        FROM notification_log
    """)).first()
    print(f"  Total rows: {rows.total_rows}")
    print(f"  Last sent at: {rows.last_sent_at}")
    print(f"  Subscribers ever notified: {rows.n_subscribers}")
    print(f"  Events ever notified about: {rows.n_events}")

    # ---- 3. subscriber 1 config ----
    print()
    print("=" * 70)
    print("3. Subscriber 1 config")
    print("=" * 70)
    sub = conn.execute(text("""
        SELECT subscriber_id, email, transport, active, schedule_cron, last_dispatched_at
        FROM subscribers
        WHERE subscriber_id = 1
    """)).first()
    if sub is None:
        print("  No subscriber with id=1!")
    else:
        for k in ("subscriber_id", "email", "transport", "active",
                 "schedule_cron", "last_dispatched_at"):
            try:
                print(f"  {k}: {getattr(sub, k)}")
            except AttributeError:
                pass  # column may not exist

    # ---- 4. simulate find_pending_for_subscriber for sub 1 ----
    print()
    print("=" * 70)
    print("4. Pending events for subscriber 1 RIGHT NOW")
    print("=" * 70)
    rows = conn.execute(text("""
        SELECT s.symbol, t.exchange, e.bar_timestamp, e.computed_at,
               e.signal_type, e.event_id
        FROM signal_events e
        JOIN tickers t ON t.ticker_id = e.ticker_id
        JOIN tickers s ON s.ticker_id = e.ticker_id
        WHERE e.computed_at >= NOW() - INTERVAL '24 hours'
          AND e.bar_timestamp >= NOW() - INTERVAL '7 days'
          AND NOT EXISTS (
              SELECT 1 FROM notification_log nl
              WHERE nl.event_id = e.event_id
                AND nl.subscriber_id = 1
                AND nl.notification_count >= 3
          )
        ORDER BY e.bar_timestamp DESC, e.computed_at DESC
        LIMIT 30
    """)).all()
    print(f"  {len(rows)} event(s) pass both filters and aren't capped:")
    for r in rows[:20]:
        print(f"    [{r.event_id:>7}] {r.exchange}:{r.symbol:<12} "
              f"bar={r.bar_timestamp.date()}  computed={r.computed_at.date()}  "
              f"type={r.signal_type}")
    if len(rows) > 20:
        print(f"    ... and {len(rows)-20} more")

    # ---- 5. recent emitted signal types ----
    print()
    print("=" * 70)
    print("5. Recent signal_events by signal_type (last 7 days bar_timestamp)")
    print("=" * 70)
    rows = conn.execute(text("""
        SELECT signal_type, COUNT(*) AS n
        FROM signal_events
        WHERE bar_timestamp >= NOW() - INTERVAL '7 days'
        GROUP BY signal_type
        ORDER BY n DESC
    """)).all()
    for r in rows:
        print(f"  {r.signal_type:<20} {r.n:>6}")

    # ---- 6. subscriptions for sub 1 ----
    print()
    print("=" * 70)
    print("6. Subscriber 1 subscriptions")
    print("=" * 70)
    rows = conn.execute(text("""
        SELECT subscription_id, ticker_id, signal_filter
        FROM subscriptions
        WHERE subscriber_id = 1
    """)).all()
    print(f"  {len(rows)} subscription(s):")
    for r in rows[:10]:
        print(f"    sub_id={r.subscription_id} ticker_id={r.ticker_id} "
              f"filter={r.signal_filter}")