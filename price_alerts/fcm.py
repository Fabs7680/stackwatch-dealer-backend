from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .contracts import ContractError, SCHEMA_VERSION, utc_iso


PAYLOAD_TYPE_PRICE_ALERT_TRIGGERED = "priceAlertTriggered"


@dataclass(frozen=True)
class FcmSendResult:
    state: str
    provider_message_id: str | None = None
    invalid_registration: bool = False


class FcmSender(Protocol):
    def send_price_alert_triggered(
        self,
        *,
        fcm_token: str,
        alert_id: str,
        event_id: str,
        source_kind: str,
        created_at_utc: datetime,
    ) -> FcmSendResult:
        ...


def price_alert_fcm_payload(
    *,
    alert_id: str,
    event_id: str,
    source_kind: str,
    created_at_utc: datetime,
) -> dict[str, str]:
    return {
        "schemaVersion": str(SCHEMA_VERSION),
        "payloadType": PAYLOAD_TYPE_PRICE_ALERT_TRIGGERED,
        "alertId": alert_id,
        "eventId": event_id,
        "sourceKind": source_kind,
        "createdAtUtc": utc_iso(created_at_utc.astimezone(timezone.utc)),
    }


class FakeFcmSender:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def send_price_alert_triggered(
        self,
        *,
        fcm_token: str,
        alert_id: str,
        event_id: str,
        source_kind: str,
        created_at_utc: datetime,
    ) -> FcmSendResult:
        payload = price_alert_fcm_payload(
            alert_id=alert_id,
            event_id=event_id,
            source_kind=source_kind,
            created_at_utc=created_at_utc,
        )
        self.sent.append(payload)
        return FcmSendResult(state="delivered", provider_message_id="fake-message-id")


class FirebaseAdminFcmSender:
    def __init__(self, *, credentials_file: str | None = None) -> None:
        self._credentials_file = credentials_file
        self._initialized = False

    def send_price_alert_triggered(
        self,
        *,
        fcm_token: str,
        alert_id: str,
        event_id: str,
        source_kind: str,
        created_at_utc: datetime,
    ) -> FcmSendResult:
        messaging = self._lazy_messaging()
        payload = price_alert_fcm_payload(
            alert_id=alert_id,
            event_id=event_id,
            source_kind=source_kind,
            created_at_utc=created_at_utc,
        )
        try:
            message = messaging.Message(
                token=fcm_token,
                data=payload,
                notification=messaging.Notification(
                    title="Bullionova Price Alert",
                    body="A price alert needs your attention.",
                ),
            )
            provider_message_id = messaging.send(message)
            return FcmSendResult(state="delivered", provider_message_id=provider_message_id)
        except Exception as exc:
            name = exc.__class__.__name__.lower()
            invalid = "unregistered" in name or "invalid" in name
            return FcmSendResult(state="failed", invalid_registration=invalid)

    def _lazy_messaging(self):
        try:
            import firebase_admin
            from firebase_admin import credentials, messaging
        except Exception as exc:
            raise ContractError("service_unavailable", "Firebase Admin dependency unavailable") from exc
        if not self._initialized:
            if not firebase_admin._apps:
                if self._credentials_file:
                    cred = credentials.Certificate(self._credentials_file)
                    firebase_admin.initialize_app(cred)
                else:
                    firebase_admin.initialize_app()
            self._initialized = True
        return messaging
