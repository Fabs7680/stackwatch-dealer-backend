from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .contracts import ContractError


SUPPORTED_SUBSCRIPTION_NOTIFICATION_TYPES = {
    1,   # recovered
    2,   # renewed
    3,   # canceled
    4,   # purchased
    5,   # on hold
    6,   # in grace period
    7,   # restarted
    8,   # price change confirmed
    9,   # deferred
    10,  # paused
    11,  # pause schedule changed
    12,  # revoked
    13,  # expired
    17,  # subscription items changed
    18,  # cancellation scheduled
    19,  # price change updated
    20,  # pending purchase canceled
    22,  # price step-up consent updated
}


@dataclass(frozen=True)
class RtdnSubscriptionEvent:
    message_id: str
    package_name: str
    product_id: str
    notification_type: int
    purchase_token: str = field(repr=False)
    event_time_millis: str | None = None

    @property
    def purchase_token_hash(self) -> str:
        return hashlib.sha256(self.purchase_token.encode("utf-8")).hexdigest()

    @property
    def idempotency_key(self) -> str:
        return (
            "rtdn:"
            f"{self.message_id}:"
            f"{self.notification_type}:"
            f"{self.purchase_token_hash}"
        )

    @property
    def requires_play_reverification(self) -> bool:
        return True


def parse_rtdn_pubsub_message(
    value: dict[str, Any],
    *,
    expected_package_id: str,
    expected_product_id: str,
) -> RtdnSubscriptionEvent:
    message = value.get("message") if isinstance(value, dict) else None
    if not isinstance(message, dict):
        raise ContractError("malformed_request", "RTDN Pub/Sub message is missing")
    message_id = _required_string(
        message.get("messageId") or message.get("message_id"),
        "messageId",
    )
    encoded_data = _required_string(message.get("data"), "data")
    try:
        decoded_bytes = base64.b64decode(encoded_data, validate=True)
        decoded = json.loads(decoded_bytes.decode("utf-8"))
    except Exception as exc:
        raise ContractError("malformed_request", "RTDN payload is malformed") from exc
    if not isinstance(decoded, dict):
        raise ContractError("malformed_request", "RTDN payload must be an object")
    package_name = _required_string(decoded.get("packageName"), "packageName")
    if package_name != expected_package_id:
        raise ContractError("entitlement_invalid", "Unexpected RTDN package")
    subscription = decoded.get("subscriptionNotification")
    if not isinstance(subscription, dict):
        raise ContractError("malformed_request", "RTDN subscription notification is missing")
    product_id = _required_string(subscription.get("subscriptionId"), "subscriptionId")
    if product_id != expected_product_id:
        raise ContractError("entitlement_invalid", "Unexpected RTDN subscription")
    notification_type = _required_int(
        subscription.get("notificationType"),
        "notificationType",
    )
    if notification_type not in SUPPORTED_SUBSCRIPTION_NOTIFICATION_TYPES:
        raise ContractError("malformed_request", "Unsupported RTDN notification type")
    purchase_token = _required_string(subscription.get("purchaseToken"), "purchaseToken")
    event_time_millis = decoded.get("eventTimeMillis")
    return RtdnSubscriptionEvent(
        message_id=message_id,
        package_name=package_name,
        product_id=product_id,
        notification_type=notification_type,
        purchase_token=purchase_token,
        event_time_millis=event_time_millis if isinstance(event_time_millis, str) else None,
    )


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError("malformed_request", f"{name} is required")
    return value.strip()


def _required_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError("malformed_request", f"{name} is required")
    return value
