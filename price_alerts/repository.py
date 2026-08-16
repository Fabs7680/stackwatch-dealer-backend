from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .contracts import ContractError
from .models import (
    EntitlementState,
    Installation,
    NotificationPreferences,
    PriceAlertDefinition,
    PriceAlertObservation,
    PriceAlertTriggerEvent,
    STATUS_ARMED,
    STATUS_WAITING,
    utc_now,
)
from .providers import FxSnapshot, ProviderObservation, SpotProviderSnapshot


class PriceAlertRepository(Protocol):
    def create_installation(
        self,
        *,
        installation: Installation,
        encoded_secret_hash: str,
        preferences: NotificationPreferences,
    ) -> None:
        ...

    def credential_hash_for_installation(self, installation_id: str) -> str | None:
        ...

    def update_installation_settings(
        self,
        *,
        installation_id: str,
        locale: str | None,
        time_zone_id: str | None,
        preferences: NotificationPreferences,
    ) -> None:
        ...

    def upsert_entitlement(self, *, installation_id: str, entitlement: EntitlementState) -> None:
        ...

    def entitlement_for_installation(self, installation_id: str) -> EntitlementState | None:
        ...

    def notification_preferences_for_installation(
        self, installation_id: str
    ) -> NotificationPreferences | None:
        ...

    def upsert_fcm_token(
        self,
        *,
        installation_id: str,
        token_hash: str,
        token_ciphertext: bytes,
        key_version: str,
        platform: str,
        token_issued_at_utc: datetime,
    ) -> None:
        ...

    def revoke_fcm_token(self, *, installation_id: str, token_hash: str | None = None) -> None:
        ...

    def active_fcm_tokens_for_installation(self, *, installation_id: str) -> list[FcmTokenRegistration]:
        ...

    def upsert_alert(self, alert: PriceAlertDefinition) -> PriceAlertDefinition:
        ...

    def alert(self, *, installation_id: str, alert_id: str) -> PriceAlertDefinition | None:
        ...

    def update_alert(self, alert: PriceAlertDefinition) -> PriceAlertDefinition:
        ...

    def delete_alert(self, *, installation_id: str, alert_id: str, deleted_at_utc: datetime) -> None:
        ...

    def delete_all_alerts(self, *, installation_id: str, deleted_at_utc: datetime) -> None:
        ...

    def list_alerts(self, *, installation_id: str, limit: int) -> list[PriceAlertDefinition]:
        ...

    def list_trigger_events(self, *, installation_id: str, limit: int) -> list[PriceAlertTriggerEvent]:
        ...

    def count_resumable_alerts(self, *, installation_id: str, exclude_alert_id: str | None = None) -> int:
        ...

    def persist_spot_snapshot(self, snapshot: SpotProviderSnapshot) -> list[PriceAlertObservation]:
        ...

    def persist_alert_observation(self, observation: PriceAlertObservation) -> None:
        ...

    def persist_fx_snapshot(self, snapshot: FxSnapshot) -> None:
        ...

    def latest_fx_snapshot(self) -> FxSnapshot | None:
        ...

    def list_active_alerts(self, *, limit: int) -> list[PriceAlertDefinition]:
        ...

    def create_trigger_event_once(self, event: PriceAlertTriggerEvent) -> bool:
        ...

    def create_delivery_once(
        self,
        *,
        installation_id: str,
        event_id: str,
        payload_hash: str,
        delivery_state: str,
        fcm_token_hash: str | None = None,
    ) -> bool:
        ...

    def update_delivery_result(
        self,
        *,
        installation_id: str,
        event_id: str,
        payload_hash: str,
        delivery_state: str,
        fcm_token_hash: str | None,
        provider_message_id: str | None,
        delivered_at_utc: datetime | None,
    ) -> None:
        ...

    def count_provider_attempts(
        self,
        *,
        provider_id: str,
        cycle_start_utc: datetime,
        cycle_end_utc: datetime,
    ) -> int:
        ...

    def record_provider_attempt(
        self,
        *,
        provider_id: str,
        attempted_at_utc: datetime,
        result: str,
        reason: str,
    ) -> None:
        ...

    def acquire_worker_lock(self, *, lock_name: str, now_utc: datetime) -> bool:
        ...

    def release_worker_lock(self, *, lock_name: str) -> None:
        ...


@dataclass(frozen=True)
class DeliveryRecord:
    installation_id: str
    event_id: str
    payload_hash: str
    delivery_state: str
    created_at_utc: datetime
    fcm_token_hash: str | None = None
    provider_message_id: str | None = None
    delivered_at_utc: datetime | None = None


@dataclass(frozen=True)
class FcmTokenRegistration:
    token_hash: str
    token_ciphertext: bytes
    key_version: str
    platform: str


class InMemoryPriceAlertRepository:
    """Test-only repository. Production construction uses PostgreSQL."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.installations: dict[str, Installation] = {}
        self.credential_hashes: dict[str, str] = {}
        self.preferences: dict[str, NotificationPreferences] = {}
        self.entitlements: dict[str, EntitlementState] = {}
        self.fcm_tokens: dict[str, dict[str, object]] = {}
        self.alerts: dict[str, PriceAlertDefinition] = {}
        self.trigger_events: dict[str, PriceAlertTriggerEvent] = {}
        self.deliveries: dict[tuple[str, str, str], DeliveryRecord] = {}
        self.provider_attempts: list[dict[str, object]] = []
        self.locks: set[str] = set()
        self.spot_observations: dict[str, PriceAlertObservation] = {}
        self.fx_snapshot: FxSnapshot | None = None

    def create_installation(
        self,
        *,
        installation: Installation,
        encoded_secret_hash: str,
        preferences: NotificationPreferences,
    ) -> None:
        with self._lock:
            self.installations[installation.installation_id] = installation
            self.credential_hashes[installation.installation_id] = encoded_secret_hash
            self.preferences[installation.installation_id] = preferences

    def credential_hash_for_installation(self, installation_id: str) -> str | None:
        return self.credential_hashes.get(installation_id)

    def update_installation_settings(
        self,
        *,
        installation_id: str,
        locale: str | None,
        time_zone_id: str | None,
        preferences: NotificationPreferences,
    ) -> None:
        with self._lock:
            installation = self.installations.get(installation_id)
            if installation is None or installation.deleted_at_utc is not None:
                raise ContractError("unauthorised_installation", "Installation unavailable")
            self.installations[installation_id] = Installation(
                installation_id=installation.installation_id,
                platform=installation.platform,
                package_id=installation.package_id,
                app_version_name=installation.app_version_name,
                app_version_code=installation.app_version_code,
                locale=locale,
                time_zone_id=time_zone_id,
                created_at_utc=installation.created_at_utc,
                updated_at_utc=utc_now(),
                deleted_at_utc=installation.deleted_at_utc,
            )
            self.preferences[installation_id] = preferences

    def upsert_entitlement(self, *, installation_id: str, entitlement: EntitlementState) -> None:
        with self._lock:
            self._require_installation(installation_id)
            self.entitlements[installation_id] = entitlement

    def entitlement_for_installation(self, installation_id: str) -> EntitlementState | None:
        return self.entitlements.get(installation_id)

    def notification_preferences_for_installation(
        self, installation_id: str
    ) -> NotificationPreferences | None:
        return self.preferences.get(installation_id)

    def upsert_fcm_token(
        self,
        *,
        installation_id: str,
        token_hash: str,
        token_ciphertext: bytes,
        key_version: str,
        platform: str,
        token_issued_at_utc: datetime,
    ) -> None:
        with self._lock:
            self._require_installation(installation_id)
            self.fcm_tokens[token_hash] = {
                "installation_id": installation_id,
                "ciphertext": token_ciphertext,
                "key_version": key_version,
                "platform": platform,
                "token_issued_at_utc": token_issued_at_utc,
                "revoked_at_utc": None,
            }

    def revoke_fcm_token(self, *, installation_id: str, token_hash: str | None = None) -> None:
        with self._lock:
            for key, record in self.fcm_tokens.items():
                if record["installation_id"] != installation_id:
                    continue
                if token_hash is not None and key != token_hash:
                    continue
                record["revoked_at_utc"] = utc_now()

    def active_fcm_tokens_for_installation(self, *, installation_id: str) -> list[FcmTokenRegistration]:
        return [
            FcmTokenRegistration(
                token_hash=key,
                token_ciphertext=record["ciphertext"],
                key_version=record["key_version"],
                platform=record["platform"],
            )
            for key, record in self.fcm_tokens.items()
            if record["installation_id"] == installation_id and record["revoked_at_utc"] is None
        ]

    def upsert_alert(self, alert: PriceAlertDefinition) -> PriceAlertDefinition:
        with self._lock:
            self._require_installation(alert.installation_id)
            existing = self.alerts.get(alert.alert_id)
            revision = existing.revision + 1 if existing is not None else alert.revision
            saved = alert.copy_with(revision=revision)
            self.alerts[alert.alert_id] = saved
            return saved

    def alert(self, *, installation_id: str, alert_id: str) -> PriceAlertDefinition | None:
        alert = self.alerts.get(alert_id)
        if alert is None or alert.installation_id != installation_id:
            return None
        return alert

    def update_alert(self, alert: PriceAlertDefinition) -> PriceAlertDefinition:
        with self._lock:
            self._require_installation(alert.installation_id)
            saved = alert.copy_with(revision=alert.revision + 1)
            self.alerts[alert.alert_id] = saved
            return saved

    def delete_alert(self, *, installation_id: str, alert_id: str, deleted_at_utc: datetime) -> None:
        with self._lock:
            alert = self.alert(installation_id=installation_id, alert_id=alert_id)
            if alert is not None:
                del self.alerts[alert.alert_id]

    def delete_all_alerts(self, *, installation_id: str, deleted_at_utc: datetime) -> None:
        with self._lock:
            for alert_id in [
                alert.alert_id
                for alert in self.alerts.values()
                if alert.installation_id == installation_id
            ]:
                del self.alerts[alert_id]

    def list_alerts(self, *, installation_id: str, limit: int) -> list[PriceAlertDefinition]:
        return [
            alert
            for alert in sorted(self.alerts.values(), key=lambda item: item.updated_at_utc, reverse=True)
            if alert.installation_id == installation_id
        ][:limit]

    def list_trigger_events(self, *, installation_id: str, limit: int) -> list[PriceAlertTriggerEvent]:
        return [
            event
            for event in sorted(
                self.trigger_events.values(),
                key=lambda item: item.triggered_at_utc,
                reverse=True,
            )
            if event.installation_id == installation_id
        ][:limit]

    def count_resumable_alerts(self, *, installation_id: str, exclude_alert_id: str | None = None) -> int:
        return sum(
            1
            for alert in self.alerts.values()
            if alert.installation_id == installation_id
            and alert.alert_id != exclude_alert_id
            and alert.counts_toward_limit
        )

    def persist_spot_snapshot(self, snapshot: SpotProviderSnapshot) -> list[PriceAlertObservation]:
        observations: list[PriceAlertObservation] = []
        for provider_observation in snapshot.observations:
            source = _spot_source(provider_observation)
            observation = PriceAlertObservation(
                observation_id=provider_observation.observation_id,
                source=source,
                metal_id=provider_observation.metal_id,
                price=provider_observation.usd_per_troy_ounce,
                currency_code="USD",
                unit_id="oz",
                price_basis="perUnit",
                provider_timestamp_utc=provider_observation.provider_timestamp_utc,
                received_at_utc=provider_observation.received_at_utc,
                is_authoritative=True,
                is_cached=False,
                is_stale=False,
                source_available=True,
                fx_required=False,
                fx_is_stale=False,
                valid_until_utc=provider_observation.received_at_utc + timedelta(minutes=35),
                source_url=provider_observation.source_url,
            )
            self.spot_observations[observation.observation_id] = observation
            observations.append(observation)
        return observations

    def persist_alert_observation(self, observation: PriceAlertObservation) -> None:
        self.spot_observations[observation.observation_id] = observation

    def persist_fx_snapshot(self, snapshot: FxSnapshot) -> None:
        self.fx_snapshot = snapshot

    def latest_fx_snapshot(self) -> FxSnapshot | None:
        return self.fx_snapshot

    def list_active_alerts(self, *, limit: int) -> list[PriceAlertDefinition]:
        return [
            alert
            for alert in self.alerts.values()
            if alert.status in {STATUS_WAITING, STATUS_ARMED}
        ][:limit]

    def create_trigger_event_once(self, event: PriceAlertTriggerEvent) -> bool:
        with self._lock:
            if event.observation_id not in self.spot_observations:
                raise ContractError("referential_integrity", "Trigger observation must be persisted first")
            if event.event_id in self.trigger_events:
                return False
            duplicate = any(
                existing.alert_id == event.alert_id
                and existing.observation_id == event.observation_id
                for existing in self.trigger_events.values()
            )
            if duplicate:
                return False
            self.trigger_events[event.event_id] = event
            return True

    def create_delivery_once(
        self,
        *,
        installation_id: str,
        event_id: str,
        payload_hash: str,
        delivery_state: str,
        fcm_token_hash: str | None = None,
    ) -> bool:
        key = (installation_id, event_id, payload_hash, fcm_token_hash)
        with self._lock:
            if key in self.deliveries:
                return False
            self.deliveries[key] = DeliveryRecord(
                installation_id=installation_id,
                event_id=event_id,
                payload_hash=payload_hash,
                delivery_state=delivery_state,
                created_at_utc=utc_now(),
                fcm_token_hash=fcm_token_hash,
            )
            return True

    def update_delivery_result(
        self,
        *,
        installation_id: str,
        event_id: str,
        payload_hash: str,
        delivery_state: str,
        fcm_token_hash: str | None,
        provider_message_id: str | None,
        delivered_at_utc: datetime | None,
    ) -> None:
        key = (installation_id, event_id, payload_hash, fcm_token_hash)
        with self._lock:
            existing = self.deliveries.get(key)
            if existing is None:
                return
            self.deliveries[key] = DeliveryRecord(
                installation_id=existing.installation_id,
                event_id=existing.event_id,
                payload_hash=existing.payload_hash,
                delivery_state=delivery_state,
                created_at_utc=existing.created_at_utc,
                fcm_token_hash=existing.fcm_token_hash,
                provider_message_id=provider_message_id,
                delivered_at_utc=delivered_at_utc,
            )

    def count_provider_attempts(
        self,
        *,
        provider_id: str,
        cycle_start_utc: datetime,
        cycle_end_utc: datetime,
    ) -> int:
        return sum(
            1
            for attempt in self.provider_attempts
            if attempt["provider_id"] == provider_id
            and attempt["result"] == "started"
            and cycle_start_utc <= attempt["attempted_at_utc"] < cycle_end_utc
        )

    def record_provider_attempt(
        self,
        *,
        provider_id: str,
        attempted_at_utc: datetime,
        result: str,
        reason: str,
    ) -> None:
        self.provider_attempts.append(
            {
                "provider_id": provider_id,
                "attempted_at_utc": attempted_at_utc.astimezone(timezone.utc),
                "result": result,
                "reason": reason,
            }
        )

    def acquire_worker_lock(self, *, lock_name: str, now_utc: datetime) -> bool:
        with self._lock:
            if lock_name in self.locks:
                return False
            self.locks.add(lock_name)
            return True

    def release_worker_lock(self, *, lock_name: str) -> None:
        with self._lock:
            self.locks.discard(lock_name)

    def _require_installation(self, installation_id: str) -> None:
        installation = self.installations.get(installation_id)
        if installation is None or installation.deleted_at_utc is not None:
            raise ContractError("unauthorised_installation", "Installation unavailable")


def _spot_source(observation: ProviderObservation):
    from .models import PriceAlertSource

    return PriceAlertSource(
        source_kind="spot",
        provider_id="bullionova-spot",
        metal_id=observation.metal_id,
        quote_id=f"bullionova-spot:{observation.metal_id}:USD:oz",
        source_currency_code="USD",
        source_unit_id="oz",
        price_basis="perUnit",
        verified=True,
        source_url=observation.source_url,
    )


def payload_hash(payload: dict[str, object]) -> str:
    encoded = repr(sorted(payload.items())).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
