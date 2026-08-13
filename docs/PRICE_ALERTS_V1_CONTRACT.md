# Bullionova Price Alerts V1 Server Contract Notes

This is an internal engineering contract for the server-authoritative Price Alerts implementation. It is not customer-facing documentation. The Flask V1 routes are registered behind the disabled-by-default `BULLIONOVA_PRICE_ALERTS_SERVER_ENABLED` gate.

## Table Responsibilities

- `price_alert_installations`: anonymous installation registration, app version, locale, timezone and deletion state.
- `price_alert_installation_credentials`: server-generated installation secret hashes and optional encrypted secret material.
- `price_alert_fcm_tokens`: future FCM token records, stored as hashes plus ciphertext placeholders.
- `price_alert_entitlements`: Google Play subscription verification state for `stackwatch_pro`.
- `price_alert_notification_preferences`: notification enabled state, quiet hours and the privacy-safe notification-detail preference.
- `price_alert_definitions`: durable alert identity, source identity, condition, target and revision.
- `price_alert_states`: baseline, comparison and triggered state that changes during evaluation.
- `price_alert_quote_observations`: validated authoritative spot or future dealer quote observations.
- `price_alert_fx_observations`: validated authoritative FX snapshots used for non-USD alert evaluation.
- `price_alert_trigger_events`: immutable trigger history and event idempotency.
- `price_alert_notification_deliveries`: delivery deduplication and delivery state.
- `price_alert_deletion_tombstones`: cursor-safe deletion propagation.
- `price_alert_idempotency_records`: client mutation idempotency state.
- `price_alert_provider_usage_records`: durable provider-call budget accounting.
- `price_alert_worker_runs`: worker start, finish and safe status metadata.
- `price_alert_security_events`: rate limit, abuse, auth and verification audit events.

## Sensitive Fields

Installation secrets, FCM tokens and Google Play purchase tokens must never be stored in plaintext. The migration provides hash columns and ciphertext placeholders only. Tokens must not appear in logs, exception messages, notification payloads, portable backups or client debug output.

## Retention Targets

- Installation deletion tombstones: retain long enough for all active clients to sync deletion state, then purge.
- Trigger events: retain the server audit trail needed for sync and support; clients keep at most 100 local trigger-history events.
- Notification deliveries: retain only for deduplication and operational diagnosis.
- Security events: retain according to abuse-monitoring and privacy-policy limits.
- Quote observations: retain enough recent authoritative data for crossing detection, 24-hour change calculation and incident diagnosis.

## Transaction Boundaries

- Installation registration: create installation and credential in one transaction.
- Entitlement verification: verify Play state, update entitlement, and enforce the active-installation cap with row locks.
- Alert upsert/resume/rearm: verify entitlement, enforce the 50 resumable-alert limit and update definition/state in one transaction.
- Alert deletion/delete-all: update/deactivate records and insert tombstones in one transaction.
- Alert crossing: read locked alert state, compare one authoritative observation, insert at most one trigger event, update alert state and enqueue at most one delivery record in one transaction.
- FCM token upsert/delete: update token records idempotently in one transaction.

## Alert Crossing Transaction

The server must evaluate only authoritative, positive, finite, non-cached and fresh observations. It must compare exact decimal strings or NUMERIC values and never floating point values. A first valid observation establishes the baseline. A trigger event is created only on a deterministic below-to-equal/above or above-to-equal/below crossing.

## Event And Delivery Idempotency

Trigger events use a stable unique identity derived from alert and observation identity. Delivery records include a notification payload hash and are unique per installation/event/payload hash. Mutating API requests require client idempotency keys.

## Deletion Flow

Deleting one alert or all alerts must create tombstones so cursor-based sync can delete local records. Installation deletion must revoke FCM tokens and stop evaluation before tombstones expire.

## Why Local JSON Or In-Memory Storage Is Not Production-Safe

The current dealer backend snapshot and in-memory metals cache are suitable for public price serving but not for Price Alerts. Production alerts require durable state across deploys, multi-instance safety, transactional crossing detection, idempotency, deletion sync, abuse controls, entitlement verification and FCM delivery deduplication. Local JSON files and process memory cannot provide those guarantees on Render, Cloud Run or any horizontally scaled host.
