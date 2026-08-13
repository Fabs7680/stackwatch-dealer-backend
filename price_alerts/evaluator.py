from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .models import (
    BASIS_PER_UNIT,
    COMPARISON_ABOVE,
    COMPARISON_BELOW,
    COMPARISON_EQUAL,
    CONDITION_FALLS,
    CONDITION_RISES,
    EvaluationResult,
    PriceAlertDefinition,
    PriceAlertObservation,
    PriceAlertTriggerEvent,
    REASON_ALREADY_TRIGGERED,
    REASON_BASELINE,
    REASON_BASIS_MISMATCH,
    REASON_CACHED,
    REASON_CROSSED_DOWN,
    REASON_CROSSED_UP,
    REASON_CURRENCY_MISMATCH,
    REASON_DUPLICATE,
    REASON_INVALID_PRICE,
    REASON_METAL_MISMATCH,
    REASON_MISSING_PRICE,
    REASON_NOT_EVALUABLE,
    REASON_NOTIFICATION_REQUIRED,
    REASON_PRODUCT_OUT_OF_STOCK,
    REASON_PRODUCT_UNAVAILABLE,
    REASON_PRO_SUSPENDED,
    REASON_REMAINED_ABOVE,
    REASON_REMAINED_BELOW,
    REASON_REMAINED_EQUAL,
    REASON_SOURCE_MISMATCH,
    REASON_SOURCE_UNAVAILABLE,
    REASON_STALE,
    REASON_STALE_FX,
    REASON_UNIT_MISMATCH,
    STATUS_ARMED,
    STATUS_DRAFT,
    STATUS_NOTIFICATION_REQUIRED,
    STATUS_PAUSED,
    STATUS_PRO_SUSPENDED,
    STATUS_RESTORE_REVIEW,
    STATUS_SOURCE_UNAVAILABLE,
    STATUS_TRIGGERED,
    STATUS_WAITING,
    as_utc,
)


class PriceAlertServerEvaluator:
    def evaluate(
        self,
        *,
        alert: PriceAlertDefinition,
        observation: PriceAlertObservation,
        evaluated_at_utc: datetime,
    ) -> EvaluationResult:
        evaluated_at = as_utc(evaluated_at_utc)

        if observation.observation_id == alert.last_observation_id:
            return EvaluationResult(alert=alert, reason=REASON_DUPLICATE)

        non_evaluable_reason = self._non_evaluable_reason(alert.status)
        if non_evaluable_reason is not None:
            return EvaluationResult(alert=alert, reason=non_evaluable_reason)

        mismatch_reason = self._mismatch_reason(alert, observation)
        if mismatch_reason is not None:
            return EvaluationResult(alert=alert, reason=mismatch_reason)

        eligibility_reason = self._observation_ineligibility_reason(
            observation,
            evaluated_at,
        )
        if eligibility_reason is not None:
            return EvaluationResult(alert=alert, reason=eligibility_reason)

        comparison = self._compare(observation.price, alert.target)
        if alert.status == STATUS_WAITING:
            return EvaluationResult(
                alert=alert.copy_with(
                    updated_at_utc=evaluated_at,
                    status=STATUS_ARMED,
                    baseline_observation_id=observation.observation_id,
                    last_observation_id=observation.observation_id,
                    last_comparison_state=comparison,
                    rearm_required=False,
                ),
                reason=REASON_BASELINE,
            )

        previous = alert.last_comparison_state
        if previous is None:
            return EvaluationResult(
                alert=alert.copy_with(
                    updated_at_utc=evaluated_at,
                    status=STATUS_ARMED,
                    baseline_observation_id=observation.observation_id,
                    last_observation_id=observation.observation_id,
                    last_comparison_state=comparison,
                    rearm_required=False,
                ),
                reason=REASON_BASELINE,
            )

        crossed_up = (
            alert.condition == CONDITION_RISES
            and previous == COMPARISON_BELOW
            and comparison != COMPARISON_BELOW
        )
        crossed_down = (
            alert.condition == CONDITION_FALLS
            and previous == COMPARISON_ABOVE
            and comparison != COMPARISON_ABOVE
        )
        if crossed_up or crossed_down:
            event = self._trigger_event(
                alert=alert,
                observation=observation,
                triggered_at_utc=evaluated_at,
            )
            return EvaluationResult(
                alert=alert.copy_with(
                    updated_at_utc=evaluated_at,
                    status=STATUS_TRIGGERED,
                    last_observation_id=observation.observation_id,
                    last_comparison_state=comparison,
                    triggered_at_utc=evaluated_at,
                    triggered_observation_id=observation.observation_id,
                    rearm_required=True,
                ),
                reason=REASON_CROSSED_UP if crossed_up else REASON_CROSSED_DOWN,
                trigger_event=event,
                would_notify=True,
            )

        return EvaluationResult(
            alert=alert.copy_with(
                updated_at_utc=evaluated_at,
                last_observation_id=observation.observation_id,
                last_comparison_state=comparison,
            ),
            reason=self._remained_reason(comparison),
        )

    def _non_evaluable_reason(self, status: str) -> str | None:
        if status in {STATUS_WAITING, STATUS_ARMED}:
            return None
        if status == STATUS_TRIGGERED:
            return REASON_ALREADY_TRIGGERED
        if status == STATUS_NOTIFICATION_REQUIRED:
            return REASON_NOTIFICATION_REQUIRED
        if status == STATUS_PRO_SUSPENDED:
            return REASON_PRO_SUSPENDED
        if status == STATUS_SOURCE_UNAVAILABLE:
            return REASON_SOURCE_UNAVAILABLE
        if status in {STATUS_DRAFT, STATUS_PAUSED, STATUS_RESTORE_REVIEW}:
            return REASON_NOT_EVALUABLE
        return REASON_NOT_EVALUABLE

    def _mismatch_reason(
        self,
        alert: PriceAlertDefinition,
        observation: PriceAlertObservation,
    ) -> str | None:
        if not alert.source.has_same_identity_as(observation.source):
            return REASON_SOURCE_MISMATCH
        if alert.metal_id != observation.metal_id:
            return REASON_METAL_MISMATCH
        if alert.alert_currency_code != observation.currency_code:
            return REASON_CURRENCY_MISMATCH
        if alert.price_basis != observation.price_basis:
            return REASON_BASIS_MISMATCH
        if alert.unit_id != observation.unit_id:
            return REASON_UNIT_MISMATCH
        return None

    def _observation_ineligibility_reason(
        self,
        observation: PriceAlertObservation,
        evaluated_at_utc: datetime,
    ) -> str | None:
        price = observation.price
        if price is None:
            return REASON_MISSING_PRICE
        if not price.is_finite() or price <= 0:
            return REASON_INVALID_PRICE
        if not observation.source_available:
            return REASON_SOURCE_UNAVAILABLE
        if not observation.is_authoritative or observation.is_cached:
            return REASON_CACHED
        if observation.is_stale:
            return REASON_STALE
        if observation.fx_required and observation.fx_is_stale:
            return REASON_STALE_FX
        if (
            observation.valid_until_utc is not None
            and not as_utc(observation.valid_until_utc) > evaluated_at_utc
        ):
            return REASON_STALE
        if not observation.source.is_evaluation_verified:
            return REASON_SOURCE_UNAVAILABLE
        if observation.source.is_dealer:
            if observation.product_available is not True:
                return REASON_PRODUCT_UNAVAILABLE
            if observation.product_in_stock is not True:
                return REASON_PRODUCT_OUT_OF_STOCK
        return None

    def _compare(self, value: Decimal | None, target: Decimal) -> str:
        if value is None:
            return COMPARISON_EQUAL
        if value < target:
            return COMPARISON_BELOW
        if value > target:
            return COMPARISON_ABOVE
        return COMPARISON_EQUAL

    def _remained_reason(self, comparison: str) -> str:
        if comparison == COMPARISON_BELOW:
            return REASON_REMAINED_BELOW
        if comparison == COMPARISON_ABOVE:
            return REASON_REMAINED_ABOVE
        return REASON_REMAINED_EQUAL

    def _trigger_event(
        self,
        *,
        alert: PriceAlertDefinition,
        observation: PriceAlertObservation,
        triggered_at_utc: datetime,
    ) -> PriceAlertTriggerEvent:
        return PriceAlertTriggerEvent(
            event_id=f"price_alert_event_{alert.alert_id}_{observation.observation_id}",
            alert_id=alert.alert_id,
            installation_id=alert.installation_id,
            triggered_at_utc=triggered_at_utc,
            observation_id=observation.observation_id,
            source=alert.source,
            metal_id=alert.metal_id,
            condition=alert.condition,
            target=alert.target,
            triggered_price=observation.price or Decimal("0"),
            alert_currency_code=alert.alert_currency_code,
            unit_id=alert.unit_id if alert.price_basis == BASIS_PER_UNIT else None,
            price_basis=alert.price_basis,
            provider_timestamp_utc=observation.provider_timestamp_utc,
            dealer_id=alert.source.dealer_id,
            product_id=alert.source.product_id,
            native_price=observation.native_price,
            native_currency_code=observation.native_currency_code,
        )


def rearm_alert(alert: PriceAlertDefinition, *, updated_at_utc: datetime) -> PriceAlertDefinition:
    return alert.copy_with(
        updated_at_utc=as_utc(updated_at_utc),
        status=STATUS_WAITING,
        baseline_observation_id=None,
        last_observation_id=None,
        last_comparison_state=None,
        triggered_at_utc=None,
        triggered_observation_id=None,
        rearm_required=False,
        restored_review_required=False,
    )


def pause_alert(alert: PriceAlertDefinition, *, updated_at_utc: datetime) -> PriceAlertDefinition:
    return alert.copy_with(updated_at_utc=as_utc(updated_at_utc), status=STATUS_PAUSED)


def resume_alert(alert: PriceAlertDefinition, *, updated_at_utc: datetime) -> PriceAlertDefinition:
    return alert.copy_with(
        updated_at_utc=as_utc(updated_at_utc),
        status=STATUS_WAITING,
        baseline_observation_id=None,
        last_observation_id=None,
        last_comparison_state=None,
        rearm_required=False,
        restored_review_required=False,
    )
