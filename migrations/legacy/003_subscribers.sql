-- 003_subscribers.sql
-- Notification distribution: who wants what, sent on what cadence, and the
-- audit log of every batch we've delivered.

CREATE TABLE IF NOT EXISTS subscribers (
    subscriber_id           SERIAL       PRIMARY KEY,
    transport               VARCHAR(16)  NOT NULL
        CHECK (transport IN ('email', 'webhook', 'slack', 'sms')),
    address                 TEXT         NOT NULL,
    notify_cadence_minutes  INTEGER      NOT NULL
        CHECK (notify_cadence_minutes BETWEEN 15 AND 1440
               AND notify_cadence_minutes % 15 = 0),
    last_notified_at        TIMESTAMPTZ,
    is_active               BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_subscribers_active_due
    ON subscribers (last_notified_at) WHERE is_active = TRUE;

CREATE TABLE IF NOT EXISTS subscriptions (
    subscription_id   SERIAL       PRIMARY KEY,
    subscriber_id     INTEGER      NOT NULL REFERENCES subscribers(subscriber_id) ON DELETE CASCADE,
    ticker_id         INTEGER      REFERENCES tickers(ticker_id) ON DELETE CASCADE,
    -- NULL ticker_id => "all tickers"
    signal_filter     VARCHAR(64),
    -- NULL signal_filter => "all signals"; otherwise comma-separated list, e.g. 'BUY,SELL'
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- Postgres treats NULLs as distinct in unique constraints; use NULLS NOT DISTINCT
    -- (PG 15+) so (sub_id, NULL, NULL) only inserts once.
    CONSTRAINT subscriptions_unique UNIQUE NULLS NOT DISTINCT
        (subscriber_id, ticker_id, signal_filter)
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_by_subscriber
    ON subscriptions (subscriber_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_by_ticker
    ON subscriptions (ticker_id) WHERE ticker_id IS NOT NULL;

-- Notification log: append-only, partitioned by month for cheap pruning later
-- (we keep forever for now per spec, but partitioning gives us the option).
CREATE TABLE IF NOT EXISTS notification_log (
    log_id              BIGSERIAL    NOT NULL,
    subscriber_id       INTEGER      NOT NULL REFERENCES subscribers(subscriber_id) ON DELETE CASCADE,
    event_id            BIGINT       NOT NULL REFERENCES signal_events(event_id) ON DELETE CASCADE,
    notification_count  INTEGER      NOT NULL DEFAULT 1
        CHECK (notification_count >= 1),
    last_sent_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- Partition key must be part of PK in a partitioned table.
    PRIMARY KEY (log_id, last_sent_at)
) PARTITION BY RANGE (last_sent_at);

-- Per-subscriber-per-event uniqueness: enforced as a unique index that
-- spans partitions (allowed since PG 11+ when the partition key is included).
CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_log_dedup
    ON notification_log (subscriber_id, event_id, last_sent_at);

-- Lookups when computing "have I notified this subscriber about this event
-- already, and how many times?" during batch composition.
CREATE INDEX IF NOT EXISTS idx_notification_log_subscriber_event
    ON notification_log (subscriber_id, event_id);

INSERT INTO schema_migrations (version) VALUES ('003') ON CONFLICT DO NOTHING;
