-- Bullionova Price Alerts V1 durable server schema.
-- Review-only migration. Do not run against production until Firebase,
-- Google Play verification and provider licensing are approved.

CREATE TABLE IF NOT EXISTS price_alert_installations (
    installation_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL CHECK (platform IN ('android', 'ios', 'web', 'desktop')),
    package_id TEXT NOT NULL,
    app_version_name TEXT NOT NULL,
    app_version_code INTEGER NOT NULL CHECK (app_version_code > 0),
    locale TEXT,
    time_zone_id TEXT,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS price_alert_installation_credentials (
    installation_id TEXT PRIMARY KEY REFERENCES price_alert_installations(installation_id) ON DELETE CASCADE,
    secret_hash TEXT NOT NULL,
    secret_ciphertext BYTEA,
    rotated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS price_alert_entitlements (
    entitlement_id TEXT PRIMARY KEY,
    installation_id TEXT NOT NULL REFERENCES price_alert_installations(installation_id) ON DELETE CASCADE,
    package_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    base_plan_id TEXT,
    purchase_token_hash TEXT NOT NULL,
    purchase_token_ciphertext BYTEA,
    purchase_token_key_version TEXT,
    status TEXT NOT NULL CHECK (status IN ('active', 'inactive', 'grace', 'unknown')),
    verified_until TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    last_verified_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (installation_id, product_id, purchase_token_hash)
);

CREATE INDEX IF NOT EXISTS price_alert_entitlements_installation_status_idx
    ON price_alert_entitlements (installation_id, status, expires_at);

CREATE TABLE IF NOT EXISTS price_alert_fcm_tokens (
    token_id TEXT PRIMARY KEY,
    installation_id TEXT NOT NULL REFERENCES price_alert_installations(installation_id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    token_ciphertext BYTEA,
    token_key_version TEXT,
    platform TEXT NOT NULL CHECK (platform IN ('android', 'ios', 'web', 'desktop')),
    token_issued_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS price_alert_fcm_tokens_installation_active_idx
    ON price_alert_fcm_tokens (installation_id, revoked_at);

CREATE TABLE IF NOT EXISTS price_alert_notification_preferences (
    installation_id TEXT PRIMARY KEY REFERENCES price_alert_installations(installation_id) ON DELETE CASCADE,
    notifications_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    show_price_details_in_notifications BOOLEAN NOT NULL DEFAULT FALSE,
    quiet_hours_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    quiet_hours_start_minute INTEGER NOT NULL DEFAULT 0 CHECK (quiet_hours_start_minute BETWEEN 0 AND 1439),
    quiet_hours_end_minute INTEGER NOT NULL DEFAULT 0 CHECK (quiet_hours_end_minute BETWEEN 0 AND 1439),
    quiet_hours_time_zone_id TEXT,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS price_alert_definitions (
    alert_id TEXT PRIMARY KEY,
    installation_id TEXT NOT NULL REFERENCES price_alert_installations(installation_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN (
        'draft',
        'activeWaitingForBaseline',
        'activeArmed',
        'triggeredNeedsRearm',
        'paused',
        'sourceUnavailable',
        'notificationPermissionRequired',
        'proSuspended',
        'restoreReviewRequired'
    )),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('spot', 'dealerRetail', 'dealerBuyback')),
    provider_id TEXT NOT NULL,
    metal_id TEXT NOT NULL,
    dealer_id TEXT,
    dealer_country_code TEXT,
    product_id TEXT,
    quote_id TEXT NOT NULL,
    quote_side TEXT,
    source_currency_code CHAR(3) NOT NULL,
    source_unit_id TEXT,
    price_basis TEXT NOT NULL CHECK (price_basis IN ('perUnit', 'productTotal')),
    source_url TEXT,
    source_verified BOOLEAN NOT NULL DEFAULT FALSE,
    alert_currency_code CHAR(3) NOT NULL,
    condition TEXT NOT NULL CHECK (condition IN ('risesToOrAbove', 'fallsToOrBelow')),
    target_numeric NUMERIC(38, 18) NOT NULL CHECK (target_numeric > 0),
    target_unscaled NUMERIC(78, 0) NOT NULL,
    target_scale INTEGER NOT NULL CHECK (target_scale >= 0),
    unit_id TEXT,
    rearm_required BOOLEAN NOT NULL DEFAULT FALSE,
    restored_review_required BOOLEAN NOT NULL DEFAULT FALSE,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK (
        (price_basis = 'perUnit' AND unit_id IS NOT NULL AND source_unit_id = unit_id)
        OR
        (price_basis = 'productTotal' AND unit_id IS NULL AND source_unit_id IS NULL)
    ),
    CHECK (
        source_kind = 'spot'
        OR
        (dealer_id IS NOT NULL AND source_kind IN ('dealerRetail', 'dealerBuyback'))
    )
);

CREATE INDEX IF NOT EXISTS price_alert_definitions_installation_status_idx
    ON price_alert_definitions (installation_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS price_alert_definitions_source_idx
    ON price_alert_definitions (
        source_kind,
        provider_id,
        metal_id,
        source_currency_code,
        source_unit_id,
        price_basis
    );

CREATE TABLE IF NOT EXISTS price_alert_states (
    alert_id TEXT PRIMARY KEY REFERENCES price_alert_definitions(alert_id) ON DELETE CASCADE,
    baseline_observation_id TEXT,
    last_observation_id TEXT,
    last_comparison_state TEXT CHECK (last_comparison_state IN ('below', 'equal', 'above')),
    triggered_at TIMESTAMPTZ,
    triggered_observation_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS price_alert_quote_observations (
    observation_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('spot', 'dealerRetail', 'dealerBuyback')),
    provider_id TEXT NOT NULL,
    metal_id TEXT NOT NULL,
    quote_id TEXT NOT NULL,
    currency_code CHAR(3) NOT NULL,
    unit_id TEXT,
    price_basis TEXT NOT NULL CHECK (price_basis IN ('perUnit', 'productTotal')),
    price_numeric NUMERIC(38, 18) NOT NULL CHECK (price_numeric > 0),
    price_unscaled NUMERIC(78, 0) NOT NULL,
    price_scale INTEGER NOT NULL CHECK (price_scale >= 0),
    provider_timestamp TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ,
    is_authoritative BOOLEAN NOT NULL,
    is_cached BOOLEAN NOT NULL,
    is_stale BOOLEAN NOT NULL,
    source_available BOOLEAN NOT NULL,
    fx_required BOOLEAN NOT NULL,
    fx_timestamp TIMESTAMPTZ,
    fx_is_stale BOOLEAN NOT NULL,
    product_available BOOLEAN,
    product_in_stock BOOLEAN,
    native_currency_code CHAR(3),
    native_price_numeric NUMERIC(38, 18),
    native_price_unscaled NUMERIC(78, 0),
    native_price_scale INTEGER CHECK (native_price_scale IS NULL OR native_price_scale >= 0),
    source_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (is_authoritative = TRUE AND is_cached = FALSE AND is_stale = FALSE)
);

CREATE INDEX IF NOT EXISTS price_alert_observations_source_time_idx
    ON price_alert_quote_observations (
        source_kind,
        provider_id,
        metal_id,
        currency_code,
        unit_id,
        price_basis,
        provider_timestamp DESC
    );

CREATE TABLE IF NOT EXISTS price_alert_fx_observations (
    observation_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    base_currency_code CHAR(3) NOT NULL,
    quote_currency_code CHAR(3) NOT NULL,
    rate_numeric NUMERIC(38, 18) NOT NULL CHECK (rate_numeric > 0),
    rate_unscaled NUMERIC(78, 0) NOT NULL,
    rate_scale INTEGER NOT NULL CHECK (rate_scale >= 0),
    provider_timestamp TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ,
    is_authoritative BOOLEAN NOT NULL,
    is_stale BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (is_authoritative = TRUE AND is_stale = FALSE)
);

CREATE INDEX IF NOT EXISTS price_alert_fx_observations_pair_time_idx
    ON price_alert_fx_observations (
        base_currency_code,
        quote_currency_code,
        provider_timestamp DESC
    );

CREATE TABLE IF NOT EXISTS price_alert_trigger_events (
    event_id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL REFERENCES price_alert_definitions(alert_id) ON DELETE CASCADE,
    installation_id TEXT NOT NULL REFERENCES price_alert_installations(installation_id) ON DELETE CASCADE,
    observation_id TEXT NOT NULL REFERENCES price_alert_quote_observations(observation_id),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('spot', 'dealerRetail', 'dealerBuyback')),
    metal_id TEXT NOT NULL,
    condition TEXT NOT NULL CHECK (condition IN ('risesToOrAbove', 'fallsToOrBelow')),
    target_numeric NUMERIC(38, 18) NOT NULL CHECK (target_numeric > 0),
    target_unscaled NUMERIC(78, 0) NOT NULL,
    target_scale INTEGER NOT NULL CHECK (target_scale >= 0),
    triggered_price_numeric NUMERIC(38, 18) NOT NULL CHECK (triggered_price_numeric > 0),
    triggered_price_unscaled NUMERIC(78, 0) NOT NULL,
    triggered_price_scale INTEGER NOT NULL CHECK (triggered_price_scale >= 0),
    alert_currency_code CHAR(3) NOT NULL,
    unit_id TEXT,
    price_basis TEXT NOT NULL CHECK (price_basis IN ('perUnit', 'productTotal')),
    provider_timestamp TIMESTAMPTZ NOT NULL,
    triggered_at TIMESTAMPTZ NOT NULL,
    dealer_id TEXT,
    product_id TEXT,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (alert_id, observation_id)
);

CREATE INDEX IF NOT EXISTS price_alert_trigger_events_installation_time_idx
    ON price_alert_trigger_events (installation_id, triggered_at DESC);

CREATE TABLE IF NOT EXISTS price_alert_notification_deliveries (
    delivery_id TEXT PRIMARY KEY,
    installation_id TEXT NOT NULL REFERENCES price_alert_installations(installation_id) ON DELETE CASCADE,
    event_id TEXT NOT NULL REFERENCES price_alert_trigger_events(event_id) ON DELETE CASCADE,
    fcm_token_hash TEXT,
    delivery_state TEXT NOT NULL CHECK (delivery_state IN ('pending', 'delivered', 'suppressedQuietHours', 'failed')),
    notification_payload_hash TEXT NOT NULL,
    provider_message_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS price_alert_notification_deliveries_state_idx
    ON price_alert_notification_deliveries (delivery_state, created_at);

CREATE TABLE IF NOT EXISTS price_alert_deletion_tombstones (
    tombstone_id TEXT PRIMARY KEY,
    installation_id TEXT NOT NULL REFERENCES price_alert_installations(installation_id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('installation', 'alert', 'event', 'fcmToken')),
    entity_id TEXT NOT NULL,
    reason TEXT,
    deleted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    UNIQUE (installation_id, entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS price_alert_tombstones_expiry_idx
    ON price_alert_deletion_tombstones (expires_at);

CREATE TABLE IF NOT EXISTS price_alert_security_events (
    security_event_id TEXT PRIMARY KEY,
    installation_id TEXT REFERENCES price_alert_installations(installation_id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    ip_hash TEXT,
    user_agent_hash TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS price_alert_security_events_installation_time_idx
    ON price_alert_security_events (installation_id, created_at DESC);

CREATE INDEX IF NOT EXISTS price_alert_security_events_type_time_idx
    ON price_alert_security_events (event_type, created_at DESC);

CREATE TABLE IF NOT EXISTS price_alert_idempotency_records (
    idempotency_key TEXT PRIMARY KEY,
    installation_id TEXT REFERENCES price_alert_installations(installation_id) ON DELETE CASCADE,
    request_hash TEXT NOT NULL,
    response_hash TEXT,
    status TEXT NOT NULL CHECK (status IN ('started', 'completed', 'failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS price_alert_idempotency_installation_time_idx
    ON price_alert_idempotency_records (installation_id, created_at DESC);

CREATE TABLE IF NOT EXISTS price_alert_provider_usage_records (
    usage_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('started', 'success', 'failed', 'budget-paused')),
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS price_alert_provider_usage_provider_time_idx
    ON price_alert_provider_usage_records (provider_id, attempted_at DESC);

CREATE TABLE IF NOT EXISTS price_alert_worker_runs (
    worker_run_id TEXT PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    evaluated_count INTEGER NOT NULL DEFAULT 0 CHECK (evaluated_count >= 0),
    triggered_count INTEGER NOT NULL DEFAULT 0 CHECK (triggered_count >= 0),
    delivery_count INTEGER NOT NULL DEFAULT 0 CHECK (delivery_count >= 0),
    safe_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS price_alert_worker_runs_started_idx
    ON price_alert_worker_runs (started_at DESC);

-- Service-layer transactional rules:
-- 1. Enforce no more than three active installations per verified subscription
--    inside the entitlement-verification transaction with row locks.
-- 2. Enforce no more than 50 resumable alerts per installation inside the
--    alert-upsert/resume/rearm transaction with row locks.
-- 3. Prune local-compatible trigger history to 100 events per installation
--    after inserting a new trigger event.
-- 4. Dealer alerts must remain inactive until exact verified dealer feeds are
--    available; the schema stores the future source identity only.
