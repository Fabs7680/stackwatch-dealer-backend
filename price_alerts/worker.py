from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from .budget import ProviderCallBudget
from .config import PriceAlertsServerConfig
from .contracts import ContractError
from .evaluator import PriceAlertServerEvaluator
from .fcm import FcmSender, FirebaseAdminFcmSender, price_alert_fcm_payload
from .models import (
    BASIS_PER_UNIT,
    EntitlementState,
    PriceAlertDefinition,
    PriceAlertObservation,
    PriceAlertSource,
    STATUS_PRO_SUSPENDED,
    as_utc,
)
from .postgres_repository import PostgresPriceAlertRepository
from .providers import FxSnapshotProvider, MetalsApiProvider, ProviderObservation
from .repository import PriceAlertRepository, payload_hash
from .security import ProtectedToken, token_protector_from_env
from .units import price_per_troy_ounce_to_unit


@dataclass(frozen=True)
class WorkerResult:
    status: str
    evaluated: int = 0
    triggered: int = 0
    deliveries_created: int = 0
    reason: str | None = None


class PriceAlertsWorker:
    lock_name = "bullionova-price-alerts-worker-v1"

    def __init__(
        self,
        *,
        config: PriceAlertsServerConfig,
        repository: PriceAlertRepository,
        metals_provider: MetalsApiProvider,
        fx_provider: FxSnapshotProvider,
        fcm_sender: FcmSender | None = None,
        token_protector=None,
        evaluator: PriceAlertServerEvaluator | None = None,
    ) -> None:
        self.config = config
        self.repository = repository
        self.metals_provider = metals_provider
        self.fx_provider = fx_provider
        self.fcm_sender = fcm_sender
        self.token_protector = token_protector
        self.evaluator = evaluator or PriceAlertServerEvaluator()

    def run_once(self, *, now_utc: datetime | None = None) -> WorkerResult:
        now = as_utc(now_utc or datetime.now(timezone.utc))
        if not self.config.enabled:
            return WorkerResult(status="disabled")
        if not self.repository.acquire_worker_lock(lock_name=self.lock_name, now_utc=now):
            return WorkerResult(status="locked")
        try:
            budget = ProviderCallBudget(
                repository=self.repository,
                provider_id=self.metals_provider.provider_id,
                plan_limit=self.config.metals_plan_limit,
                hard_limit=self.config.metals_application_hard_limit,
                warning_threshold=self.config.metals_warning_threshold,
                anchor_day=self.config.metals_billing_cycle_anchor_day,
            )
            decision = budget.decision(now)
            if not decision.allowed:
                return WorkerResult(status="budget-paused")
            budget.record_attempt(now_utc=now, result="started", reason="scheduled")
            try:
                spot_snapshot = self.metals_provider.fetch_latest(
                    now_utc=now,
                    freshness_window=self.config.spot_freshness_window,
                )
                budget.record_attempt(now_utc=now, result="success", reason="scheduled")
            except Exception as exc:
                budget.record_attempt(now_utc=now, result="failed", reason=exc.__class__.__name__)
                return WorkerResult(status="provider-failed", reason=exc.__class__.__name__)

            self.repository.persist_spot_snapshot(spot_snapshot)
            active_alerts = self.repository.list_active_alerts(
                limit=self.config.worker_batch_size,
            )
            required_currencies = {alert.alert_currency_code for alert in active_alerts}
            fx_snapshot = None
            if required_currencies - {"USD"}:
                fx_snapshot = self.repository.latest_fx_snapshot()
                if fx_snapshot is None:
                    fx_snapshot = self.fx_provider.fetch_latest(
                        required_currencies=required_currencies,
                        now_utc=now,
                        freshness_window=self.config.fx_freshness_window,
                    )
                    self.repository.persist_fx_snapshot(fx_snapshot)

            by_metal = {item.metal_id: item for item in spot_snapshot.observations}
            evaluated = 0
            triggered = 0
            deliveries = 0
            for alert in active_alerts:
                entitlement = self.repository.entitlement_for_installation(alert.installation_id)
                if entitlement is None or not _entitlement_current(entitlement, now):
                    self.repository.update_alert(
                        alert.copy_with(status=STATUS_PRO_SUSPENDED, updated_at_utc=now)
                    )
                    continue
                provider_observation = by_metal.get(alert.metal_id)
                if provider_observation is None:
                    continue
                observation = _observation_for_alert(
                    alert=alert,
                    provider_observation=provider_observation,
                    fx_snapshot=fx_snapshot,
                    now_utc=now,
                )
                result = self.evaluator.evaluate(
                    alert=alert,
                    observation=observation,
                    evaluated_at_utc=now,
                )
                evaluated += 1
                self.repository.update_alert(result.alert)
                if result.trigger_event is None:
                    continue
                if not self.repository.create_trigger_event_once(result.trigger_event):
                    continue
                triggered += 1
                delivery_state = "pending"
                if alert.quiet_hours_policy.contains_local_minute(now.hour * 60 + now.minute):
                    delivery_state = "suppressedQuietHours"
                event_payload = price_alert_fcm_payload(
                    alert_id=result.trigger_event.alert_id,
                    event_id=result.trigger_event.event_id,
                    source_kind=result.trigger_event.source.source_kind,
                    created_at_utc=result.trigger_event.triggered_at_utc,
                )
                event_payload_hash = payload_hash(event_payload)
                if delivery_state == "suppressedQuietHours":
                    created = self.repository.create_delivery_once(
                        installation_id=alert.installation_id,
                        event_id=result.trigger_event.event_id,
                        payload_hash=event_payload_hash,
                        delivery_state=delivery_state,
                    )
                    if created:
                        deliveries += 1
                    continue
                token_records = (
                    self.repository.active_fcm_tokens_for_installation(
                        installation_id=alert.installation_id,
                    )
                    if self.fcm_sender is not None and self.token_protector is not None
                    else []
                )
                if not token_records:
                    created = self.repository.create_delivery_once(
                        installation_id=alert.installation_id,
                        event_id=result.trigger_event.event_id,
                        payload_hash=event_payload_hash,
                        delivery_state=delivery_state,
                    )
                    if created:
                        deliveries += 1
                    continue
                for token_record in token_records:
                    created = self.repository.create_delivery_once(
                        installation_id=alert.installation_id,
                        event_id=result.trigger_event.event_id,
                        payload_hash=event_payload_hash,
                        delivery_state=delivery_state,
                        fcm_token_hash=token_record.token_hash,
                    )
                    if not created:
                        continue
                    deliveries += 1
                    try:
                        fcm_token = self.token_protector.reveal(
                            ProtectedToken(
                                keyed_hash=token_record.token_hash,
                                ciphertext=token_record.token_ciphertext,
                                key_version=token_record.key_version,
                            )
                        )
                        send_result = self.fcm_sender.send_price_alert_triggered(
                            fcm_token=fcm_token,
                            alert_id=result.trigger_event.alert_id,
                            event_id=result.trigger_event.event_id,
                            source_kind=result.trigger_event.source.source_kind,
                            created_at_utc=result.trigger_event.triggered_at_utc,
                        )
                        final_state = "delivered" if send_result.state == "delivered" else "failed"
                        self.repository.update_delivery_result(
                            installation_id=alert.installation_id,
                            event_id=result.trigger_event.event_id,
                            payload_hash=event_payload_hash,
                            delivery_state=final_state,
                            fcm_token_hash=token_record.token_hash,
                            provider_message_id=send_result.provider_message_id,
                            delivered_at_utc=now if final_state == "delivered" else None,
                        )
                        if send_result.invalid_registration:
                            self.repository.revoke_fcm_token(
                                installation_id=alert.installation_id,
                                token_hash=token_record.token_hash,
                            )
                    except Exception:
                        self.repository.update_delivery_result(
                            installation_id=alert.installation_id,
                            event_id=result.trigger_event.event_id,
                            payload_hash=event_payload_hash,
                            delivery_state="failed",
                            fcm_token_hash=token_record.token_hash,
                            provider_message_id=None,
                            delivered_at_utc=None,
                        )
            return WorkerResult(
                status="ok",
                evaluated=evaluated,
                triggered=triggered,
                deliveries_created=deliveries,
            )
        finally:
            self.repository.release_worker_lock(lock_name=self.lock_name)


def _entitlement_current(entitlement: EntitlementState, now_utc: datetime) -> bool:
    if not entitlement.is_active:
        return False
    if entitlement.verified_until_utc is not None and entitlement.verified_until_utc < now_utc:
        return False
    if entitlement.expires_at_utc is not None and entitlement.expires_at_utc < now_utc:
        return False
    return True


def _observation_for_alert(
    *,
    alert: PriceAlertDefinition,
    provider_observation: ProviderObservation,
    fx_snapshot,
    now_utc: datetime,
) -> PriceAlertObservation:
    if alert.price_basis != BASIS_PER_UNIT:
        raise ContractError("dealer_source_unavailable", "Product total spot alerts are unsupported")
    usd_per_oz = provider_observation.usd_per_troy_ounce
    if alert.alert_currency_code == "USD":
        currency_rate = Decimal("1")
        fx_required = False
        fx_timestamp = None
        fx_is_stale = False
    else:
        if fx_snapshot is None:
            raise ContractError("service_unavailable", "FX snapshot required")
        currency_rate = fx_snapshot.rate_from_usd(alert.alert_currency_code)
        fx_required = True
        fx_timestamp = fx_snapshot.provider_timestamp_utc
        fx_is_stale = False
    converted_per_oz = usd_per_oz * currency_rate
    converted_per_unit = price_per_troy_ounce_to_unit(converted_per_oz, alert.unit_id or "oz")
    source = PriceAlertSource(
        source_kind="spot",
        provider_id="bullionova-spot",
        metal_id=alert.metal_id,
        quote_id=f"bullionova-spot:{alert.metal_id}:{alert.alert_currency_code}:{alert.unit_id or 'oz'}",
        source_currency_code=alert.alert_currency_code,
        source_unit_id=alert.unit_id or "oz",
        price_basis=BASIS_PER_UNIT,
        verified=True,
        source_url=provider_observation.source_url,
    )
    return PriceAlertObservation(
        observation_id=(
            f"{source.quote_id}:{int(provider_observation.provider_timestamp_utc.timestamp())}:"
            f"{alert.alert_currency_code}:{alert.unit_id or 'oz'}"
        ),
        source=source,
        metal_id=alert.metal_id,
        price=converted_per_unit,
        currency_code=alert.alert_currency_code,
        unit_id=alert.unit_id or "oz",
        price_basis=BASIS_PER_UNIT,
        provider_timestamp_utc=provider_observation.provider_timestamp_utc,
        received_at_utc=now_utc,
        is_authoritative=True,
        is_cached=False,
        is_stale=False,
        source_available=True,
        fx_required=fx_required,
        fx_timestamp_utc=fx_timestamp,
        fx_is_stale=fx_is_stale,
        valid_until_utc=now_utc + self_freshness_placeholder(),
    )


def self_freshness_placeholder():
    from datetime import timedelta

    return timedelta(minutes=35)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bullionova Price Alerts worker")
    parser.add_argument("--once", action="store_true", help="Run one bounded worker cycle")
    args = parser.parse_args(argv)
    if not args.once:
        parser.error("Only --once is supported in this phase")
    config = PriceAlertsServerConfig.from_env()
    if not config.enabled or not config.worker_enabled:
        print({"status": "disabled"})
        return 0
    repository = PostgresPriceAlertRepository(database_url=config.database_url)
    worker = PriceAlertsWorker(
        config=config,
        repository=repository,
        metals_provider=MetalsApiProvider(access_key=config.metals_api_key),
        fx_provider=FxSnapshotProvider(),
        fcm_sender=(
            FirebaseAdminFcmSender(credentials_file=config.firebase_credentials_file or None)
            if config.fcm_enabled
            else None
        ),
        token_protector=token_protector_from_env() if config.fcm_enabled else None,
    )
    result = worker.run_once()
    print(result)
    return 0 if result.status in {"ok", "disabled", "locked", "budget-paused"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
