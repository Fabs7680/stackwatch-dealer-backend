from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from price_alerts import (  # noqa: E402
    API_PATHS,
    ERROR_CODES,
    ApiErrorResponse,
    ContractError,
    EntitlementVerificationRequest,
    FcmTokenRequest,
    Idempotency,
    InstallationRegistrationRequest,
    InstallationRegistrationResponse,
    NotificationPayload,
    PriceAlertContractRecord,
    SyncCursor,
    canonical_decimal,
    parse_stable_enum,
    parse_utc_timestamp,
)


class PriceAlertsContractTests(unittest.TestCase):
    def test_api_paths_and_error_codes_are_stable(self) -> None:
        self.assertEqual(
            API_PATHS["register_installation"],
            "POST /v1/installations/register",
        )
        self.assertEqual(API_PATHS["health"], "GET /v1/health")
        self.assertIn("invalid_idempotency_key", ERROR_CODES)
        self.assertIn("service_unavailable", ERROR_CODES)

    def test_registration_and_error_parsing(self) -> None:
        request = InstallationRegistrationRequest.from_json(
            {
                "schemaVersion": 1,
                "platform": "android",
                "packageId": "com.northstack.stackwatch",
                "appVersionName": "1.0.22",
                "appVersionCode": 23,
            }
        )
        self.assertEqual(request.package_id, "com.northstack.stackwatch")

        error = ApiErrorResponse.from_json(
            {
                "schemaVersion": 1,
                "code": "service_unavailable",
                "message": "Temporary outage",
            }
        )
        self.assertEqual(error.to_json()["code"], "service_unavailable")

    def test_timestamp_decimal_cursor_and_idempotency_validation(self) -> None:
        self.assertEqual(
            parse_utc_timestamp("2026-08-12T00:00:00Z"),
            datetime(2026, 8, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(canonical_decimal("3500.0000"), "3500")
        self.assertEqual(SyncCursor.from_json("cursor_001.~ok").value, "cursor_001.~ok")
        idem = Idempotency.from_json(
            {
                "key": "alert-save-20260812-0001",
                "createdAtUtc": "2026-08-12T00:00:00Z",
            }
        )
        self.assertEqual(idem.key, "alert-save-20260812-0001")

        with self.assertRaisesRegex(ContractError, "invalid_timestamp"):
            parse_utc_timestamp("2026-08-12T00:00:00")
        with self.assertRaisesRegex(ContractError, "invalid_decimal"):
            canonical_decimal(3500.25)
        with self.assertRaisesRegex(ContractError, "invalid_idempotency_key"):
            Idempotency.from_json(
                {
                    "key": "short",
                    "createdAtUtc": "2026-08-12T00:00:00Z",
                }
            )

    def test_unknown_enums_fail_closed(self) -> None:
        with self.assertRaisesRegex(ContractError, "invalid_enum"):
            parse_stable_enum("futureSource", {"spot"})

        with self.assertRaisesRegex(ContractError, "invalid_enum"):
            PriceAlertContractRecord.from_json(
                {
                    "schemaVersion": 1,
                    "alertId": "alert-1",
                    "source": {"sourceKind": "futureSource"},
                    "priceBasis": "perUnit",
                    "condition": "risesToOrAbove",
                    "status": "activeWaitingForBaseline",
                    "target": "3500",
                    "alertCurrencyCode": "AUD",
                    "revision": 1,
                }
            )

    def test_sensitive_representations_are_redacted(self) -> None:
        idempotency = Idempotency.from_json(
            {
                "key": "token-sync-20260812-0001",
                "createdAtUtc": "2026-08-12T00:00:00Z",
            }
        )
        fcm = FcmTokenRequest.from_json(
            {
                "schemaVersion": 1,
                "installationId": "install-1",
                "fcmToken": "fake_fcm_token_for_tests_only_123456",
                "platform": "android",
                "tokenIssuedAtUtc": "2026-08-12T00:00:00Z",
                "idempotency": idempotency.to_json(),
            }
        )
        entitlement = EntitlementVerificationRequest.from_json(
            {
                "schemaVersion": 1,
                "installationId": "install-1",
                "packageId": "com.northstack.stackwatch",
                "productId": "stackwatch_pro",
                "basePlanId": "monthly",
                "playPurchaseToken": "fake_play_purchase_token_for_tests_only_123456",
                "idempotency": idempotency.to_json(),
            }
        )
        registration = InstallationRegistrationResponse.from_json(
            {
                "schemaVersion": 1,
                "installationId": "install-1",
                "installationSecret": "fake_installation_secret_for_tests_only",
                "createdAtUtc": "2026-08-12T00:00:00Z",
                "serverTimeUtc": "2026-08-12T00:00:00Z",
            }
        )

        self.assertIn("[redacted]", repr(fcm))
        self.assertNotIn("fake_fcm_token_for_tests", repr(fcm))
        self.assertIn("[redacted]", repr(entitlement))
        self.assertNotIn("fake_play_purchase_token", repr(entitlement))
        self.assertIn("[redacted]", repr(registration))
        self.assertNotIn("fake_installation_secret", repr(registration))

    def test_notification_payload_contains_routing_only(self) -> None:
        payload = NotificationPayload.from_json(
            {
                "schemaVersion": 1,
                "alertId": "alert-1",
                "eventId": "event-1",
                "sourceKind": "spot",
                "createdAtUtc": "2026-08-12T00:00:00Z",
            }
        )
        self.assertEqual(payload.alert_id, "alert-1")

        with self.assertRaisesRegex(ContractError, "routing identifiers only"):
            NotificationPayload.from_json(
                {
                    "schemaVersion": 1,
                    "alertId": "alert-1",
                    "eventId": "event-1",
                    "sourceKind": "spot",
                    "target": "3500",
                    "createdAtUtc": "2026-08-12T00:00:00Z",
                }
            )

    def test_shared_fixture_schema_is_contract_safe(self) -> None:
        fixture_path = PROJECT_DIR / "test" / "fixtures" / "price_alert_evaluator_v1.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertEqual(fixture["schemaVersion"], 1)
        names = {case["name"] for case in fixture["cases"]}
        self.assertIn("rising crossing", names)
        self.assertIn("dealer source unverified", names)
        for case in fixture["cases"]:
            self.assertIsInstance(case["alert"]["target"], str)
            price = case["observation"].get("price")
            if price is not None:
                self.assertIsInstance(price, str)

    def test_postgresql_migration_structural_invariants(self) -> None:
        sql = (BACKEND_DIR / "migrations" / "0001_price_alerts_v1.sql").read_text(
            encoding="utf-8"
        )
        for table in [
            "price_alert_installations",
            "price_alert_installation_credentials",
            "price_alert_fcm_tokens",
            "price_alert_entitlements",
            "price_alert_notification_preferences",
            "price_alert_definitions",
            "price_alert_states",
            "price_alert_quote_observations",
            "price_alert_fx_observations",
            "price_alert_trigger_events",
            "price_alert_notification_deliveries",
            "price_alert_deletion_tombstones",
            "price_alert_idempotency_records",
            "price_alert_provider_usage_records",
            "price_alert_worker_runs",
            "price_alert_security_events",
        ]:
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", sql)
        self.assertNotIn("DOUBLE PRECISION", sql)
        self.assertNotIn(" FLOAT", sql)
        self.assertNotIn(" REAL", sql)
        self.assertNotIn("holding", sql.lower())
        self.assertIn("purchase_token_hash", sql)
        self.assertIn("token_hash", sql)
        self.assertIn("50 resumable alerts", sql)
        self.assertIn("three active installations", sql)

    def test_v1_routes_are_registered_behind_disabled_default_gate(self) -> None:
        api_source = (BACKEND_DIR / "api.py").read_text(encoding="utf-8")
        self.assertIn("create_price_alerts_blueprint", api_source)
        route_source = (BACKEND_DIR / "price_alerts" / "api_routes.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("/v1/alerts", route_source)
        self.assertIn("/v1/fcm-token", route_source)
        self.assertIn("/v1/installations/register", route_source)
        self.assertIn("Price Alerts server is disabled", route_source)


if __name__ == "__main__":
    unittest.main()
