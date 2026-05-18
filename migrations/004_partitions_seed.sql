-- 004_partitions_seed.sql
-- Bootstrap monthly partitions for notification_log and provide a function
-- to add future partitions. The scheduler can call this function (or an
-- ops cron) to ensure the current and next month always have partitions.

CREATE OR REPLACE FUNCTION ensure_notification_log_partition(p_month DATE)
RETURNS VOID AS $$
DECLARE
    partition_name TEXT;
    start_ts       TIMESTAMPTZ;
    end_ts         TIMESTAMPTZ;
BEGIN
    -- Truncate to month start
    start_ts := DATE_TRUNC('month', p_month);
    end_ts   := start_ts + INTERVAL '1 month';
    partition_name := 'notification_log_' || TO_CHAR(start_ts, 'YYYY_MM');

    EXECUTE FORMAT(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF notification_log
            FOR VALUES FROM (%L) TO (%L)',
        partition_name, start_ts, end_ts
    );
END;
$$ LANGUAGE plpgsql;

-- Seed partitions: previous month, current month, and next two months.
-- Gives a comfortable forward buffer; the app should call
-- ensure_notification_log_partition() periodically to keep extending.
SELECT ensure_notification_log_partition((NOW() - INTERVAL '1 month')::DATE);
SELECT ensure_notification_log_partition(NOW()::DATE);
SELECT ensure_notification_log_partition((NOW() + INTERVAL '1 month')::DATE);
SELECT ensure_notification_log_partition((NOW() + INTERVAL '2 months')::DATE);

INSERT INTO schema_migrations (version) VALUES ('004') ON CONFLICT DO NOTHING;
