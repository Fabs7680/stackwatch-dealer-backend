from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from price_alerts.config import PriceAlertsServerConfig  # noqa: E402
from price_alerts.contracts import ContractError  # noqa: E402
from price_alerts.models import (  # noqa: E402
    EntitlementState,
    Installation,
    NotificationPreferences,
)
from price_alerts.play import StaticPlayVerifier  # noqa: E402
from price_alerts.repository import InMemoryPriceAlertRepository  # noqa: E402
from price_alerts.security import DeterministicTestTokenProtector  # noqa: E402
from price_alerts.service import PriceAlertServerService  # noqa: E402


NOW = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)


class FcmLifecycleTests(unittest.TestCase):
    def test_registration_fails_closed_while_fcm_gate_disabled(self) -> None:
        service, repository = _service(fcm_enabled=False)

        with self.assertRaisesRegex(ContractError, "FCM registration is disabled"):
            service.upsert_fcm_token(_fcm_payload())

        self.assertEqual(repository.active_fcm_tokens_for_installation(installation_id="installation-1"), [])

    def test_registration_requires_current_entitlement_and_enabled_preferences(self) -> None:
        service, repository = _service(fcm_enabled=True)
        repository.entitlements["installation-1"] = EntitlementState(
            status="inactive",
            verified_until_utc=NOW,
            expires_at_utc=NOW,
            last_verified_at_utc=NOW,
        )
        with self.assertRaisesRegex(ContractError, "Pro entitlement required"):
            service.upsert_fcm_token(_fcm_payload())

        repository.entitlements["installation-1"] = _active_entitlement()
        repository.preferences["installation-1"] = NotificationPreferences(
            notifications_enabled=False,
            updated_at_utc=NOW,
        )
        with self.assertRaisesRegex(ContractError, "Notifications are disabled"):
            service.upsert_fcm_token(_fcm_payload())

    def test_registration_and_revoke_are_idempotent_and_redacted(self) -> None:
        service, repository = _service(fcm_enabled=True)
        repository.entitlements["installation-1"] = _active_entitlement()

        response = service.upsert_fcm_token(_fcm_payload())
        self.assertTrue(response["ok"])
        self.assertEqual(
            len(repository.active_fcm_tokens_for_installation(installation_id="installation-1")),
            1,
        )

        delete = service.delete_fcm_token(_delete_payload())
        self.assertTrue(delete["ok"])
        self.assertEqual(repository.active_fcm_tokens_for_installation(installation_id="installation-1"), [])

        second_delete = service.delete_fcm_token(_delete_payload())
        self.assertTrue(second_delete["ok"])
        self.assertEqual(repository.active_fcm_tokens_for_installation(installation_id="installation-1"), [])


def _service(*, fcm_enabled: bool):
    repository = InMemoryPriceAlertRepository()
    repository.installations["installation-1"] = Installation(
        installation_id="installation-1",
        platform="android",
        package_id="com.northstack.stackwatch",
        app_version_name="1.0.22",
        app_version_code=23,
        locale="en_AU",
        time_zone_id="UTC",
        created_at_utc=NOW,
        updated_at_utc=NOW,
    )
    repository.preferences["installation-1"] = NotificationPreferences(
        notifications_enabled=True,
        updated_at_utc=NOW,
    )
    return (
        PriceAlertServerService(
            config=PriceAlertsServerConfig(enabled=True, fcm_enabled=fcm_enabled),
            repository=repository,
            play_verifier=StaticPlayVerifier(),
            token_protector=DeterministicTestTokenProtector(),
        ),
        repository,
    )


def _active_entitlement() -> EntitlementState:
    return EntitlementState(
        status="active",
        verified_until_utc=NOW + timedelta(days=3650),
        expires_at_utc=NOW + timedelta(days=3680),
        last_verified_at_utc=NOW,
    )


def _fcm_payload() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "installationId": "installation-1",
        "fcmToken": "fake-fcm-token-not-logged",
        "platform": "android",
        "tokenIssuedAtUtc": "2026-08-14T12:00:00Z",
        "idempotency": {
            "key": "fcm-token-upsert-0001",
            "createdAtUtc": "2026-08-14T12:00:00Z",
        },
    }


def _delete_payload() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "installationId": "installation-1",
        "idempotency": {
            "key": "fcm-token-delete-0001",
            "createdAtUtc": "2026-08-14T12:00:00Z",
        },
    }


if __name__ == "__main__":
    unittest.main()
