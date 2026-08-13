# Bullionova Price Alerts 5B Sync and Staging Blueprint

This document describes the disabled-by-default client/server sync foundation for server-backed Spot Price Alerts. It is not an activation runbook and does not authorize production use.

## Client Registration

The Flutter client may register an anonymous installation only when all of these are true:

- `BULLIONOVA_PRICE_ALERTS_SERVER_SYNC_ENABLED=true`
- `BULLIONOVA_PRICE_ALERTS_SERVER_ENVIRONMENT` is valid for the build type
- `BULLIONOVA_PRICE_ALERTS_SERVER_BASE_URL` is a valid HTTPS URL
- the user has accepted the server-backed alert disclosure
- the user is creating or maintaining a server-backed Spot Alert

The installation ID and secret are stored in encrypted device persistence under installation-bound keys. They are never added to portable `.stackwatchbackup` exports.

## Consent and Disclosure

Disclosure consent is stored per environment and host. A staging consent cannot authorize production, and a production consent cannot authorize staging. A future disclosure wording change must increase `priceAlertServerDisclosureVersion`.

The disclosure must explain that server monitoring sends only the anonymous installation identifier, notification destination, alert metal, target, direction, currency, unit, timezone/locale, notification preferences, and subscription verification evidence. It must also state that holdings, portfolio value, purchase history, notes, analytics, and backup password are not sent.

## Alert Mutation Sequence

The local alert definition remains the user-editable source. Network state is held separately in `priceAlertServerSyncMetadataV1`. Mutations are queued durably in `priceAlertServerMutationQueueV1` with stable idempotency keys, then flushed through the authenticated V1 API:

- `PUT /v1/alerts/{alertId}`
- `POST /v1/alerts/{alertId}:pause`
- `POST /v1/alerts/{alertId}:resume`
- `POST /v1/alerts/{alertId}:rearm`
- `DELETE /v1/alerts/{alertId}`
- `DELETE /v1/price-alerts`

Dealer alerts remain unavailable for server-active monitoring.

## FCM Token Lifecycle

FCM tokens are sent only through the authenticated V1 API after policy, consent, and installation registration allow sync. The local client stores only a digest for repeat-registration detection, plus any pending secure queue item needed for retry. Notification payloads remain generic and routing-only.

## Play Token Lifecycle

The billing layer keeps existing local Pro behavior. Server verification evidence is submitted only through the optional sync callback for `stackwatch_pro` / `monthly`. Tokens are not logged or backed up. If temporary offline persistence is required, the value is scoped to the server environment and host and removed after authoritative verification.

`STACKWATCH_DEBUG_PRO` is never server entitlement evidence.

## Server-Authoritative State

The server is authoritative for background monitoring eligibility, trigger events, delivery status, and server entitlement. The device is authoritative for the editable local draft until a mutation is accepted.

## Offline Queue and Retries

The queue is encrypted, bounded, environment-scoped, and host-scoped. Superseded unsent edits coalesce. A later delete removes earlier unsent upserts for the same alert. Transient errors use bounded exponential backoff. Permanent validation, authentication, entitlement, and dealer-unavailable errors are not retried indefinitely.

## Reconciliation

Trigger events are fetched from `GET /v1/events/sync` and merged once by event ID using the existing local event store. The client does not reconstruct price details from FCM payloads.

## Deletion Flow

Single-alert and delete-all operations create authenticated tombstone/delete mutations. If remote deletion is pending, local alert definitions can be removed according to the user action while minimum encrypted sync state remains until deletion is confirmed. Portable restore never restores another device's installation credential or sync queue.

## Staging Test Entitlements

The debug tablet cannot prove a production Play entitlement. Staging may use a server-side admin CLI only when:

- `BULLIONOVA_ENVIRONMENT=staging`
- `BULLIONOVA_PRICE_ALERTS_ALLOW_TEST_ENTITLEMENTS=true`
- the registered installation package is `com.northstack.stackwatch.debug`
- the requested TTL is explicit and no more than 24 hours

Example, from the repository root:

```powershell
python -m dealer_backend.price_alerts.admin grant-test-entitlement --installation-id installation_xxx --package-id com.northstack.stackwatch.debug --ttl-hours 2
python -m dealer_backend.price_alerts.admin status --installation-id installation_xxx
python -m dealer_backend.price_alerts.admin revoke-test-entitlement --installation-id installation_xxx
```

The CLI prints no secrets or tokens and records an audit event.

## Render Staging Blueprint

`dealer_backend/render.staging.yaml` describes:

- `bullionova-price-alerts-staging-web`
- `bullionova-price-alerts-staging-db`
- `bullionova-price-alerts-staging-worker`

The worker is a 10-minute cron:

```text
*/10 * * * *
```

Because `rootDir` is `dealer_backend`, the worker command is:

```text
python -m price_alerts.worker --once
```

The blueprint leaves Price Alerts disabled by default. Secret values such as database credentials, Metals-API keys, Firebase credentials, Play credentials, token hash keys, and encryption keys must be supplied only through the hosting provider.

## Activation Checklist

1. Provision staging PostgreSQL.
2. Apply `dealer_backend/migrations/0001_price_alerts_v1.sql`.
3. Set token encryption and hash keys in the host.
4. Configure staging Metals and FX provider limits.
5. Configure Firebase Admin credentials.
6. Configure Google Play service-account verification only for production package testing.
7. Enable the staging server gate.
8. Enable the debug client sync define with an HTTPS staging base URL.
9. Register the debug installation through the app.
10. Grant a short staging test entitlement through the CLI.
11. Run one controlled server-backed Spot Alert end-to-end test.

## Kill Switch and Rollback

Set `BULLIONOVA_PRICE_ALERTS_SERVER_ENABLED=false` server-side to stop all Price Alert server operations. Set `BULLIONOVA_PRICE_ALERTS_SERVER_SYNC_ENABLED=false` client-side to prevent the app from registering, sending FCM tokens, submitting entitlement evidence, or flushing alert mutations. Local alert definitions remain on device.
