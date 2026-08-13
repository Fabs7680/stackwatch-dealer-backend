# Bullionova Price Alerts Server Deployment Readiness

This document covers the local backend foundation for server-authoritative Spot Price Alerts. It is technical deployment guidance only. Price Alerts production readiness remains disabled until owner setup, staging verification and end-to-end notification testing are complete.

## Components

- Flask V1 REST routes under `/v1/*`, registered by `dealer_backend/api.py`.
- PostgreSQL schema in `dealer_backend/migrations/0001_price_alerts_v1.sql`.
- Explicit migration runner: `python -m dealer_backend.price_alerts.migrations --check` and `--apply`.
- One-run worker: `python -m dealer_backend.price_alerts.worker --once`.
- Metals-API adapter using one central latest-rates request for `XAU,XAG,XPT,XPD`.
- FX snapshot adapter boundary using a central USD-base snapshot.
- Google Play subscription verification adapter.
- Firebase Admin delivery adapter.
- Decimal server evaluator matching the Dart V1 fixture.

## Required Environment

Set `BULLIONOVA_PRICE_ALERTS_SERVER_ENABLED=true` only in a controlled staging or production host after all required secrets and services are configured.

Required before enabling:

- `DATABASE_URL`
- `PRICE_ALERTS_TOKEN_HASH_KEY`
- `PRICE_ALERTS_TOKEN_ENCRYPTION_KEYS`
- `METALS_API_KEY`
- `PRICE_ALERTS_GOOGLE_PLAY_CREDENTIALS_FILE` or equivalent host-managed Google credentials
- `PRICE_ALERTS_FIREBASE_CREDENTIALS_FILE` or equivalent host-managed Firebase credentials

Optional controls include `PRICE_ALERTS_METALS_POLL_INTERVAL_SECONDS`, `PRICE_ALERTS_METALS_APPLICATION_HARD_LIMIT`, `PRICE_ALERTS_METALS_BILLING_CYCLE_ANCHOR_DAY`, freshness windows, batch sizes and CORS allowlist.

## Migration Procedure

1. Provision PostgreSQL on the host.
2. Set `DATABASE_URL` in the host secret manager.
3. Run `python -m dealer_backend.price_alerts.migrations --check`.
4. Run `python -m dealer_backend.price_alerts.migrations --apply`.
5. Run `python -m dealer_backend.price_alerts.migrations --check` again.

The web process must not run destructive migrations on import or startup.

## Worker Procedure

Schedule `python -m dealer_backend.price_alerts.worker --once` every 10 minutes. Do not schedule more frequently than 600 seconds. The worker uses a durable singleton lock and provider-call budget checks before fetching Metals-API. When FCM is explicitly enabled and token protection is configured, eligible non-quiet-hours events send generic routing-only Firebase payloads and record the delivery result.

## Health Checks

Use `/v1/health` for Price Alerts readiness and `/health` for the existing dealer API. Health output is intentionally secret-free and may report disabled, configuring, ready, degraded or budget-paused states.

## 5,000-Call Budget Protection

The Metals-API Bronze plan allows 5,000 calls per billing period. The default application hard stop is 4,800 calls per configured billing cycle, with a warning threshold at 4,500. Every attempted provider call is recorded in PostgreSQL. At the hard stop the worker stops before making another Metals-API request and will not evaluate alerts from stale data.

Expected 10-minute scheduled usage:

- 144 calls/day
- 4,320 calls in 30 days
- 4,464 calls in 31 days

## Disable And Rollback

Set `BULLIONOVA_PRICE_ALERTS_SERVER_ENABLED=false` to disable mutation, evaluation and delivery operations. Existing `/prices`, `/refresh` and dealer health behavior remain available for current clients. Do not drop tables during rollback; retain state for safe future recovery or deletion workflows.

## Owner Setup Still Required

- Firebase project and server credentials.
- Google Play Android Developer API access for `com.northstack.stackwatch`.
- Production PostgreSQL service and backups.
- Render cron or equivalent scheduler.
- FX provider legal approval for server-backed public alerts.
- Staging end-to-end tests with real Play tokens, FCM tokens and hosted database.
- Privacy Policy and Google Play Data Safety updates before production activation.
