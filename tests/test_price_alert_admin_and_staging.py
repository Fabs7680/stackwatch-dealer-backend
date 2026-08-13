from __future__ import annotations

import sys
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from price_alerts.admin import (  # noqa: E402
    DEBUG_PACKAGE_ID,
    TEST_PRODUCT_ID,
    grant_test_entitlement,
    main as admin_main,
    revoke_test_entitlement,
    status_for_installation,
)
from price_alerts.config import PriceAlertsServerConfig  # noqa: E402
from price_alerts.contracts import ContractError  # noqa: E402
from price_alerts.models import (  # noqa: E402
    BASIS_PER_UNIT,
    COMPARISON_BELOW,
    CONDITION_RISES,
    EntitlementState,
    Installation,
    PriceAlertDefinition,
    PriceAlertSource,
    STATUS_ARMED,
)
from price_alerts.repository import InMemoryPriceAlertRepository  # noqa: E402
from price_alerts.synthetic import run_synthetic_spot_crossing  # noqa: E402


class PriceAlertAdminTest(unittest.TestCase):
    def test_test_entitlements_are_default_disabled(self) -> None:
        repo = _FakeAdminRepository()
        repo.packages["installation_1"] = DEBUG_PACKAGE_ID

        with self.assertRaises(ContractError):
            grant_test_entitlement(
                config=PriceAlertsServerConfig(environment="staging"),
                repository=repo,
                installation_id="installation_1",
                package_id=DEBUG_PACKAGE_ID,
                ttl_hours=1,
                now_utc=_now(),
            )

    def test_production_rejects_test_entitlements_even_when_flag_set(self) -> None:
        repo = _FakeAdminRepository()
        repo.packages["installation_1"] = DEBUG_PACKAGE_ID

        with self.assertRaises(ContractError):
            grant_test_entitlement(
                config=PriceAlertsServerConfig(
                    environment="production",
                    allow_test_entitlements=True,
                ),
                repository=repo,
                installation_id="installation_1",
                package_id=DEBUG_PACKAGE_ID,
                ttl_hours=1,
                now_utc=_now(),
            )

    def test_wrong_package_is_rejected(self) -> None:
        repo = _FakeAdminRepository()
        repo.packages["installation_1"] = "com.northstack.stackwatch"

        with self.assertRaises(ContractError):
            grant_test_entitlement(
                config=_staging_config(),
                repository=repo,
                installation_id="installation_1",
                package_id="com.northstack.stackwatch",
                ttl_hours=1,
                now_utc=_now(),
            )

    def test_grant_revoke_status_and_audit_event(self) -> None:
        repo = _FakeAdminRepository()
        repo.packages["installation_1"] = DEBUG_PACKAGE_ID

        result = grant_test_entitlement(
            config=_staging_config(),
            repository=repo,
            installation_id="installation_1",
            package_id=DEBUG_PACKAGE_ID,
            ttl_hours=2,
            now_utc=_now(),
        )

        self.assertEqual(result.package_id, DEBUG_PACKAGE_ID)
        self.assertEqual(result.expires_at_utc, datetime(2026, 8, 13, 2, tzinfo=timezone.utc))
        self.assertEqual(repo.entitlements["installation_1"].status, "active")
        self.assertEqual(repo.audit_events[-1]["event_type"], "staging_test_entitlement_granted")
        self.assertEqual(
            status_for_installation(
                config=_staging_config(),
                repository=repo,
                installation_id="installation_1",
            )["status"],
            "active",
        )

        revoke_test_entitlement(
            config=_staging_config(),
            repository=repo,
            installation_id="installation_1",
        )

        self.assertEqual(repo.entitlements["installation_1"].status, "inactive")
        self.assertEqual(repo.audit_events[-1]["event_type"], "staging_test_entitlement_revoked")

    def test_ttl_is_capped_to_24_hours(self) -> None:
        repo = _FakeAdminRepository()
        repo.packages["installation_1"] = DEBUG_PACKAGE_ID

        with self.assertRaises(ContractError):
            grant_test_entitlement(
                config=_staging_config(),
                repository=repo,
                installation_id="installation_1",
                package_id=DEBUG_PACKAGE_ID,
                ttl_hours=25,
                now_utc=_now(),
            )

    def test_admin_cli_rejects_before_database_construction_when_disabled(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "BULLIONOVA_ENVIRONMENT": "staging",
                "BULLIONOVA_PRICE_ALERTS_SERVER_ENABLED": "false",
                "BULLIONOVA_PRICE_ALERTS_ALLOW_TEST_ENTITLEMENTS": "true",
            },
            clear=True,
        ):
            self.assertEqual(
                admin_main(
                    [
                        "grant-test-entitlement",
                        "--installation-id",
                        "installation_1",
                        "--package-id",
                        DEBUG_PACKAGE_ID,
                        "--ttl-hours",
                        "1",
                    ]
                ),
                2,
            )

    def test_synthetic_quotes_require_staging_and_explicit_enablement(self) -> None:
        repo = _synthetic_repository()
        audit = _FakeAdminRepository()

        with self.assertRaises(ContractError):
            run_synthetic_spot_crossing(
                config=PriceAlertsServerConfig(
                    environment="production",
                    enabled=True,
                    allow_synthetic_quotes=True,
                ),
                repository=repo,
                audit_repository=audit,
                installation_id="installation_1",
                metal_id="Gold",
                usd_per_troy_ounce="3600.00",
                now_utc=_now(),
            )
        with self.assertRaises(ContractError):
            run_synthetic_spot_crossing(
                config=PriceAlertsServerConfig(
                    environment="staging",
                    enabled=True,
                ),
                repository=repo,
                audit_repository=audit,
                installation_id="installation_1",
                metal_id="Gold",
                usd_per_troy_ounce="3600.00",
                now_utc=_now(),
            )

    def test_synthetic_quote_uses_real_worker_and_triggers_once(self) -> None:
        repo = _synthetic_repository()
        audit = _FakeAdminRepository()
        config = PriceAlertsServerConfig(
            environment="staging",
            enabled=True,
            allow_synthetic_quotes=True,
        )

        first = run_synthetic_spot_crossing(
            config=config,
            repository=repo,
            audit_repository=audit,
            installation_id="installation_1",
            metal_id="Gold",
            usd_per_troy_ounce="3600.00",
            now_utc=_now(),
        )
        second = run_synthetic_spot_crossing(
            config=config,
            repository=repo,
            audit_repository=audit,
            installation_id="installation_1",
            metal_id="Gold",
            usd_per_troy_ounce="3600.00",
            now_utc=_now(),
        )

        self.assertEqual(first.worker_result.status, "ok")
        self.assertEqual(first.worker_result.triggered, 1)
        self.assertEqual(first.worker_result.deliveries_created, 1)
        self.assertEqual(second.worker_result.triggered, 0)
        self.assertEqual(len(repo.trigger_events), 1)
        self.assertEqual(len(repo.deliveries), 1)
        self.assertEqual(
            audit.audit_events[0]["event_type"],
            "staging_synthetic_spot_observation_inserted",
        )
        self.assertEqual(
            audit.audit_events[1]["event_type"],
            "staging_synthetic_spot_crossing_evaluated",
        )
        self.assertFalse(
            any(item["provider_id"] == "metals-api" for item in repo.provider_attempts)
        )

    def test_postgres_admin_event_uses_migration_columns(self) -> None:
        source = (BACKEND_DIR / "price_alerts" / "admin.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("security_event_id", source)
        self.assertIn("severity", source)
        self.assertIn("metadata", source)
        self.assertNotIn("details_json", source)


class RenderStagingBlueprintTest(unittest.TestCase):
    def test_blueprint_keeps_server_disabled_and_uses_ten_minute_cron(self) -> None:
        text = (BACKEND_DIR / "render.staging.yaml").read_text(encoding="utf-8")

        self.assertIn("bullionova-price-alerts-staging-web", text)
        self.assertIn("bullionova-price-alerts-staging-db", text)
        self.assertNotIn("rootDir: dealer_backend", text)
        self.assertIn("runtime: python", text)
        self.assertIn("PYTHON_VERSION", text)
        self.assertIn('schedule: "*/10 * * * *"', text)
        self.assertIn("startCommand: python -m price_alerts.worker --once", text)
        self.assertIn("BULLIONOVA_PRICE_ALERTS_SERVER_ENABLED", text)
        self.assertIn("BULLIONOVA_PRICE_ALERTS_WORKER_ENABLED", text)
        self.assertIn("BULLIONOVA_PRICE_ALERTS_ALLOW_SYNTHETIC_QUOTES", text)
        self.assertIn("DEALER_API_METALS_CACHE_ENABLED", text)
        self.assertIn("sync: false", text)
        self.assertIn('value: "false"', text)
        self.assertNotIn("METALS_API_KEY=", text)
        self.assertNotIn("PRICE_ALERTS_TOKEN_HASH_KEY=", text)


class _FakeAdminRepository:
    def __init__(self) -> None:
        self.packages: dict[str, str] = {}
        self.entitlements: dict[str, EntitlementState] = {}
        self.audit_events: list[dict[str, object]] = []

    def installation_package_id(self, installation_id: str) -> str | None:
        return self.packages.get(installation_id)

    def upsert_test_entitlement(
        self,
        *,
        installation_id: str,
        package_id: str,
        entitlement: EntitlementState,
    ) -> None:
        self.entitlements[installation_id] = entitlement

    def revoke_test_entitlement(self, *, installation_id: str) -> None:
        current = self.entitlements[installation_id]
        self.entitlements[installation_id] = EntitlementState(
            status="inactive",
            verified_until_utc=current.verified_until_utc,
            expires_at_utc=current.expires_at_utc,
            last_verified_at_utc=current.last_verified_at_utc,
        )

    def entitlement_for_installation(self, installation_id: str) -> EntitlementState | None:
        return self.entitlements.get(installation_id)

    def record_admin_event(
        self,
        *,
        installation_id: str,
        event_type: str,
        details: dict[str, object],
    ) -> None:
        self.audit_events.append(
            {
                "installation_id": installation_id,
                "event_type": event_type,
                "details": details,
            }
        )


def _staging_config() -> PriceAlertsServerConfig:
    return PriceAlertsServerConfig(
        environment="staging",
        enabled=True,
        allow_test_entitlements=True,
        max_test_entitlement_ttl_hours=24,
    )


def _now() -> datetime:
    return datetime(2026, 8, 13, tzinfo=timezone.utc)


def _synthetic_repository() -> InMemoryPriceAlertRepository:
    repo = InMemoryPriceAlertRepository()
    installation_id = "installation_1"
    repo.installations[installation_id] = Installation(
        installation_id=installation_id,
        platform="android",
        package_id=DEBUG_PACKAGE_ID,
        app_version_name="1.0.22-debug",
        app_version_code=23,
        locale="en_AU",
        time_zone_id="UTC",
        created_at_utc=_now(),
        updated_at_utc=_now(),
    )
    repo.entitlements[installation_id] = EntitlementState(
        status="active",
        verified_until_utc=_now() + timedelta(hours=1),
        expires_at_utc=_now() + timedelta(hours=1),
        last_verified_at_utc=_now(),
    )
    source = PriceAlertSource(
        source_kind="spot",
        provider_id="bullionova-spot",
        metal_id="Gold",
        quote_id="bullionova-spot:Gold:USD:oz",
        source_currency_code="USD",
        source_unit_id="oz",
        price_basis=BASIS_PER_UNIT,
        verified=True,
    )
    repo.alerts["alert_1"] = PriceAlertDefinition(
        alert_id="alert_1",
        installation_id=installation_id,
        created_at_utc=_now() - timedelta(minutes=5),
        updated_at_utc=_now() - timedelta(minutes=5),
        status=STATUS_ARMED,
        source=source,
        metal_id="Gold",
        alert_currency_code="USD",
        condition=CONDITION_RISES,
        target=Decimal("3500"),
        price_basis=BASIS_PER_UNIT,
        rearm_required=False,
        restored_review_required=False,
        unit_id="oz",
        baseline_observation_id="baseline_below",
        last_observation_id="baseline_below",
        last_comparison_state=COMPARISON_BELOW,
    )
    return repo


if __name__ == "__main__":
    unittest.main()
