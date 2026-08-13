# PR-PRICE-ALERTS-5C2 Staging Runbook

This runbook is for the first controlled Bullionova server-backed Spot Price Alerts staging test. It must not be used for production activation.

Use placeholders for all secrets and staging URLs. Do not paste credentials into source control, terminal transcripts, screenshots, issue trackers or chat.

## Required Order

1. Owner approves maximum cost.
   - Confirm the Render workspace can use a Free web service and one Free Postgres database.
   - Confirm the minimum Render cron charge is acceptable.
   - Confirm an explicit spending limit or teardown deadline before creating resources.

2. Prepare a deliberate `dealer_backend` commit.
   - Work from `C:\NorthStackDigital\TRAINING_GROUND\00_SANDBOX_APPS\StackWatch_Audit_copy\Dev\stackwatch\dealer_backend`.
   - Review only intended files:
     - `git status --short`
     - `git diff -- render.staging.yaml .env.example price_alerts migrations tests docs`
   - Commit only after owner approval.

3. Push only the intended backend branch.
   - `git branch --show-current`
   - `git push origin <approved-backend-branch>`

4. Create staging Render resources.
   - Use `render.staging.yaml` from the `dealer_backend` repository root.
   - Do not connect the existing production service.
   - Expected resource names:
     - `bullionova-price-alerts-staging-web`
     - `bullionova-price-alerts-staging-worker`
     - `bullionova-price-alerts-staging-db`

5. Add secrets manually in Render.
   - Required secret or sensitive variables:
     - `DATABASE_URL` from `bullionova-price-alerts-staging-db`
     - `PRICE_ALERTS_TOKEN_HASH_KEY`
     - `PRICE_ALERTS_TOKEN_ENCRYPTION_KEYS`
     - `METALS_API_KEY`
     - `PRICE_ALERTS_FIREBASE_CREDENTIALS_FILE`
     - `PRICE_ALERTS_GOOGLE_PLAY_CREDENTIALS_FILE`
   - Leave first-run service gates disabled:
     - `BULLIONOVA_PRICE_ALERTS_SERVER_ENABLED=false`
     - `BULLIONOVA_PRICE_ALERTS_WORKER_ENABLED=false`
     - `BULLIONOVA_PRICE_ALERTS_ALLOW_TEST_ENTITLEMENTS=false`
     - `BULLIONOVA_PRICE_ALERTS_ALLOW_SYNTHETIC_QUOTES=false`
     - `PRICE_ALERTS_FCM_ENABLED=false`
     - `PRICE_ALERTS_PLAY_VERIFICATION_ENABLED=false`

6. Leave backend and worker disabled.
   - Confirm `/v1/health` reports `priceAlertsEnabled=false`.
   - Confirm the worker run logs show `{'status': 'disabled'}`.

7. Apply PostgreSQL migration.
   - From a Render shell or one-off job with the staging database URL only:
     - `python -m price_alerts.migrations --status`
     - `python -m price_alerts.migrations --check`
     - `python -m price_alerts.migrations --apply`
   - Do not run against production.

8. Verify schema.
   - `python -m price_alerts.migrations --check`
   - Confirm all expected `price_alert_*` tables exist in staging.
   - Confirm no production database URL is present in staging service settings.

9. Enable staging web API only.
   - For `bullionova-price-alerts-staging-web` only:
     - `BULLIONOVA_PRICE_ALERTS_SERVER_ENABLED=true`
   - Keep scheduled worker disabled:
     - `BULLIONOVA_PRICE_ALERTS_WORKER_ENABLED=false`

10. Verify `/health` and `/v1/health`.
    - `GET https://<staging-web-host>/health`
    - `GET https://<staging-web-host>/v1/health`
    - Confirm no secrets, keys, tokens or credential material are exposed.

11. Build a staging debug APK with exact dart-defines.
    - From the Flutter project root:
      - `flutter build apk --debug --dart-define=STACKWATCH_DEBUG_PRO=true --dart-define=BULLIONOVA_PRICE_ALERTS_SERVER_SYNC_ENABLED=true --dart-define=BULLIONOVA_PRICE_ALERTS_SERVER_ENVIRONMENT=staging --dart-define=BULLIONOVA_PRICE_ALERTS_SERVER_BASE_URL=https://<staging-web-host>`
    - Do not set `BULLIONOVA_PRICE_ALERTS_PRODUCTION_READY=true`.

12. Install only on tablet `a6d51fc6`.
    - `adb -s a6d51fc6 install -r -t "C:\NorthStackDigital\TRAINING_GROUND\00_SANDBOX_APPS\StackWatch_Audit_copy\Dev\stackwatch\build\app\outputs\flutter-apk\app-debug.apk"`
    - Do not target any phone.
    - Do not clear data.

13. Accept the disclosure and register installation.
    - Open Price Alerts.
    - Create or save a Spot Alert.
    - Choose `Continue` on the server-monitoring disclosure.
    - Confirm registration succeeds.

14. Copy the staging installation ID only.
    - Use `Copy staging installation ID` in the debug staging UI.
    - Do not copy or expose the installation secret.

15. Grant temporary staging debug entitlement through CLI.
    - In the staging web service shell:
      - `BULLIONOVA_ENVIRONMENT=staging BULLIONOVA_PRICE_ALERTS_SERVER_ENABLED=true BULLIONOVA_PRICE_ALERTS_ALLOW_TEST_ENTITLEMENTS=true python -m price_alerts.admin grant-test-entitlement --installation-id <installation_id> --package-id com.northstack.stackwatch.debug --ttl-hours 1`
    - TTL must not exceed 24 hours.

16. Create and sync a Spot Price Alert.
    - Use a USD per-oz Spot Alert for deterministic synthetic testing.
    - Confirm local alert remains saved if synchronization is pending.
    - Confirm the app does not call dealer-alert endpoints.

17. Register its FCM token.
    - Enable notifications on the tablet.
    - Enable `PRICE_ALERTS_FCM_ENABLED=true` only after Firebase Admin credentials are configured for staging.
    - Confirm token registration without logging or copying the token.

18. Run the deterministic synthetic crossing.
    - Enable only for staging:
      - `BULLIONOVA_PRICE_ALERTS_ALLOW_SYNTHETIC_QUOTES=true`
    - In the staging web service shell:
      - `BULLIONOVA_ENVIRONMENT=staging BULLIONOVA_PRICE_ALERTS_SERVER_ENABLED=true BULLIONOVA_PRICE_ALERTS_ALLOW_TEST_ENTITLEMENTS=true BULLIONOVA_PRICE_ALERTS_ALLOW_SYNTHETIC_QUOTES=true python -m price_alerts.admin run-synthetic-spot-crossing --installation-id <installation_id> --metal-id Gold --usd-per-troy-ounce <exact_decimal>`
    - This command must not consume Metals-API quota.

19. Verify generic FCM foreground, background and terminated delivery.
    - Confirm notifications contain routing/private-safe generic content only.
    - Confirm no price, target, holding, portfolio value or note appears.

20. Verify notification routing and event dedupe.
    - Tap the notification.
    - Confirm Bullionova opens the relevant alert/event once.
    - Run the same synthetic crossing again and confirm no duplicate event is created.

21. Perform one separately approved real Metals-API call.
    - Before enabling the worker, owner approves one real provider call.
    - Temporarily set a known small staging cap using `PRICE_ALERTS_METALS_APPLICATION_HARD_LIMIT=<approved-small-cap>`.
    - Run exactly one worker cycle:
      - `BULLIONOVA_PRICE_ALERTS_SERVER_ENABLED=true BULLIONOVA_PRICE_ALERTS_WORKER_ENABLED=true python -m price_alerts.worker --once`
    - Immediately disable worker again.

22. Test pause, resume, re-arm, edit and delete.
    - Confirm each local action queues and synchronizes one idempotent mutation.
    - Confirm delete creates remote deletion/tombstone behavior.

23. Verify restart and reconciliation.
    - Force stop and relaunch the debug app on tablet only.
    - Confirm server status reconciles without duplicate registration or duplicate events.

24. Revoke staging entitlement.
    - `BULLIONOVA_ENVIRONMENT=staging BULLIONOVA_PRICE_ALERTS_SERVER_ENABLED=true BULLIONOVA_PRICE_ALERTS_ALLOW_TEST_ENTITLEMENTS=true python -m price_alerts.admin revoke-test-entitlement --installation-id <installation_id>`

25. Disable worker and backend.
    - Set:
      - `BULLIONOVA_PRICE_ALERTS_WORKER_ENABLED=false`
      - `BULLIONOVA_PRICE_ALERTS_SERVER_ENABLED=false`
      - `BULLIONOVA_PRICE_ALERTS_ALLOW_TEST_ENTITLEMENTS=false`
      - `BULLIONOVA_PRICE_ALERTS_ALLOW_SYNTHETIC_QUOTES=false`

26. Remove staging test data safely.
    - Export any required logs first.
    - Delete only staging rows/resources.
    - Do not touch production data or the existing production Render service.

27. Decide GO/NO-GO for production integration.
    - GO requires:
      - schema verified;
      - no secret exposure;
      - no duplicate events;
      - generic FCM delivery in all app states;
      - local alerts preserved on transient failures;
      - provider budget records correct attempts;
      - worker disabled after test;
      - owner accepts remaining hosting cost.
