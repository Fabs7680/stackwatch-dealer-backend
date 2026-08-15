from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .config import PriceAlertsServerConfig
from .contracts import ContractError, utc_iso
from .models import EntitlementState
from .postgres_repository import PostgresPriceAlertRepository
from .synthetic import run_synthetic_spot_crossing


CONTROLLED_STAGING_PACKAGE_ID = "com.northstack.stackwatch"
TEST_PRODUCT_ID = "stackwatch_pro"


class StagingEntitlementRepository(Protocol):
    def installation_package_id(self, installation_id: str) -> str | None:
        ...

    def upsert_test_entitlement(
        self,
        *,
        installation_id: str,
        package_id: str,
        entitlement: EntitlementState,
    ) -> None:
        ...

    def revoke_test_entitlement(self, *, installation_id: str) -> None:
        ...

    def entitlement_for_installation(self, installation_id: str) -> EntitlementState | None:
        ...

    def record_admin_event(
        self,
        *,
        installation_id: str,
        event_type: str,
        details: dict[str, object],
    ) -> None:
        ...


@dataclass(frozen=True)
class TestEntitlementGrant:
    installation_id: str
    package_id: str
    expires_at_utc: datetime


class PostgresStagingEntitlementRepository:
    def __init__(self, *, database_url: str) -> None:
        if not database_url.strip():
            raise ContractError("service_unavailable", "DATABASE_URL is not configured")
        self.database_url = database_url

    def installation_package_id(self, installation_id: str) -> str | None:
        def run(conn):
            row = conn.execute(
                """
                SELECT package_id
                FROM price_alert_installations
                WHERE installation_id = %s AND deleted_at IS NULL
                """,
                (installation_id,),
            ).fetchone()
            return row[0] if row else None

        return self._with_connection(run)

    def upsert_test_entitlement(
        self,
        *,
        installation_id: str,
        package_id: str,
        entitlement: EntitlementState,
    ) -> None:
        entitlement_id = hashlib.sha256(
            f"staging-test:{installation_id}:{TEST_PRODUCT_ID}".encode("utf-8")
        ).hexdigest()
        purchase_token_hash = hashlib.sha256(
            f"staging-test-token:{installation_id}".encode("utf-8")
        ).hexdigest()

        def run(conn) -> None:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO price_alert_entitlements(
                        entitlement_id, installation_id, package_id, product_id,
                        base_plan_id, purchase_token_hash, status,
                        verified_until, expires_at, last_verified_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (entitlement_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        verified_until = EXCLUDED.verified_until,
                        expires_at = EXCLUDED.expires_at,
                        last_verified_at = EXCLUDED.last_verified_at,
                        updated_at = now()
                    """,
                    (
                        entitlement_id,
                        installation_id,
                        package_id,
                        TEST_PRODUCT_ID,
                        "monthly",
                        purchase_token_hash,
                        entitlement.status,
                        entitlement.verified_until_utc,
                        entitlement.expires_at_utc,
                        entitlement.last_verified_at_utc,
                    ),
                )

        self._with_connection(run)

    def revoke_test_entitlement(self, *, installation_id: str) -> None:
        now = datetime.now(timezone.utc)

        def run(conn) -> None:
            conn.execute(
                """
                UPDATE price_alert_entitlements
                SET status = 'inactive',
                    verified_until = %s,
                    expires_at = %s,
                    last_verified_at = %s,
                    updated_at = now()
                WHERE installation_id = %s
                  AND product_id = %s
                  AND purchase_token_hash = %s
                """,
                (
                    now,
                    now,
                    now,
                    installation_id,
                    TEST_PRODUCT_ID,
                    hashlib.sha256(
                        f"staging-test-token:{installation_id}".encode("utf-8")
                    ).hexdigest(),
                ),
            )

        self._with_connection(run)

    def entitlement_for_installation(self, installation_id: str) -> EntitlementState | None:
        def run(conn):
            row = conn.execute(
                """
                SELECT status, verified_until, expires_at, last_verified_at
                FROM price_alert_entitlements
                WHERE installation_id = %s AND product_id = %s
                ORDER BY last_verified_at DESC
                LIMIT 1
                """,
                (installation_id, TEST_PRODUCT_ID),
            ).fetchone()
            if row is None:
                return None
            return EntitlementState(
                status=row[0],
                verified_until_utc=row[1],
                expires_at_utc=row[2],
                last_verified_at_utc=row[3],
            )

        return self._with_connection(run)

    def record_admin_event(
        self,
        *,
        installation_id: str,
        event_type: str,
        details: dict[str, object],
    ) -> None:
        event_id = hashlib.sha256(
            f"{installation_id}:{event_type}:{datetime.now(timezone.utc).isoformat()}".encode(
                "utf-8"
            )
        ).hexdigest()

        def run(conn) -> None:
            conn.execute(
                """
                INSERT INTO price_alert_security_events(
                    security_event_id, installation_id, event_type, severity, metadata, created_at
                ) VALUES (%s, %s, %s, 'info', %s::jsonb, now())
                """,
                (event_id, installation_id, event_type, json.dumps(details, sort_keys=True)),
            )

        self._with_connection(run)

    def _with_connection(self, callback):
        try:
            import psycopg
        except Exception as exc:  # pragma: no cover - exercised in deployment.
            raise ContractError("service_unavailable", "psycopg dependency unavailable") from exc
        with psycopg.connect(self.database_url) as conn:
            return callback(conn)


def grant_test_entitlement(
    *,
    config: PriceAlertsServerConfig,
    repository: StagingEntitlementRepository,
    installation_id: str,
    package_id: str,
    ttl_hours: int,
    now_utc: datetime | None = None,
) -> TestEntitlementGrant:
    now = _utc(now_utc)
    _require_staging_test_mode(config)
    _require_configured_package(package_id, config)
    max_ttl = min(config.max_test_entitlement_ttl_hours, 24)
    if ttl_hours < 1 or ttl_hours > max_ttl:
        raise ContractError("malformed_request", "TTL must be between 1 and 24 hours")
    registered_package = repository.installation_package_id(installation_id)
    if registered_package != config.package_id:
        raise ContractError(
            "entitlement_invalid",
            "Installation is not registered to the configured package",
        )
    expires_at = now + timedelta(hours=ttl_hours)
    entitlement = EntitlementState(
        status="active",
        verified_until_utc=expires_at,
        expires_at_utc=expires_at,
        last_verified_at_utc=now,
    )
    repository.upsert_test_entitlement(
        installation_id=installation_id,
        package_id=package_id,
        entitlement=entitlement,
    )
    repository.record_admin_event(
        installation_id=installation_id,
        event_type="staging_test_entitlement_granted",
        details={
            "packageId": package_id,
            "productId": TEST_PRODUCT_ID,
            "expiresAtUtc": utc_iso(expires_at),
        },
    )
    return TestEntitlementGrant(
        installation_id=installation_id,
        package_id=package_id,
        expires_at_utc=expires_at,
    )


def revoke_test_entitlement(
    *,
    config: PriceAlertsServerConfig,
    repository: StagingEntitlementRepository,
    installation_id: str,
) -> None:
    _require_staging_test_mode(config)
    repository.revoke_test_entitlement(installation_id=installation_id)
    repository.record_admin_event(
        installation_id=installation_id,
        event_type="staging_test_entitlement_revoked",
        details={"productId": TEST_PRODUCT_ID},
    )


def status_for_installation(
    *,
    config: PriceAlertsServerConfig,
    repository: StagingEntitlementRepository,
    installation_id: str,
) -> dict[str, object]:
    _require_staging_test_mode(config)
    entitlement = repository.entitlement_for_installation(installation_id)
    return {
        "installationId": installation_id,
        "productId": TEST_PRODUCT_ID,
        "status": entitlement.status if entitlement else "inactive",
        **(
            {"expiresAtUtc": utc_iso(entitlement.expires_at_utc)}
            if entitlement and entitlement.expires_at_utc
            else {}
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bullionova Price Alerts admin")
    sub = parser.add_subparsers(dest="command", required=True)
    grant = sub.add_parser("grant-test-entitlement")
    grant.add_argument("--installation-id", required=True)
    grant.add_argument("--package-id", required=True)
    grant.add_argument("--ttl-hours", required=True, type=int)
    revoke = sub.add_parser("revoke-test-entitlement")
    revoke.add_argument("--installation-id", required=True)
    status = sub.add_parser("status")
    status.add_argument("--installation-id", required=True)
    synthetic = sub.add_parser("run-synthetic-spot-crossing")
    synthetic.add_argument("--installation-id", required=True)
    synthetic.add_argument("--metal-id", required=True)
    synthetic.add_argument("--usd-per-troy-ounce", required=True)
    args = parser.parse_args(argv)

    config = PriceAlertsServerConfig.from_env()
    try:
        if args.command == "run-synthetic-spot-crossing":
            _require_synthetic_admin_mode(config)
        else:
            _require_staging_test_mode(config)
        admin_repository = PostgresStagingEntitlementRepository(
            database_url=config.database_url
        )
        if args.command == "grant-test-entitlement":
            result = grant_test_entitlement(
                config=config,
                repository=admin_repository,
                installation_id=args.installation_id,
                package_id=args.package_id,
                ttl_hours=args.ttl_hours,
            )
            print(
                json.dumps(
                    {
                        "installationId": result.installation_id,
                        "packageId": result.package_id,
                        "productId": TEST_PRODUCT_ID,
                        "expiresAtUtc": utc_iso(result.expires_at_utc),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "revoke-test-entitlement":
            revoke_test_entitlement(
                config=config,
                repository=admin_repository,
                installation_id=args.installation_id,
            )
            print(json.dumps({"installationId": args.installation_id, "status": "revoked"}))
            return 0
        if args.command == "status":
            print(
                json.dumps(
                    status_for_installation(
                        config=config,
                        repository=admin_repository,
                        installation_id=args.installation_id,
                    ),
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "run-synthetic-spot-crossing":
            result = run_synthetic_spot_crossing(
                config=config,
                repository=PostgresPriceAlertRepository(database_url=config.database_url),
                audit_repository=admin_repository,
                installation_id=args.installation_id,
                metal_id=args.metal_id,
                usd_per_troy_ounce=args.usd_per_troy_ounce,
            )
            print(
                json.dumps(
                    {
                        "status": result.worker_result.status,
                        "evaluated": result.worker_result.evaluated,
                        "triggered": result.worker_result.triggered,
                        "deliveriesCreated": result.worker_result.deliveries_created,
                        "observationId": result.observation_id,
                    },
                    sort_keys=True,
                )
            )
            return 0
        raise ContractError("malformed_request", "Unknown admin command")
    except ContractError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": exc.code,
                    "message": exc.message,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


def _require_staging_test_mode(config: PriceAlertsServerConfig) -> None:
    if config.environment != "staging":
        raise ContractError("entitlement_invalid", "Test entitlements require staging")
    if not config.enabled:
        raise ContractError("service_unavailable", "Price Alerts server is disabled")
    if not config.allow_test_entitlements:
        raise ContractError("entitlement_invalid", "Test entitlements are disabled")


def _require_configured_package(package_id: str, config: PriceAlertsServerConfig) -> None:
    if package_id != config.package_id:
        raise ContractError(
            "entitlement_invalid",
            "Only the configured staging package can receive test entitlement",
        )


def _require_synthetic_admin_mode(config: PriceAlertsServerConfig) -> None:
    _require_staging_test_mode(config)
    if not config.allow_synthetic_quotes:
        raise ContractError("entitlement_invalid", "Synthetic quotes are disabled")


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
