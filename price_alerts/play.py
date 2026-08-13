from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .contracts import ContractError
from .models import EntitlementState


@dataclass(frozen=True)
class PlayVerificationResult:
    status: str
    verified_until_utc: datetime | None
    expires_at_utc: datetime | None
    raw_state: str


class PlayVerifier(Protocol):
    def verify_subscription(
        self,
        *,
        package_id: str,
        product_id: str,
        base_plan_id: str,
        purchase_token: str,
        now_utc: datetime,
    ) -> PlayVerificationResult:
        ...


class UnconfiguredPlayVerifier:
    def verify_subscription(
        self,
        *,
        package_id: str,
        product_id: str,
        base_plan_id: str,
        purchase_token: str,
        now_utc: datetime,
    ) -> PlayVerificationResult:
        raise ContractError("service_unavailable", "Play verifier is not configured")


class StaticPlayVerifier:
    def __init__(self, result: PlayVerificationResult | None = None) -> None:
        now = datetime(2026, 8, 12, tzinfo=timezone.utc)
        self.result = result or PlayVerificationResult(
            status="active",
            verified_until_utc=now + timedelta(hours=12),
            expires_at_utc=now + timedelta(days=30),
            raw_state="test-active",
        )
        self.calls: list[dict[str, object]] = []

    def verify_subscription(
        self,
        *,
        package_id: str,
        product_id: str,
        base_plan_id: str,
        purchase_token: str,
        now_utc: datetime,
    ) -> PlayVerificationResult:
        self.calls.append(
            {
                "package_id": package_id,
                "product_id": product_id,
                "base_plan_id": base_plan_id,
                "token_length": len(purchase_token),
                "now_utc": now_utc,
            }
        )
        return self.result


class GooglePlayDeveloperApiVerifier:
    def __init__(self, *, credentials_file: str) -> None:
        if not credentials_file.strip():
            raise ContractError("service_unavailable", "Google Play credentials are not configured")
        self._credentials_file = credentials_file
        self._service = None

    def verify_subscription(
        self,
        *,
        package_id: str,
        product_id: str,
        base_plan_id: str,
        purchase_token: str,
        now_utc: datetime,
    ) -> PlayVerificationResult:
        if package_id != "com.northstack.stackwatch" or product_id != "stackwatch_pro":
            raise ContractError("entitlement_invalid", "Unexpected package or product")
        service = self._lazy_service()
        try:
            response = (
                service.purchases()
                .subscriptionsv2()
                .get(packageName=package_id, token=purchase_token)
                .execute()
            )
        except Exception as exc:
            raise ContractError("service_unavailable", "Play verification unavailable") from exc
        return _parse_subscriptions_v2_response(response, now_utc=now_utc)

    def _lazy_service(self):
        if self._service is not None:
            return self._service
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except Exception as exc:
            raise ContractError("service_unavailable", "Google API dependency unavailable") from exc
        credentials = service_account.Credentials.from_service_account_file(
            self._credentials_file,
            scopes=["https://www.googleapis.com/auth/androidpublisher"],
        )
        self._service = build("androidpublisher", "v3", credentials=credentials, cache_discovery=False)
        return self._service


def entitlement_from_play_result(
    result: PlayVerificationResult,
    *,
    now_utc: datetime,
) -> EntitlementState:
    return EntitlementState(
        status=result.status,
        verified_until_utc=result.verified_until_utc,
        expires_at_utc=result.expires_at_utc,
        last_verified_at_utc=now_utc.astimezone(timezone.utc),
    )


def _parse_subscriptions_v2_response(
    response: dict[str, object],
    *,
    now_utc: datetime,
) -> PlayVerificationResult:
    state = str(response.get("subscriptionState", "SUBSCRIPTION_STATE_UNSPECIFIED"))
    expiry = None
    line_items = response.get("lineItems")
    if isinstance(line_items, list) and line_items:
        raw_expiry = line_items[0].get("expiryTime") if isinstance(line_items[0], dict) else None
        if isinstance(raw_expiry, str):
            expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00")).astimezone(timezone.utc)
    active_states = {
        "SUBSCRIPTION_STATE_ACTIVE": "active",
        "SUBSCRIPTION_STATE_IN_GRACE_PERIOD": "grace",
    }
    inactive_states = {
        "SUBSCRIPTION_STATE_CANCELED",
        "SUBSCRIPTION_STATE_EXPIRED",
        "SUBSCRIPTION_STATE_PAUSED",
        "SUBSCRIPTION_STATE_ON_HOLD",
    }
    if state in active_states and (expiry is None or expiry > now_utc):
        return PlayVerificationResult(
            status=active_states[state],
            verified_until_utc=now_utc + timedelta(hours=12),
            expires_at_utc=expiry,
            raw_state=state,
        )
    if state in inactive_states:
        return PlayVerificationResult(
            status="inactive",
            verified_until_utc=now_utc,
            expires_at_utc=expiry,
            raw_state=state,
        )
    return PlayVerificationResult(
        status="unknown",
        verified_until_utc=now_utc,
        expires_at_utc=expiry,
        raw_state=state,
    )
