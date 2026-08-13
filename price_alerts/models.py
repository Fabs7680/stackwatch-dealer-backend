from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal


SOURCE_SPOT = "spot"
SOURCE_DEALER_RETAIL = "dealerRetail"
SOURCE_DEALER_BUYBACK = "dealerBuyback"

BASIS_PER_UNIT = "perUnit"
BASIS_PRODUCT_TOTAL = "productTotal"

CONDITION_RISES = "risesToOrAbove"
CONDITION_FALLS = "fallsToOrBelow"

STATUS_DRAFT = "draft"
STATUS_WAITING = "activeWaitingForBaseline"
STATUS_ARMED = "activeArmed"
STATUS_TRIGGERED = "triggeredNeedsRearm"
STATUS_PAUSED = "paused"
STATUS_SOURCE_UNAVAILABLE = "sourceUnavailable"
STATUS_NOTIFICATION_REQUIRED = "notificationPermissionRequired"
STATUS_PRO_SUSPENDED = "proSuspended"
STATUS_RESTORE_REVIEW = "restoreReviewRequired"

COMPARISON_BELOW = "below"
COMPARISON_EQUAL = "equal"
COMPARISON_ABOVE = "above"

REASON_BASELINE = "baselineEstablished"
REASON_REMAINED_BELOW = "remainedBelow"
REASON_REMAINED_ABOVE = "remainedAbove"
REASON_REMAINED_EQUAL = "remainedEqual"
REASON_CROSSED_UP = "crossedUp"
REASON_CROSSED_DOWN = "crossedDown"
REASON_ALREADY_TRIGGERED = "alreadyTriggered"
REASON_NOT_EVALUABLE = "alertNotEvaluable"
REASON_DUPLICATE = "duplicateObservation"
REASON_SOURCE_MISMATCH = "sourceMismatch"
REASON_METAL_MISMATCH = "metalMismatch"
REASON_CURRENCY_MISMATCH = "currencyMismatch"
REASON_UNIT_MISMATCH = "unitMismatch"
REASON_BASIS_MISMATCH = "basisMismatch"
REASON_MISSING_PRICE = "missingPrice"
REASON_INVALID_PRICE = "invalidPrice"
REASON_CACHED = "cachedObservation"
REASON_STALE = "staleObservation"
REASON_STALE_FX = "staleFx"
REASON_SOURCE_UNAVAILABLE = "sourceUnavailable"
REASON_PRODUCT_UNAVAILABLE = "productUnavailable"
REASON_PRODUCT_OUT_OF_STOCK = "productOutOfStock"
REASON_NOTIFICATION_REQUIRED = "notificationPermissionRequired"
REASON_PRO_SUSPENDED = "proSuspended"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class QuietHoursPolicy:
    enabled: bool = False
    start_minute: int = 0
    end_minute: int = 0
    time_zone_id: str | None = None

    def contains_local_minute(self, minute_of_day: int) -> bool:
        if not self.enabled:
            return False
        if minute_of_day < 0 or minute_of_day > 1439:
            raise ValueError("minute_of_day must be 0..1439")
        if self.start_minute == self.end_minute:
            return True
        if self.start_minute < self.end_minute:
            return self.start_minute <= minute_of_day < self.end_minute
        return minute_of_day >= self.start_minute or minute_of_day < self.end_minute


@dataclass(frozen=True)
class PriceAlertSource:
    source_kind: str
    provider_id: str
    metal_id: str
    quote_id: str
    source_currency_code: str
    price_basis: str
    verified: bool
    source_unit_id: str | None = None
    dealer_id: str | None = None
    dealer_country_code: str | None = None
    product_id: str | None = None
    quote_side: str | None = None
    source_url: str | None = None

    @property
    def is_dealer(self) -> bool:
        return self.source_kind in {SOURCE_DEALER_RETAIL, SOURCE_DEALER_BUYBACK}

    @property
    def is_evaluation_verified(self) -> bool:
        if self.source_kind == SOURCE_SPOT:
            return self.verified
        return self.verified and self.dealer_id is not None

    def has_same_identity_as(self, other: "PriceAlertSource") -> bool:
        return (
            self.source_kind == other.source_kind
            and self.provider_id == other.provider_id
            and self.metal_id == other.metal_id
            and self.dealer_id == other.dealer_id
            and self.dealer_country_code == other.dealer_country_code
            and self.product_id == other.product_id
            and self.quote_id == other.quote_id
            and self.quote_side == other.quote_side
            and self.source_currency_code == other.source_currency_code
            and self.source_unit_id == other.source_unit_id
            and self.price_basis == other.price_basis
        )


@dataclass(frozen=True)
class PriceAlertDefinition:
    alert_id: str
    installation_id: str
    created_at_utc: datetime
    updated_at_utc: datetime
    status: str
    source: PriceAlertSource
    metal_id: str
    alert_currency_code: str
    condition: str
    target: Decimal
    price_basis: str
    rearm_required: bool
    restored_review_required: bool
    revision: int = 0
    unit_id: str | None = None
    baseline_observation_id: str | None = None
    last_observation_id: str | None = None
    last_comparison_state: str | None = None
    triggered_at_utc: datetime | None = None
    triggered_observation_id: str | None = None
    quiet_hours_policy: QuietHoursPolicy = QuietHoursPolicy()

    @property
    def counts_toward_limit(self) -> bool:
        return self.status in {
            STATUS_WAITING,
            STATUS_ARMED,
            STATUS_PAUSED,
            STATUS_SOURCE_UNAVAILABLE,
            STATUS_NOTIFICATION_REQUIRED,
            STATUS_PRO_SUSPENDED,
            STATUS_RESTORE_REVIEW,
        }

    def copy_with(self, **changes: object) -> "PriceAlertDefinition":
        return replace(self, **changes)


@dataclass(frozen=True)
class PriceAlertObservation:
    observation_id: str
    source: PriceAlertSource
    metal_id: str
    price: Decimal | None
    currency_code: str
    unit_id: str | None
    price_basis: str
    provider_timestamp_utc: datetime
    received_at_utc: datetime
    is_authoritative: bool
    is_cached: bool
    is_stale: bool
    source_available: bool
    fx_required: bool
    fx_is_stale: bool
    fx_timestamp_utc: datetime | None = None
    product_available: bool | None = None
    product_in_stock: bool | None = None
    native_price: Decimal | None = None
    native_currency_code: str | None = None
    source_url: str | None = None
    valid_until_utc: datetime | None = None


@dataclass(frozen=True)
class PriceAlertTriggerEvent:
    event_id: str
    alert_id: str
    installation_id: str
    triggered_at_utc: datetime
    observation_id: str
    source: PriceAlertSource
    metal_id: str
    condition: str
    target: Decimal
    triggered_price: Decimal
    alert_currency_code: str
    price_basis: str
    provider_timestamp_utc: datetime
    revision: int = 0
    unit_id: str | None = None
    dealer_id: str | None = None
    product_id: str | None = None
    native_price: Decimal | None = None
    native_currency_code: str | None = None


@dataclass(frozen=True)
class EvaluationResult:
    alert: PriceAlertDefinition
    reason: str
    trigger_event: PriceAlertTriggerEvent | None = None
    would_notify: bool = False

    @property
    def triggered(self) -> bool:
        return self.trigger_event is not None


@dataclass(frozen=True)
class Installation:
    installation_id: str
    platform: str
    package_id: str
    app_version_name: str
    app_version_code: int
    locale: str | None
    time_zone_id: str | None
    created_at_utc: datetime
    updated_at_utc: datetime
    deleted_at_utc: datetime | None = None


@dataclass(frozen=True)
class NotificationPreferences:
    notifications_enabled: bool = True
    show_price_details_in_notifications: bool = False
    quiet_hours_policy: QuietHoursPolicy = QuietHoursPolicy()
    revision: int = 0
    updated_at_utc: datetime | None = None


@dataclass(frozen=True)
class EntitlementState:
    status: str
    verified_until_utc: datetime | None
    expires_at_utc: datetime | None
    last_verified_at_utc: datetime

    @property
    def is_active(self) -> bool:
        return self.status in {"active", "grace"}
