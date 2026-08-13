from __future__ import annotations

import json
import os
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from price_alerts.budget import ProviderCallBudget, billing_cycle  # noqa: E402
from price_alerts.config import PriceAlertsServerConfig, env_bool  # noqa: E402
from price_alerts.contracts import SCHEMA_VERSION  # noqa: E402
from price_alerts.decimal_utils import canonical_decimal_string  # noqa: E402
from price_alerts.evaluator import PriceAlertServerEvaluator  # noqa: E402
from price_alerts.fcm import FakeFcmSender, price_alert_fcm_payload  # noqa: E402
from price_alerts.migrations import main as migrations_main  # noqa: E402
from price_alerts.models import (  # noqa: E402
    BASIS_PER_UNIT,
    COMPARISON_BELOW,
    CONDITION_RISES,
    EntitlementState,
    Installation,
    PriceAlertDefinition,
    PriceAlertObservation,
    PriceAlertSource,
    QuietHoursPolicy,
    STATUS_ARMED,
    STATUS_WAITING,
)
from price_alerts.play import StaticPlayVerifier  # noqa: E402
from price_alerts.providers import (  # noqa: E402
    FxSnapshot,
    FxSnapshotProvider,
    MetalsApiProvider,
    ProviderObservation,
    SpotProviderSnapshot,
)
from price_alerts.repository import InMemoryPriceAlertRepository  # noqa: E402
from price_alerts.security import DeterministicTestTokenProtector  # noqa: E402
from price_alerts.service import PriceAlertServerService  # noqa: E402
from price_alerts.units import SUPPORTED_UNITS  # noqa: E402
from price_alerts.worker import PriceAlertsWorker, main as worker_main  # noqa: E402


NOW = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)


class FakeHttpClient:
    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    def get_json(self, url: str, *, headers: dict[str, str], timeout_seconds: int):
        self.urls.append(url)
        return self.payload


@contextmanager
def patched_env(**values: str):
    old = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            os.environ[key] = value
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class PriceAlertServerFoundationTests(unittest.TestCase):
    def test_feature_gate_fails_closed(self) -> None:
        with patched_env(BULLIONOVA_PRICE_ALERTS_SERVER_ENABLED="maybe"):
            self.assertFalse(PriceAlertsServerConfig.from_env().enabled)
        with patched_env(BULLIONOVA_PRICE_ALERTS_SERVER_ENABLED="true"):
            self.assertTrue(PriceAlertsServerConfig.from_env().enabled)
        self.assertFalse(env_bool("THIS_VARIABLE_IS_NOT_SET", False))

    def test_health_is_disabled_without_constructing_integrations(self) -> None:
        from flask import Flask
        from price_alerts.api_routes import create_price_alerts_blueprint

        app = Flask(__name__)
        app.register_blueprint(
            create_price_alerts_blueprint(
                config=PriceAlertsServerConfig(enabled=False),
                server_factory=lambda: self.fail("server must not be constructed"),
            )
        )
        response = app.test_client().get("/v1/health")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertFalse(body["priceAlertsEnabled"])
        self.assertEqual(body["priceAlerts"]["state"], "disabled")
        self.assertNotIn("DATABASE_URL", json.dumps(body))

    def test_disabled_mutation_returns_stable_error(self) -> None:
        from flask import Flask
        from price_alerts.api_routes import create_price_alerts_blueprint

        app = Flask(__name__)
        app.register_blueprint(
            create_price_alerts_blueprint(config=PriceAlertsServerConfig(enabled=False))
        )
        response = app.test_client().post("/v1/installations/register", json={})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["code"], "service_unavailable")

    def test_registration_auth_fcm_and_alert_sync_with_fakes(self) -> None:
        from flask import Flask
        from price_alerts.api_routes import create_price_alerts_blueprint

        repository = InMemoryPriceAlertRepository()
        service = PriceAlertServerService(
            config=PriceAlertsServerConfig(enabled=True),
            repository=repository,
            play_verifier=StaticPlayVerifier(),
            token_protector=DeterministicTestTokenProtector(),
        )
        app = Flask(__name__)
        app.register_blueprint(
            create_price_alerts_blueprint(
                config=PriceAlertsServerConfig(enabled=True),
                server_factory=lambda: service,
            )
        )
        client = app.test_client()
        registration = client.post(
            "/v1/installations/register",
            json={
                "schemaVersion": 1,
                "platform": "android",
                "packageId": "com.northstack.stackwatch",
                "appVersionName": "1.0.22",
                "appVersionCode": 23,
            },
        )
        self.assertEqual(registration.status_code, 200)
        credentials = registration.get_json()
        headers = {
            "Authorization": (
                f"BullionovaInstallation {credentials['installationId']}:"
                f"{credentials['installationSecret']}"
            )
        }
        entitlement = client.post(
            "/v1/entitlements/verify",
            headers=headers,
            json={
                "schemaVersion": 1,
                "installationId": credentials["installationId"],
                "packageId": "com.northstack.stackwatch",
                "productId": "stackwatch_pro",
                "basePlanId": "monthly",
                "playPurchaseToken": "fake-play-token-not-logged",
                "idempotency": {
                    "key": "verify-entitlement-0001",
                    "createdAtUtc": "2026-08-12T00:00:00Z",
                },
            },
        )
        self.assertEqual(entitlement.status_code, 200)
        self.assertEqual(entitlement.get_json()["status"], "active")
        fcm = client.post(
            "/v1/fcm-token",
            headers=headers,
            json={
                "schemaVersion": 1,
                "installationId": credentials["installationId"],
                "fcmToken": "fake-fcm-token-not-logged",
                "platform": "android",
                "tokenIssuedAtUtc": "2026-08-12T00:00:00Z",
                "idempotency": {
                    "key": "fcm-token-upsert-0001",
                    "createdAtUtc": "2026-08-12T00:00:00Z",
                },
            },
        )
        self.assertEqual(fcm.status_code, 200)
        alert_response = client.put(
            "/v1/alerts/alert-1",
            headers=headers,
            json={
                "schemaVersion": 1,
                "installationId": credentials["installationId"],
                "idempotency": {
                    "key": "alert-upsert-0000001",
                    "createdAtUtc": "2026-08-12T00:00:00Z",
                },
                "alert": _alert_json("alert-1"),
            },
        )
        self.assertEqual(alert_response.status_code, 200)
        sync = client.get("/v1/alerts/sync", headers=headers)
        self.assertEqual(sync.status_code, 200)
        self.assertEqual(len(sync.get_json()["records"]), 1)

    def test_sensitive_portfolio_fields_fail_closed(self) -> None:
        from flask import Flask
        from price_alerts.api_routes import create_price_alerts_blueprint

        repository = InMemoryPriceAlertRepository()
        service = PriceAlertServerService(
            config=PriceAlertsServerConfig(enabled=True),
            repository=repository,
            play_verifier=StaticPlayVerifier(),
            token_protector=DeterministicTestTokenProtector(),
        )
        app = Flask(__name__)
        app.register_blueprint(
            create_price_alerts_blueprint(
                config=PriceAlertsServerConfig(enabled=True),
                server_factory=lambda: service,
            )
        )
        response = app.test_client().post(
            "/v1/installations/register",
            json={
                "schemaVersion": 1,
                "platform": "android",
                "packageId": "com.northstack.stackwatch",
                "appVersionName": "1.0.22",
                "appVersionCode": 23,
                "holdings": [],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "malformed_request")

    def test_metals_api_provider_uses_one_batch_and_decimal_reciprocal(self) -> None:
        timestamp = int(NOW.timestamp())
        http = FakeHttpClient(
            {
                "success": True,
                "base": "USD",
                "timestamp": timestamp,
                "rates": {
                    "XAU": "0.00025",
                    "XAG": "0.04",
                    "XPT": "0.001",
                    "XPD": "0.0008",
                },
            }
        )
        provider = MetalsApiProvider(access_key="test-key", http_client=http)
        snapshot = provider.fetch_latest(
            now_utc=NOW,
            freshness_window=timedelta(minutes=35),
        )
        self.assertEqual(len(http.urls), 1)
        self.assertIn("symbols=XAU%2CXAG%2CXPT%2CXPD", http.urls[0])
        prices = {item.metal_id: item.usd_per_troy_ounce for item in snapshot.observations}
        self.assertEqual(prices["Gold"], Decimal("4E+3"))
        self.assertEqual(prices["Silver"], Decimal("25"))

    def test_fx_snapshot_provider_validates_required_currencies(self) -> None:
        http = FakeHttpClient(
            {
                "base_code": "USD",
                "time_last_update_unix": int(NOW.timestamp()),
                "rates": {"USD": "1", "AUD": "1.52", "EUR": "0.92"},
            }
        )
        snapshot = FxSnapshotProvider(http_client=http).fetch_latest(
            required_currencies={"AUD", "EUR"},
            now_utc=NOW,
            freshness_window=timedelta(days=1),
        )
        self.assertEqual(snapshot.rate_from_usd("AUD"), Decimal("1.52"))

    def test_budget_hard_stop_and_billing_cycle(self) -> None:
        repository = InMemoryPriceAlertRepository()
        budget = ProviderCallBudget(
            repository=repository,
            provider_id="metals-api",
            plan_limit=5000,
            hard_limit=4800,
            warning_threshold=4500,
            anchor_day=12,
        )
        start, end = billing_cycle(NOW, 12)
        self.assertEqual(start, datetime(2026, 8, 12, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 9, 12, tzinfo=timezone.utc))
        for minute in range(4800):
            repository.record_provider_attempt(
                provider_id="metals-api",
                attempted_at_utc=NOW + timedelta(minutes=minute),
                result="started",
                reason="test",
            )
        decision = budget.decision(NOW)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.state, "budget-paused")

    def test_ten_minute_polling_fits_provider_budget_thresholds(self) -> None:
        self.assertEqual(30 * 24 * 60 // 10, 4320)
        self.assertEqual(31 * 24 * 60 // 10, 4464)
        repository = InMemoryPriceAlertRepository()
        budget = ProviderCallBudget(
            repository=repository,
            provider_id="metals-api",
            plan_limit=5000,
            hard_limit=4800,
            warning_threshold=4500,
            anchor_day=1,
        )
        for minute in range(4500):
            repository.record_provider_attempt(
                provider_id="metals-api",
                attempted_at_utc=NOW + timedelta(minutes=minute),
                result="started",
                reason="test",
            )
        decision = budget.decision(NOW)
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.warning)
        self.assertEqual(decision.state, "warning")

    def test_worker_starts_one_metals_batch_and_counts_failed_attempt(self) -> None:
        repository = InMemoryPriceAlertRepository()
        provider = FailingMetalsProvider()
        worker = PriceAlertsWorker(
            config=PriceAlertsServerConfig(enabled=True),
            repository=repository,
            metals_provider=provider,
            fx_provider=FakeFxProvider(),
        )
        result = worker.run_once(now_utc=NOW)
        self.assertEqual(result.status, "provider-failed")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(
            [item["result"] for item in repository.provider_attempts],
            ["started", "failed"],
        )

    def test_worker_cli_requires_separate_worker_gate(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "BULLIONOVA_PRICE_ALERTS_SERVER_ENABLED": "true",
                "BULLIONOVA_PRICE_ALERTS_WORKER_ENABLED": "false",
            },
            clear=True,
        ):
            self.assertEqual(worker_main(["--once"]), 0)

    def test_migration_status_does_not_require_database_url(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(migrations_main(["--status"]), 0)

    def test_fcm_payload_contains_routing_only(self) -> None:
        payload = price_alert_fcm_payload(
            alert_id="alert-1",
            event_id="event-1",
            source_kind="spot",
            created_at_utc=NOW,
        )
        self.assertEqual(set(payload), {
            "schemaVersion",
            "payloadType",
            "alertId",
            "eventId",
            "sourceKind",
            "createdAtUtc",
        })
        self.assertNotIn("price", payload)
        self.assertNotIn("target", payload)

    def test_worker_triggers_once_and_suppresses_quiet_hours_delivery(self) -> None:
        repository = InMemoryPriceAlertRepository()
        installation_id = "installation-1"
        repository.installations[installation_id] = Installation(
            installation_id=installation_id,
            platform="android",
            package_id="com.northstack.stackwatch",
            app_version_name="1.0.22",
            app_version_code=23,
            locale="en_AU",
            time_zone_id="UTC",
            created_at_utc=NOW,
            updated_at_utc=NOW,
        )
        repository.entitlements[installation_id] = EntitlementState(
            status="active",
            verified_until_utc=NOW + timedelta(hours=1),
            expires_at_utc=NOW + timedelta(days=30),
            last_verified_at_utc=NOW,
        )
        alert = _domain_alert(
            installation_id=installation_id,
            quiet_hours=QuietHoursPolicy(
                enabled=True,
                start_minute=0,
                end_minute=0,
                time_zone_id="UTC",
            ),
        )
        repository.alerts[alert.alert_id] = alert
        worker = PriceAlertsWorker(
            config=PriceAlertsServerConfig(enabled=True),
            repository=repository,
            metals_provider=FakeMetalsProvider(),
            fx_provider=FakeFxProvider(),
        )
        first = worker.run_once(now_utc=NOW)
        second = worker.run_once(now_utc=NOW)
        self.assertEqual(first.status, "ok")
        self.assertEqual(first.triggered, 1)
        self.assertEqual(second.triggered, 0)
        self.assertEqual(len(repository.trigger_events), 1)
        self.assertEqual(next(iter(repository.deliveries.values())).delivery_state, "suppressedQuietHours")

    def test_worker_sends_generic_fcm_payload_with_fake_sender(self) -> None:
        repository = InMemoryPriceAlertRepository()
        installation_id = "installation-1"
        repository.installations[installation_id] = Installation(
            installation_id=installation_id,
            platform="android",
            package_id="com.northstack.stackwatch",
            app_version_name="1.0.22",
            app_version_code=23,
            locale="en_AU",
            time_zone_id="UTC",
            created_at_utc=NOW,
            updated_at_utc=NOW,
        )
        repository.entitlements[installation_id] = EntitlementState(
            status="active",
            verified_until_utc=NOW + timedelta(hours=1),
            expires_at_utc=NOW + timedelta(days=30),
            last_verified_at_utc=NOW,
        )
        repository.alerts["alert-1"] = _domain_alert(installation_id=installation_id)
        token_protector = DeterministicTestTokenProtector()
        protected = token_protector.protect("fake-fcm-token-not-logged")
        repository.upsert_fcm_token(
            installation_id=installation_id,
            token_hash=protected.keyed_hash,
            token_ciphertext=protected.ciphertext,
            key_version=protected.key_version,
            platform="android",
            token_issued_at_utc=NOW,
        )
        fcm_sender = FakeFcmSender()
        worker = PriceAlertsWorker(
            config=PriceAlertsServerConfig(enabled=True),
            repository=repository,
            metals_provider=FakeMetalsProvider(),
            fx_provider=FakeFxProvider(),
            fcm_sender=fcm_sender,
            token_protector=token_protector,
        )
        result = worker.run_once(now_utc=NOW)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.triggered, 1)
        self.assertEqual(len(fcm_sender.sent), 1)
        self.assertEqual(set(fcm_sender.sent[0]), {
            "schemaVersion",
            "payloadType",
            "alertId",
            "eventId",
            "sourceKind",
            "createdAtUtc",
        })
        self.assertNotIn("price", fcm_sender.sent[0])
        self.assertNotIn("target", fcm_sender.sent[0])
        delivery = next(iter(repository.deliveries.values()))
        self.assertEqual(delivery.delivery_state, "delivered")
        self.assertEqual(delivery.provider_message_id, "fake-message-id")

    def test_unit_manifest_matches_expected_python_registry(self) -> None:
        self.assertEqual(len(SUPPORTED_UNITS), 20)
        self.assertEqual(SUPPORTED_UNITS["oz"].grams_per_unit, Decimal("31.1034768"))
        self.assertEqual(SUPPORTED_UNITS["g"].grams_per_unit, Decimal("1"))

    def test_migration_contains_required_durable_tables_and_no_binary_float(self) -> None:
        sql = (BACKEND_DIR / "migrations" / "0001_price_alerts_v1.sql").read_text(
            encoding="utf-8"
        )
        for table in [
            "price_alert_fx_observations",
            "price_alert_idempotency_records",
            "price_alert_provider_usage_records",
            "price_alert_worker_runs",
        ]:
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", sql)
        self.assertNotIn("DOUBLE PRECISION", sql)
        self.assertNotIn(" FLOAT", sql)
        self.assertNotIn(" REAL", sql)

    def test_provider_modules_do_not_import_external_sdks(self) -> None:
        self.assertNotIn("firebase_admin", sys.modules)
        self.assertNotIn("googleapiclient.discovery", sys.modules)


class ServerEvaluatorFixtureParityTests(unittest.TestCase):
    def test_python_evaluator_replays_dart_fixture(self) -> None:
        evaluator = PriceAlertServerEvaluator()
        fixture = json.loads(
            (PROJECT_DIR / "test" / "fixtures" / "price_alert_evaluator_v1.json")
            .read_text(encoding="utf-8")
        )
        for item in fixture["cases"]:
            expected = item["expected"]
            if expected.get("constructionError"):
                continue
            result = evaluator.evaluate(
                alert=_fixture_alert(item),
                observation=_fixture_observation(item),
                evaluated_at_utc=datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
            )
            self.assertEqual(result.reason, expected["reason"], item["name"])
            self.assertEqual(result.triggered, expected["triggered"], item["name"])
            self.assertEqual(result.alert.status, expected["status"], item["name"])
            if "canonicalTarget" in expected:
                self.assertEqual(
                    canonical_decimal_string(result.trigger_event.target),
                    expected["canonicalTarget"],
                    item["name"],
                )


class FakeMetalsProvider:
    provider_id = "metals-api"

    def fetch_latest(self, *, now_utc, freshness_window):
        return SpotProviderSnapshot(
            provider_id=self.provider_id,
            observations=(
                ProviderObservation(
                    observation_id="obs-provider-gold-1",
                    provider_id=self.provider_id,
                    metal_id="Gold",
                    symbol="XAU",
                    usd_per_troy_ounce=Decimal("3600"),
                    provider_timestamp_utc=now_utc - timedelta(minutes=1),
                    received_at_utc=now_utc,
                    raw_base="USD",
                    source_url="https://metals-api.com/api/latest",
                ),
            ),
            provider_timestamp_utc=now_utc - timedelta(minutes=1),
            received_at_utc=now_utc,
        )


class FailingMetalsProvider:
    provider_id = "metals-api"

    def __init__(self) -> None:
        self.calls = 0

    def fetch_latest(self, *, now_utc, freshness_window):
        self.calls += 1
        raise RuntimeError("provider down")


class FakeFxProvider:
    def fetch_latest(self, *, required_currencies, now_utc, freshness_window):
        return FxSnapshot(
            provider_id="fake-fx",
            base_currency_code="USD",
            rates={code: Decimal("1") for code in required_currencies | {"USD"}},
            provider_timestamp_utc=now_utc - timedelta(hours=1),
            received_at_utc=now_utc,
        )


def _domain_alert(
    *,
    installation_id: str,
    quiet_hours: QuietHoursPolicy | None = None,
) -> PriceAlertDefinition:
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
    return PriceAlertDefinition(
        alert_id="alert-1",
        installation_id=installation_id,
        created_at_utc=NOW - timedelta(days=1),
        updated_at_utc=NOW - timedelta(days=1),
        status=STATUS_ARMED,
        source=source,
        metal_id="Gold",
        unit_id="oz",
        alert_currency_code="USD",
        condition=CONDITION_RISES,
        target=Decimal("3500"),
        price_basis=BASIS_PER_UNIT,
        baseline_observation_id="obs-base",
        last_observation_id="obs-base",
        last_comparison_state=COMPARISON_BELOW,
        rearm_required=False,
        restored_review_required=False,
        quiet_hours_policy=quiet_hours or QuietHoursPolicy(),
    )


def _fixture_alert(item) -> PriceAlertDefinition:
    alert = item["alert"]
    status = alert.get("status", STATUS_WAITING)
    unit_id = alert.get("unitId", "oz") if "unitId" in alert else "oz"
    source = _fixture_source({**alert}, unit_id=unit_id)
    return PriceAlertDefinition(
        alert_id=f"alert-{abs(hash(item['name']))}",
        installation_id="installation-1",
        created_at_utc=datetime(2026, 8, 11, tzinfo=timezone.utc),
        updated_at_utc=datetime(2026, 8, 11, tzinfo=timezone.utc),
        status=status,
        source=source,
        metal_id="Gold",
        unit_id=unit_id,
        alert_currency_code="AUD",
        condition=alert.get("condition", "risesToOrAbove"),
        target=Decimal(alert["target"]),
        price_basis=alert.get("priceBasis", "perUnit"),
        baseline_observation_id=alert.get("baselineObservationId"),
        last_observation_id=alert.get("lastObservationId"),
        last_comparison_state=alert.get("lastComparisonState"),
        triggered_at_utc=_parse_optional_utc(alert.get("triggeredAtUtc")),
        triggered_observation_id=alert.get("triggeredObservationId"),
        rearm_required=alert.get("rearmRequired", False),
        quiet_hours_policy=QuietHoursPolicy(
            enabled=alert.get("quietHoursEnabled", False),
            start_minute=0,
            end_minute=0,
            time_zone_id="device" if alert.get("quietHoursEnabled", False) else None,
        ),
        restored_review_required=alert.get("restoredReviewRequired", status == "restoreReviewRequired"),
    )


def _fixture_observation(item) -> PriceAlertObservation:
    observation = item["observation"]
    alert = item["alert"]
    unit_id = observation["unitId"] if "unitId" in observation else "oz"
    source = _fixture_source({**alert, **observation}, unit_id=unit_id)
    return PriceAlertObservation(
        observation_id=observation["observationId"],
        source=source,
        metal_id="Gold",
        price=Decimal(observation["price"]),
        currency_code=observation.get("currencyCode", "AUD"),
        unit_id=unit_id,
        price_basis=observation.get("priceBasis", "perUnit"),
        provider_timestamp_utc=datetime(2026, 8, 11, 11, 55, tzinfo=timezone.utc),
        received_at_utc=datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
        is_authoritative=observation.get("isAuthoritative", True),
        is_cached=observation.get("isCached", False),
        is_stale=observation.get("isStale", False),
        source_available=observation.get("sourceAvailable", True),
        fx_required=observation.get("fxRequired", False),
        fx_timestamp_utc=datetime(2026, 8, 11, 11, tzinfo=timezone.utc)
        if observation.get("fxRequired")
        else None,
        fx_is_stale=observation.get("fxIsStale", False),
        product_available=observation.get("productAvailable"),
        product_in_stock=observation.get("productInStock"),
    )


def _fixture_source(values, *, unit_id):
    source_kind = values.get("sourceKind", "spot")
    price_basis = values.get("priceBasis", "perUnit")
    if source_kind == "spot":
        return PriceAlertSource(
            source_kind="spot",
            provider_id="bullionova-spot",
            metal_id="Gold",
            quote_id=values.get("quoteId", "bullionova-spot:Gold:AUD:oz"),
            source_currency_code="AUD",
            source_unit_id="oz",
            price_basis=price_basis,
            verified=values.get("verified", True),
        )
    return PriceAlertSource(
        source_kind=source_kind,
        provider_id="dealer-world",
        metal_id="Gold",
        dealer_id="dealer-au",
        product_id="gold-1oz" if source_kind == "dealerRetail" else None,
        quote_id=f"dealer-au:gold-1oz:{source_kind}",
        source_currency_code="AUD",
        price_basis=price_basis,
        verified=values.get("verified", True),
    )


def _parse_optional_utc(value):
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _alert_json(alert_id: str) -> dict[str, object]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "alertId": alert_id,
        "createdAtUtc": "2026-08-12T00:00:00Z",
        "updatedAtUtc": "2026-08-12T00:00:00Z",
        "status": "activeWaitingForBaseline",
        "source": {
            "sourceKind": "spot",
            "providerId": "bullionova-spot",
            "metalId": "Gold",
            "quoteId": "bullionova-spot:Gold:AUD:oz",
            "sourceCurrencyCode": "AUD",
            "sourceUnitId": "oz",
            "priceBasis": "perUnit",
            "verified": True,
        },
        "metalId": "Gold",
        "unitId": "oz",
        "alertCurrencyCode": "AUD",
        "condition": "risesToOrAbove",
        "target": "3500",
        "priceBasis": "perUnit",
        "rearmRequired": False,
        "restoredReviewRequired": False,
        "revision": 0,
    }


if __name__ == "__main__":
    unittest.main()
