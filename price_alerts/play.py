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
    is_test_purchase: bool = False


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
        return _parse_subscriptions_v2_response(
            response,
            now_utc=now_utc,
            expected_package_id=package_id,
            expected_product_id=product_id,
            expected_base_plan_id=base_plan_id,
        )

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
    expected_package_id: str,
    expected_product_id: str,
    expected_base_plan_id: str,
) -> PlayVerificationResult:
    response_package = response.get("packageName")
    if isinstance(response_package, str) and response_package != expected_package_id:
        raise ContractError("entitlement_invalid", "Unexpected package in Play response")

    state = str(response.get("subscriptionState", "SUBSCRIPTION_STATE_UNSPECIFIED"))
    line_item = _matching_line_item(
        response.get("lineItems"),
        expected_product_id=expected_product_id,
    )
    if line_item is None:
        raise ContractError("entitlement_invalid", "Expected subscription line item missing")
    base_plan_id = _line_item_base_plan_id(line_item)
    if base_plan_id != expected_base_plan_id:
        raise ContractError("entitlement_invalid", "Unexpected base plan in Play response")
    expiry = _line_item_expiry(line_item)
    if expiry is None:
        return _unknown_result(state, now_utc=now_utc, expiry=None, response=response)

    # Flutter's BillingClient flow is the acknowledgement authority via
    # completePurchase; the backend verifies acknowledgement state only.
    acknowledgement_state = str(
        response.get("acknowledgementState", "ACKNOWLEDGEMENT_STATE_UNSPECIFIED")
    )
    if acknowledgement_state != "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED":
        return _unknown_result(state, now_utc=now_utc, expiry=expiry, response=response)

    if state == "SUBSCRIPTION_STATE_ACTIVE" and expiry > now_utc:
        return _active_result("active", state, now_utc, expiry, response)
    if state == "SUBSCRIPTION_STATE_IN_GRACE_PERIOD" and expiry > now_utc:
        return _active_result("grace", state, now_utc, expiry, response)
    if state == "SUBSCRIPTION_STATE_CANCELED" and expiry > now_utc:
        return _active_result("active", state, now_utc, expiry, response)
    if state in {
        "SUBSCRIPTION_STATE_CANCELED",
        "SUBSCRIPTION_STATE_EXPIRED",
        "SUBSCRIPTION_STATE_PAUSED",
        "SUBSCRIPTION_STATE_ON_HOLD",
        "SUBSCRIPTION_STATE_PENDING_PURCHASE_CANCELED",
        "SUBSCRIPTION_STATE_REVOKED",
    }:
        return PlayVerificationResult(
            status="inactive",
            verified_until_utc=now_utc,
            expires_at_utc=expiry,
            raw_state=state,
            is_test_purchase=_is_test_purchase(response),
        )
    return _unknown_result(state, now_utc=now_utc, expiry=expiry, response=response)


def _matching_line_item(
    value: object,
    *,
    expected_product_id: str,
) -> dict[str, object] | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if not isinstance(item, dict):
            continue
        candidate = dict(item)
        if candidate.get("productId") == expected_product_id:
            return candidate
    return None


def _line_item_base_plan_id(line_item: dict[str, object]) -> str | None:
    offer_details = line_item.get("offerDetails")
    if isinstance(offer_details, dict):
        base_plan_id = offer_details.get("basePlanId")
        if isinstance(base_plan_id, str) and base_plan_id.strip():
            return base_plan_id.strip()
    base_plan_id = line_item.get("basePlanId")
    if isinstance(base_plan_id, str) and base_plan_id.strip():
        return base_plan_id.strip()
    return None


def _line_item_expiry(line_item: dict[str, object]) -> datetime | None:
    raw_expiry = line_item.get("expiryTime")
    if not isinstance(raw_expiry, str) or not raw_expiry.strip():
        return None
    try:
        return datetime.fromisoformat(raw_expiry.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        return None


def _active_result(
    status: str,
    state: str,
    now_utc: datetime,
    expiry: datetime,
    response: dict[str, object],
) -> PlayVerificationResult:
    return PlayVerificationResult(
        status=status,
        verified_until_utc=now_utc + timedelta(hours=12),
        expires_at_utc=expiry,
        raw_state=state,
        is_test_purchase=_is_test_purchase(response),
    )


def _unknown_result(
    state: str,
    *,
    now_utc: datetime,
    expiry: datetime | None,
    response: dict[str, object],
) -> PlayVerificationResult:
    return PlayVerificationResult(
        status="unknown",
        verified_until_utc=now_utc,
        expires_at_utc=expiry,
        raw_state=state,
        is_test_purchase=_is_test_purchase(response),
    )


def _is_test_purchase(response: dict[str, object]) -> bool:
    return isinstance(response.get("testPurchase"), dict)
