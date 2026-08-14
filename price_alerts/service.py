from __future__ import annotations

import secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .config import PriceAlertsServerConfig
from .contracts import (
    ContractError,
    FcmTokenRequest,
    Idempotency,
    InstallationRegistrationRequest,
    InstallationRegistrationResponse,
    SCHEMA_VERSION,
    canonical_decimal,
    parse_currency,
    parse_stable_enum,
    parse_string,
    parse_utc_timestamp,
    utc_iso,
)
from .decimal_utils import canonical_decimal_string, parse_decimal
from .evaluator import pause_alert, rearm_alert, resume_alert
from .models import (
    BASIS_PER_UNIT,
    CONDITION_FALLS,
    CONDITION_RISES,
    EntitlementState,
    Installation,
    NotificationPreferences,
    PriceAlertDefinition,
    PriceAlertSource,
    PriceAlertTriggerEvent,
    QuietHoursPolicy,
    SOURCE_DEALER_BUYBACK,
    SOURCE_DEALER_RETAIL,
    SOURCE_SPOT,
    STATUS_WAITING,
    utc_now,
)
from .play import PlayVerifier, entitlement_from_play_result
from .repository import PriceAlertRepository
from .security import (
    DeterministicTestTokenProtector,
    InstallationSecretHasher,
    TokenProtector,
    new_public_id,
    new_secret,
)


FORBIDDEN_PORTFOLIO_FIELDS = {
    "holdings",
    "portfolio",
    "portfolioValue",
    "stackValue",
    "purchaseHistory",
    "purchaseNotes",
    "holdingNotes",
    "notes",
    "backupPassword",
}


class PriceAlertServerService:
    def __init__(
        self,
        *,
        config: PriceAlertsServerConfig,
        repository: PriceAlertRepository,
        play_verifier: PlayVerifier,
        token_protector: TokenProtector | DeterministicTestTokenProtector,
        secret_hasher: InstallationSecretHasher | None = None,
    ) -> None:
        self.config = config
        self.repository = repository
        self.play_verifier = play_verifier
        self.token_protector = token_protector
        self.secret_hasher = secret_hasher or InstallationSecretHasher()

    def register_installation(self, payload: dict[str, Any]) -> dict[str, Any]:
        reject_forbidden_portfolio_fields(payload)
        request = InstallationRegistrationRequest.from_json(payload)
        if request.package_id != self.config.package_id:
            raise ContractError("malformed_request", "Unsupported package ID")
        now = utc_now()
        installation_id = new_public_id("installation")
        secret = new_secret()
        encoded_hash = self.secret_hasher.hash_secret(secret).encode()
        self.repository.create_installation(
            installation=Installation(
                installation_id=installation_id,
                platform=request.platform,
                package_id=request.package_id,
                app_version_name=request.app_version_name,
                app_version_code=request.app_version_code,
                locale=request.locale,
                time_zone_id=request.time_zone_id,
                created_at_utc=now,
                updated_at_utc=now,
            ),
            encoded_secret_hash=encoded_hash,
            preferences=NotificationPreferences(updated_at_utc=now),
        )
        return InstallationRegistrationResponse(
            installation_id=installation_id,
            installation_secret=secret,
            created_at_utc=now,
            server_time_utc=now,
        ).to_json()

    def authenticate(self, *, installation_id: str, installation_secret: str) -> None:
        encoded = self.repository.credential_hash_for_installation(installation_id)
        if encoded is None:
            raise ContractError("unauthorised_installation", "Installation credentials invalid")
        if not self.secret_hasher.verify(installation_secret, encoded):
            raise ContractError("unauthorised_installation", "Installation credentials invalid")

    def update_installation_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        reject_forbidden_portfolio_fields(payload)
        require_schema(payload)
        installation_id = parse_string(payload.get("installationId"), "installationId")
        preferences = _preferences_from_json(payload.get("notificationPreferences") or {})
        self.repository.update_installation_settings(
            installation_id=installation_id,
            locale=_optional_string(payload.get("locale")),
            time_zone_id=_optional_string(payload.get("timeZoneId")),
            preferences=preferences,
        )
        return {"schemaVersion": SCHEMA_VERSION, "ok": True, "serverTimeUtc": utc_iso(utc_now())}

    def verify_entitlement(self, payload: dict[str, Any]) -> dict[str, Any]:
        reject_forbidden_portfolio_fields(payload)
        require_schema(payload)
        installation_id = parse_string(payload.get("installationId"), "installationId")
        package_id = parse_string(payload.get("packageId"), "packageId")
        product_id = parse_string(payload.get("productId"), "productId")
        base_plan_id = parse_string(payload.get("basePlanId"), "basePlanId")
        purchase_token = parse_string(payload.get("playPurchaseToken"), "playPurchaseToken")
        Idempotency.from_json(payload.get("idempotency") or {})
        if package_id != self.config.package_id or product_id != self.config.product_id:
            raise ContractError("entitlement_invalid", "Unsupported package or product")
        now = utc_now()
        result = self.play_verifier.verify_subscription(
            package_id=package_id,
            product_id=product_id,
            base_plan_id=base_plan_id,
            purchase_token=purchase_token,
            now_utc=now,
        )
        entitlement = entitlement_from_play_result(result, now_utc=now)
        self.repository.upsert_entitlement(
            installation_id=installation_id,
            entitlement=entitlement,
        )
        return {
            "schemaVersion": SCHEMA_VERSION,
            "status": entitlement.status,
            "serverTimeUtc": utc_iso(now),
            **(
                {"verifiedUntilUtc": utc_iso(entitlement.verified_until_utc)}
                if entitlement.verified_until_utc
                else {}
            ),
            **(
                {"expiresAtUtc": utc_iso(entitlement.expires_at_utc)}
                if entitlement.expires_at_utc
                else {}
            ),
        }

    def entitlement_status(self, *, installation_id: str) -> dict[str, Any]:
        entitlement = self.repository.entitlement_for_installation(installation_id)
        now = utc_now()
        status = entitlement.status if entitlement is not None else "inactive"
        return {
            "schemaVersion": SCHEMA_VERSION,
            "status": status,
            "serverTimeUtc": utc_iso(now),
            **(
                {"verifiedUntilUtc": utc_iso(entitlement.verified_until_utc)}
                if entitlement and entitlement.verified_until_utc
                else {}
            ),
            **(
                {"expiresAtUtc": utc_iso(entitlement.expires_at_utc)}
                if entitlement and entitlement.expires_at_utc
                else {}
            ),
        }

    def upsert_fcm_token(self, payload: dict[str, Any]) -> dict[str, Any]:
        reject_forbidden_portfolio_fields(payload)
        request = FcmTokenRequest.from_json(payload)
        now = utc_now()
        if not self.config.fcm_enabled:
            raise ContractError("service_unavailable", "FCM registration is disabled")
        entitlement = self.repository.entitlement_for_installation(request.installation_id)
        if entitlement is None or not _entitlement_current(entitlement, now):
            raise ContractError("entitlement_required", "Bullionova Pro entitlement required")
        preferences = self.repository.notification_preferences_for_installation(
            request.installation_id
        )
        if preferences is None or not preferences.notifications_enabled:
            raise ContractError("malformed_request", "Notifications are disabled")
        protected = self.token_protector.protect(request.fcm_token)
        self.repository.upsert_fcm_token(
            installation_id=request.installation_id,
            token_hash=protected.keyed_hash,
            token_ciphertext=protected.ciphertext,
            key_version=protected.key_version,
            platform=request.platform,
            token_issued_at_utc=request.token_issued_at_utc,
        )
        return {"schemaVersion": SCHEMA_VERSION, "ok": True, "serverTimeUtc": utc_iso(now)}

    def delete_fcm_token(self, payload: dict[str, Any]) -> dict[str, Any]:
        reject_forbidden_portfolio_fields(payload)
        require_schema(payload)
        installation_id = parse_string(payload.get("installationId"), "installationId")
        token = _optional_string(payload.get("fcmToken"))
        token_hash = self.token_protector.protect(token).keyed_hash if token else None
        Idempotency.from_json(payload.get("idempotency") or {})
        self.repository.revoke_fcm_token(
            installation_id=installation_id,
            token_hash=token_hash,
        )
        return {"schemaVersion": SCHEMA_VERSION, "ok": True, "serverTimeUtc": utc_iso(utc_now())}

    def upsert_alert(self, *, alert_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        reject_forbidden_portfolio_fields(payload)
        require_schema(payload)
        installation_id = parse_string(payload.get("installationId"), "installationId")
        Idempotency.from_json(payload.get("idempotency") or {})
        alert = _alert_from_json(
            payload.get("alert") or {},
            installation_id=installation_id,
            fallback_alert_id=alert_id,
        )
        if alert.source.source_kind in {SOURCE_DEALER_RETAIL, SOURCE_DEALER_BUYBACK}:
            raise ContractError("dealer_source_unavailable", "Dealer alerts are not available")
        if (
            self.repository.count_resumable_alerts(
                installation_id=installation_id,
                exclude_alert_id=alert.alert_id,
            )
            >= self.config.max_resumable_alerts
            and alert.counts_toward_limit
        ):
            raise ContractError("alert_limit_reached", "Alert limit reached")
        entitlement = self.repository.entitlement_for_installation(installation_id)
        if entitlement is None or not entitlement.is_active:
            raise ContractError("entitlement_required", "Bullionova Pro entitlement required")
        saved = self.repository.upsert_alert(alert)
        return _alert_response(saved)

    def alert_action(self, *, installation_id: str, alert_id: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        reject_forbidden_portfolio_fields(payload)
        require_schema(payload)
        Idempotency.from_json(payload.get("idempotency") or {})
        alert = self.repository.alert(installation_id=installation_id, alert_id=alert_id)
        if alert is None:
            raise ContractError("malformed_request", "Alert not found")
        now = utc_now()
        if action == "pause":
            updated = pause_alert(alert, updated_at_utc=now)
        elif action == "resume":
            updated = resume_alert(alert, updated_at_utc=now)
        elif action == "rearm":
            updated = rearm_alert(alert, updated_at_utc=now)
        else:
            raise ContractError("malformed_request", "Unknown alert action")
        return _alert_response(self.repository.update_alert(updated))

    def delete_alert(self, *, installation_id: str, alert_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        reject_forbidden_portfolio_fields(payload)
        if payload:
            require_schema(payload)
            Idempotency.from_json(payload.get("idempotency") or {})
        self.repository.delete_alert(
            installation_id=installation_id,
            alert_id=alert_id,
            deleted_at_utc=utc_now(),
        )
        return {"schemaVersion": SCHEMA_VERSION, "ok": True, "serverTimeUtc": utc_iso(utc_now())}

    def delete_all_alerts(self, payload: dict[str, Any]) -> dict[str, Any]:
        reject_forbidden_portfolio_fields(payload)
        require_schema(payload)
        installation_id = parse_string(payload.get("installationId"), "installationId")
        Idempotency.from_json(payload.get("idempotency") or {})
        self.repository.delete_all_alerts(
            installation_id=installation_id,
            deleted_at_utc=utc_now(),
        )
        return {"schemaVersion": SCHEMA_VERSION, "ok": True, "serverTimeUtc": utc_iso(utc_now())}

    def sync_alerts(self, *, installation_id: str) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "records": [_alert_to_json(alert) for alert in self.repository.list_alerts(
                installation_id=installation_id,
                limit=self.config.max_resumable_alerts,
            )],
            "hasMore": False,
            "serverTimeUtc": utc_iso(utc_now()),
        }

    def sync_events(self, *, installation_id: str) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "records": [_event_to_json(event) for event in self.repository.list_trigger_events(
                installation_id=installation_id,
                limit=self.config.max_visible_trigger_events,
            )],
            "hasMore": False,
            "serverTimeUtc": utc_iso(utc_now()),
        }


def require_schema(payload: dict[str, Any]) -> None:
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise ContractError("unsupported_schema", "Unsupported schema version")


def reject_forbidden_portfolio_fields(payload: Any) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in FORBIDDEN_PORTFOLIO_FIELDS:
                raise ContractError("malformed_request", "Forbidden portfolio field")
            reject_forbidden_portfolio_fields(value)
    elif isinstance(payload, list):
        for item in payload:
            reject_forbidden_portfolio_fields(item)


def _preferences_from_json(value: dict[str, Any]) -> NotificationPreferences:
    quiet = value.get("quietHoursPolicy") if isinstance(value, dict) else {}
    if not isinstance(quiet, dict):
        quiet = {}
    return NotificationPreferences(
        notifications_enabled=value.get("notificationsEnabled") is not False,
        show_price_details_in_notifications=value.get("showPriceDetailsInNotifications") is True,
        quiet_hours_policy=QuietHoursPolicy(
            enabled=quiet.get("enabled") is True,
            start_minute=int(quiet.get("startMinute") or 0),
            end_minute=int(quiet.get("endMinute") or 0),
            time_zone_id=_optional_string(quiet.get("timeZoneId")),
        ),
        revision=int(value.get("revision") or 0),
        updated_at_utc=utc_now(),
    )


def _entitlement_current(entitlement: EntitlementState, now_utc: datetime) -> bool:
    if not entitlement.is_active:
        return False
    if entitlement.verified_until_utc is not None and entitlement.verified_until_utc < now_utc:
        return False
    if entitlement.expires_at_utc is not None and entitlement.expires_at_utc < now_utc:
        return False
    return True


def _alert_from_json(
    value: dict[str, Any],
    *,
    installation_id: str,
    fallback_alert_id: str,
) -> PriceAlertDefinition:
    if not isinstance(value, dict):
        raise ContractError("malformed_request", "alert must be an object")
    require_schema(value)
    source = _source_from_json(value.get("source") or {})
    status = parse_stable_enum(
        value.get("status"),
        {
            "draft",
            "activeWaitingForBaseline",
            "activeArmed",
            "triggeredNeedsRearm",
            "paused",
            "sourceUnavailable",
            "notificationPermissionRequired",
            "proSuspended",
            "restoreReviewRequired",
        },
    )
    condition = parse_stable_enum(value.get("condition"), {CONDITION_RISES, CONDITION_FALLS})
    price_basis = parse_stable_enum(value.get("priceBasis"), {BASIS_PER_UNIT, "productTotal"})
    alert_id = parse_string(value.get("alertId") or fallback_alert_id, "alertId")
    if alert_id != fallback_alert_id:
        raise ContractError("conflict", "Path alert ID does not match payload")
    unit_id = _optional_string(value.get("unitId"))
    now = utc_now()
    created_at = _optional_utc(value.get("createdAtUtc")) or now
    updated_at = _optional_utc(value.get("updatedAtUtc")) or now
    return PriceAlertDefinition(
        alert_id=alert_id,
        installation_id=installation_id,
        created_at_utc=created_at,
        updated_at_utc=updated_at,
        status=status,
        source=source,
        metal_id=parse_string(value.get("metalId"), "metalId"),
        unit_id=unit_id,
        alert_currency_code=parse_currency(value.get("alertCurrencyCode")),
        condition=condition,
        target=parse_decimal(value.get("target"), positive=True),
        price_basis=price_basis,
        baseline_observation_id=_optional_string(value.get("baselineObservationId")),
        last_observation_id=_optional_string(value.get("lastObservationId")),
        last_comparison_state=_optional_string(value.get("lastComparisonState")),
        triggered_at_utc=_optional_utc(value.get("triggeredAtUtc")),
        triggered_observation_id=_optional_string(value.get("triggeredObservationId")),
        rearm_required=value.get("rearmRequired") is True,
        quiet_hours_policy=_preferences_from_json({"quietHoursPolicy": value.get("quietHoursPolicy")}).quiet_hours_policy,
        restored_review_required=value.get("restoredReviewRequired") is True,
        revision=int(value.get("revision") or 0),
    )


def _source_from_json(value: dict[str, Any]) -> PriceAlertSource:
    if not isinstance(value, dict):
        raise ContractError("malformed_request", "source must be an object")
    source_kind = parse_stable_enum(value.get("sourceKind"), {SOURCE_SPOT, SOURCE_DEALER_RETAIL, SOURCE_DEALER_BUYBACK})
    price_basis = parse_stable_enum(value.get("priceBasis"), {BASIS_PER_UNIT, "productTotal"})
    return PriceAlertSource(
        source_kind=source_kind,
        provider_id=parse_string(value.get("providerId"), "providerId"),
        metal_id=parse_string(value.get("metalId"), "metalId"),
        dealer_id=_optional_string(value.get("dealerId")),
        dealer_country_code=_optional_string(value.get("dealerCountryCode")),
        product_id=_optional_string(value.get("productId")),
        quote_id=parse_string(value.get("quoteId"), "quoteId"),
        quote_side=_optional_string(value.get("quoteSide")),
        source_currency_code=parse_currency(value.get("sourceCurrencyCode")),
        source_unit_id=_optional_string(value.get("sourceUnitId")),
        price_basis=price_basis,
        source_url=_optional_string(value.get("sourceUrl")),
        verified=value.get("verified") is True,
    )


def _alert_response(alert: PriceAlertDefinition) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "alert": _alert_to_json(alert),
        "serverTimeUtc": utc_iso(utc_now()),
    }


def _source_to_json(source: PriceAlertSource) -> dict[str, Any]:
    return {
        "sourceKind": source.source_kind,
        "providerId": source.provider_id,
        "metalId": source.metal_id,
        **({"dealerId": source.dealer_id} if source.dealer_id else {}),
        **({"dealerCountryCode": source.dealer_country_code} if source.dealer_country_code else {}),
        **({"productId": source.product_id} if source.product_id else {}),
        "quoteId": source.quote_id,
        **({"quoteSide": source.quote_side} if source.quote_side else {}),
        "sourceCurrencyCode": source.source_currency_code,
        **({"sourceUnitId": source.source_unit_id} if source.source_unit_id else {}),
        "priceBasis": source.price_basis,
        **({"sourceUrl": source.source_url} if source.source_url else {}),
        "verified": source.verified,
    }


def _alert_to_json(alert: PriceAlertDefinition) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "alertId": alert.alert_id,
        "createdAtUtc": utc_iso(alert.created_at_utc),
        "updatedAtUtc": utc_iso(alert.updated_at_utc),
        "status": alert.status,
        "source": _source_to_json(alert.source),
        "metalId": alert.metal_id,
        **({"unitId": alert.unit_id} if alert.unit_id else {}),
        "alertCurrencyCode": alert.alert_currency_code,
        "condition": alert.condition,
        "target": canonical_decimal_string(alert.target),
        "priceBasis": alert.price_basis,
        **({"baselineObservationId": alert.baseline_observation_id} if alert.baseline_observation_id else {}),
        **({"lastObservationId": alert.last_observation_id} if alert.last_observation_id else {}),
        **({"lastComparisonState": alert.last_comparison_state} if alert.last_comparison_state else {}),
        **({"triggeredAtUtc": utc_iso(alert.triggered_at_utc)} if alert.triggered_at_utc else {}),
        **({"triggeredObservationId": alert.triggered_observation_id} if alert.triggered_observation_id else {}),
        "rearmRequired": alert.rearm_required,
        "quietHoursPolicy": {
            "enabled": alert.quiet_hours_policy.enabled,
            "startMinute": alert.quiet_hours_policy.start_minute,
            "endMinute": alert.quiet_hours_policy.end_minute,
            **({"timeZoneId": alert.quiet_hours_policy.time_zone_id} if alert.quiet_hours_policy.time_zone_id else {}),
        },
        "restoredReviewRequired": alert.restored_review_required,
        "revision": alert.revision,
    }


def _event_to_json(event: PriceAlertTriggerEvent) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "eventId": event.event_id,
        "alertId": event.alert_id,
        "triggeredAtUtc": utc_iso(event.triggered_at_utc),
        "observationId": event.observation_id,
        "source": _source_to_json(event.source),
        "metalId": event.metal_id,
        "condition": event.condition,
        "target": canonical_decimal_string(event.target),
        "triggeredPrice": canonical_decimal_string(event.triggered_price),
        "alertCurrencyCode": event.alert_currency_code,
        **({"unitId": event.unit_id} if event.unit_id else {}),
        "priceBasis": event.price_basis,
        "providerTimestampUtc": utc_iso(event.provider_timestamp_utc),
        **({"dealerId": event.dealer_id} if event.dealer_id else {}),
        **({"productId": event.product_id} if event.product_id else {}),
        "revision": event.revision,
    }


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractError("malformed_request", "Optional string is invalid")
    stripped = value.strip()
    return stripped or None


def _optional_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    return parse_utc_timestamp(value)
