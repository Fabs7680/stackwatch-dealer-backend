from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

SCHEMA_VERSION = 1

API_PATHS = {
    "register_installation": "POST /v1/installations/register",
    "update_installation_settings": "PATCH /v1/installations/settings",
    "verify_entitlement": "POST /v1/entitlements/verify",
    "upsert_alert": "PUT /v1/alerts/{alertId}",
    "pause_alert": "POST /v1/alerts/{alertId}:pause",
    "resume_alert": "POST /v1/alerts/{alertId}:resume",
    "rearm_alert": "POST /v1/alerts/{alertId}:rearm",
    "delete_alert": "DELETE /v1/alerts/{alertId}",
    "delete_all_price_alerts": "DELETE /v1/price-alerts",
    "sync_alerts": "GET /v1/alerts/sync",
    "sync_events": "GET /v1/events/sync",
    "upsert_fcm_token": "POST /v1/fcm-token",
    "delete_fcm_token": "DELETE /v1/fcm-token",
    "health": "GET /v1/health",
}

ERROR_CODES = {
    "unsupported_schema",
    "malformed_request",
    "invalid_decimal",
    "invalid_timestamp",
    "invalid_enum",
    "invalid_cursor",
    "invalid_idempotency_key",
    "unauthorised_installation",
    "entitlement_required",
    "entitlement_invalid",
    "alert_limit_reached",
    "dealer_source_unavailable",
    "stale_observation",
    "non_authoritative_observation",
    "conflict",
    "rate_limited",
    "service_unavailable",
}

SOURCE_KINDS = {"spot", "dealerRetail", "dealerBuyback"}
PRICE_BASES = {"perUnit", "productTotal"}
CONDITIONS = {"risesToOrAbove", "fallsToOrBelow"}
STATUSES = {
    "draft",
    "activeWaitingForBaseline",
    "activeArmed",
    "triggeredNeedsRearm",
    "paused",
    "sourceUnavailable",
    "notificationPermissionRequired",
    "proSuspended",
    "restoreReviewRequired",
}
COMPARISON_STATES = {"below", "equal", "above"}
PLATFORMS = {"android", "ios", "web", "desktop"}

_IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")
_CURSOR_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")
_DECIMAL_PATTERN = re.compile(r"^[+]?\d+(?:\.\d+)?$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


class ContractError(ValueError):
    def __init__(self, code: str, message: str):
        if code not in ERROR_CODES:
            raise ValueError(f"Unknown contract error code: {code}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def require_schema(value: dict[str, Any]) -> None:
    if value.get("schemaVersion") != SCHEMA_VERSION:
        raise ContractError("unsupported_schema", "Unsupported schema version")


def parse_stable_enum(value: Any, allowed: set[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ContractError("invalid_enum", "Unknown enum value")
    return value


def parse_utc_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ContractError("invalid_timestamp", "Timestamp must be a string")
    candidate = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ContractError("invalid_timestamp", "Invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ContractError("invalid_timestamp", "Timestamp must be UTC")
    return parsed.astimezone(timezone.utc)


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_decimal(value: Any) -> str:
    if not isinstance(value, str) or not _DECIMAL_PATTERN.fullmatch(value):
        raise ContractError("invalid_decimal", "Decimal must be a string")
    try:
        decimal = Decimal(value)
    except InvalidOperation as exc:
        raise ContractError("invalid_decimal", "Invalid decimal") from exc
    if not decimal.is_finite() or decimal <= 0:
        raise ContractError("invalid_decimal", "Decimal must be positive")
    normalized = decimal.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f").split(".")[0]
    return format(normalized, "f")


def parse_currency(value: Any) -> str:
    if not isinstance(value, str) or not _CURRENCY_PATTERN.fullmatch(value):
        raise ContractError("malformed_request", "Currency must be ISO-4217")
    return value


def parse_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError("malformed_request", f"{field} is required")
    return value.strip()


def redact(value: str) -> str:
    if len(value) <= 6:
        return "[redacted]"
    return f"{value[:3]}...[redacted]"


@dataclass(frozen=True)
class Idempotency:
    key: str
    created_at_utc: datetime

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "Idempotency":
        key = parse_string(value.get("key"), "idempotency.key")
        if not _IDEMPOTENCY_PATTERN.fullmatch(key):
            raise ContractError("invalid_idempotency_key", "Invalid idempotency key")
        return cls(
            key=key,
            created_at_utc=parse_utc_timestamp(value.get("createdAtUtc")),
        )

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key, "createdAtUtc": utc_iso(self.created_at_utc)}


@dataclass(frozen=True)
class SyncCursor:
    value: str

    @classmethod
    def from_json(cls, value: Any) -> "SyncCursor":
        raw = value if isinstance(value, str) else value.get("value")
        raw = parse_string(raw, "cursor")
        if not _CURSOR_PATTERN.fullmatch(raw):
            raise ContractError("invalid_cursor", "Invalid cursor")
        return cls(value=raw)

    def to_json(self) -> str:
        return self.value


@dataclass(frozen=True)
class ApiErrorResponse:
    code: str
    message: str
    request_id: str | None = None

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "ApiErrorResponse":
        require_schema(value)
        code = parse_stable_enum(value.get("code"), ERROR_CODES)
        return cls(
            code=code,
            message=parse_string(value.get("message"), "message"),
            request_id=value.get("requestId"),
        )

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "code": self.code,
            "message": self.message,
        }
        if self.request_id:
            payload["requestId"] = self.request_id
        return payload


@dataclass(frozen=True)
class InstallationRegistrationRequest:
    platform: str
    package_id: str
    app_version_name: str
    app_version_code: int
    locale: str | None = None
    time_zone_id: str | None = None

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "InstallationRegistrationRequest":
        require_schema(value)
        code = value.get("appVersionCode")
        if not isinstance(code, int) or code <= 0:
            raise ContractError("malformed_request", "appVersionCode must be positive")
        return cls(
            platform=parse_stable_enum(value.get("platform"), PLATFORMS),
            package_id=parse_string(value.get("packageId"), "packageId"),
            app_version_name=parse_string(value.get("appVersionName"), "appVersionName"),
            app_version_code=code,
            locale=value.get("locale"),
            time_zone_id=value.get("timeZoneId"),
        )


@dataclass(frozen=True)
class InstallationRegistrationResponse:
    installation_id: str
    installation_secret: str
    created_at_utc: datetime
    server_time_utc: datetime

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "InstallationRegistrationResponse":
        require_schema(value)
        return cls(
            installation_id=parse_string(value.get("installationId"), "installationId"),
            installation_secret=parse_string(
                value.get("installationSecret"), "installationSecret"
            ),
            created_at_utc=parse_utc_timestamp(value.get("createdAtUtc")),
            server_time_utc=parse_utc_timestamp(value.get("serverTimeUtc")),
        )

    def __repr__(self) -> str:
        return (
            "InstallationRegistrationResponse("
            f"installation_id={self.installation_id!r}, "
            f"installation_secret={redact(self.installation_secret)!r})"
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "installationId": self.installation_id,
            "installationSecret": self.installation_secret,
            "createdAtUtc": utc_iso(self.created_at_utc),
            "serverTimeUtc": utc_iso(self.server_time_utc),
        }


@dataclass(frozen=True)
class EntitlementVerificationRequest:
    installation_id: str
    package_id: str
    product_id: str
    base_plan_id: str
    play_purchase_token: str
    idempotency: Idempotency

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "EntitlementVerificationRequest":
        require_schema(value)
        return cls(
            installation_id=parse_string(value.get("installationId"), "installationId"),
            package_id=parse_string(value.get("packageId"), "packageId"),
            product_id=parse_string(value.get("productId"), "productId"),
            base_plan_id=parse_string(value.get("basePlanId"), "basePlanId"),
            play_purchase_token=parse_string(
                value.get("playPurchaseToken"), "playPurchaseToken"
            ),
            idempotency=Idempotency.from_json(value.get("idempotency") or {}),
        )

    def __repr__(self) -> str:
        return (
            "EntitlementVerificationRequest("
            f"installation_id={self.installation_id!r}, "
            f"product_id={self.product_id!r}, "
            f"play_purchase_token={redact(self.play_purchase_token)!r})"
        )


@dataclass(frozen=True)
class FcmTokenRequest:
    installation_id: str
    fcm_token: str
    platform: str
    token_issued_at_utc: datetime
    idempotency: Idempotency

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "FcmTokenRequest":
        require_schema(value)
        return cls(
            installation_id=parse_string(value.get("installationId"), "installationId"),
            fcm_token=parse_string(value.get("fcmToken"), "fcmToken"),
            platform=parse_stable_enum(value.get("platform"), PLATFORMS),
            token_issued_at_utc=parse_utc_timestamp(value.get("tokenIssuedAtUtc")),
            idempotency=Idempotency.from_json(value.get("idempotency") or {}),
        )

    def __repr__(self) -> str:
        return (
            "FcmTokenRequest("
            f"installation_id={self.installation_id!r}, "
            f"fcm_token={redact(self.fcm_token)!r}, platform={self.platform!r})"
        )


@dataclass(frozen=True)
class PriceAlertContractRecord:
    alert_id: str
    source_kind: str
    price_basis: str
    condition: str
    status: str
    target: str
    alert_currency_code: str
    revision: int

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "PriceAlertContractRecord":
        require_schema(value)
        source = value.get("source") or {}
        revision = value.get("revision")
        if not isinstance(revision, int) or revision < 0:
            raise ContractError("malformed_request", "revision must be non-negative")
        return cls(
            alert_id=parse_string(value.get("alertId"), "alertId"),
            source_kind=parse_stable_enum(source.get("sourceKind"), SOURCE_KINDS),
            price_basis=parse_stable_enum(value.get("priceBasis"), PRICE_BASES),
            condition=parse_stable_enum(value.get("condition"), CONDITIONS),
            status=parse_stable_enum(value.get("status"), STATUSES),
            target=canonical_decimal(value.get("target")),
            alert_currency_code=parse_currency(value.get("alertCurrencyCode")),
            revision=revision,
        )


@dataclass(frozen=True)
class NotificationPayload:
    alert_id: str
    event_id: str | None
    source_kind: str | None
    created_at_utc: datetime

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "NotificationPayload":
        require_schema(value)
        source_kind = value.get("sourceKind")
        if source_kind is not None:
            source_kind = parse_stable_enum(source_kind, SOURCE_KINDS)
        forbidden_detail_fields = {"target", "triggeredPrice", "currentPrice", "metalName"}
        if forbidden_detail_fields.intersection(value):
            raise ContractError(
                "malformed_request",
                "Notification payload must contain routing identifiers only",
            )
        return cls(
            alert_id=parse_string(value.get("alertId"), "alertId"),
            event_id=value.get("eventId"),
            source_kind=source_kind,
            created_at_utc=parse_utc_timestamp(value.get("createdAtUtc")),
        )


def deterministic_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
