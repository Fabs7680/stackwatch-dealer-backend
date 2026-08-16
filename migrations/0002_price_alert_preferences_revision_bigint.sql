-- Keep preference revision compatible with client timestamp-based revisions.

ALTER TABLE price_alert_notification_preferences
    ALTER COLUMN revision TYPE BIGINT;
