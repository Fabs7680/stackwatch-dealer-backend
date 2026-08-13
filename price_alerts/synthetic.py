from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol

from .config import PriceAlertsServerConfig
from .contracts import ContractError
from .decimal_utils import canonical_decimal_string, parse_decimal
from .models import as_utc
from .providers import FxSnapshot, ProviderObservation, SpotProviderSnapshot
from .worker import PriceAlertsWorker, WorkerResult


SYNTHETIC_PROVIDER_ID = "synthetic-staging-spot"
SYNTHETIC_SOURCE_URL = "staging-cli://synthetic-spot"


class SyntheticAuditRepository(Protocol):
    def record_admin_event(
        self,
        *,
        installation_id: str,
        event_type: str,
        details: dict[str, object],
    ) -> None:
        ...


@dataclass(frozen=True)
class SyntheticSpotCrossingResult:
    observation_id: str
    worker_result: WorkerResult


class SyntheticSpotProvider:
    provider_id = SYNTHETIC_PROVIDER_ID

    def __init__(
        self,
        *,
        metal_id: str,
        usd_per_troy_ounce: Decimal,
        provider_timestamp_utc: datetime,
    ) -> None:
        self._metal_id = metal_id
        self._usd_per_troy_ounce = usd_per_troy_ounce
        self._provider_timestamp_utc = as_utc(provider_timestamp_utc)

    @property
    def observation_id(self) -> str:
        return (
            f"{self.provider_id}:{self._metal_id}:"
            f"{int(self._provider_timestamp_utc.timestamp())}:"
            f"{canonical_decimal_string(self._usd_per_troy_ounce)}"
        )

    def fetch_latest(
        self,
        *,
        now_utc: datetime | None = None,
        freshness_window: timedelta,
    ) -> SpotProviderSnapshot:
        received_at = as_utc(now_utc or datetime.now(timezone.utc))
        if received_at - self._provider_timestamp_utc > freshness_window:
            raise ContractError("stale_observation", "Synthetic observation is stale")
        observation = ProviderObservation(
            observation_id=self.observation_id,
            provider_id=self.provider_id,
            metal_id=self._metal_id,
            symbol=_symbol_for_metal(self._metal_id),
            usd_per_troy_ounce=self._usd_per_troy_ounce,
            provider_timestamp_utc=self._provider_timestamp_utc,
            received_at_utc=received_at,
            raw_base="USD",
            source_url=SYNTHETIC_SOURCE_URL,
        )
        return SpotProviderSnapshot(
            provider_id=self.provider_id,
            observations=(observation,),
            provider_timestamp_utc=self._provider_timestamp_utc,
            received_at_utc=received_at,
        )


class SyntheticFxProvider:
    provider_id = "synthetic-staging-fx"

    def fetch_latest(
        self,
        *,
        required_currencies: set[str],
        now_utc: datetime | None = None,
        freshness_window: timedelta,
    ) -> FxSnapshot:
        unsupported = {code for code in required_currencies if code != "USD"}
        if unsupported:
            raise ContractError(
                "service_unavailable",
                "Synthetic staging quotes support USD spot-alert testing only",
            )
        now = as_utc(now_utc or datetime.now(timezone.utc))
        return FxSnapshot(
            provider_id=self.provider_id,
            base_currency_code="USD",
            rates={"USD": Decimal("1")},
            provider_timestamp_utc=now,
            received_at_utc=now,
        )


def run_synthetic_spot_crossing(
    *,
    config: PriceAlertsServerConfig,
    repository,
    audit_repository: SyntheticAuditRepository,
    installation_id: str,
    metal_id: str,
    usd_per_troy_ounce: str,
    now_utc: datetime | None = None,
) -> SyntheticSpotCrossingResult:
    _require_synthetic_staging_mode(config)
    now = as_utc(now_utc or datetime.now(timezone.utc))
    price = parse_decimal(usd_per_troy_ounce, positive=True)
    provider = SyntheticSpotProvider(
        metal_id=metal_id,
        usd_per_troy_ounce=price,
        provider_timestamp_utc=now,
    )
    audit_repository.record_admin_event(
        installation_id=installation_id,
        event_type="staging_synthetic_spot_observation_inserted",
        details={
            "providerId": SYNTHETIC_PROVIDER_ID,
            "metalId": metal_id,
            "usdPerTroyOunce": canonical_decimal_string(price),
            "observationId": provider.observation_id,
        },
    )
    worker = PriceAlertsWorker(
        config=config,
        repository=repository,
        metals_provider=provider,
        fx_provider=SyntheticFxProvider(),
    )
    worker_result = worker.run_once(now_utc=now)
    audit_repository.record_admin_event(
        installation_id=installation_id,
        event_type="staging_synthetic_spot_crossing_evaluated",
        details={
            "providerId": SYNTHETIC_PROVIDER_ID,
            "observationId": provider.observation_id,
            "status": worker_result.status,
            "evaluated": worker_result.evaluated,
            "triggered": worker_result.triggered,
            "deliveriesCreated": worker_result.deliveries_created,
        },
    )
    return SyntheticSpotCrossingResult(
        observation_id=provider.observation_id,
        worker_result=worker_result,
    )


def _require_synthetic_staging_mode(config: PriceAlertsServerConfig) -> None:
    if config.environment != "staging":
        raise ContractError("entitlement_invalid", "Synthetic quotes require staging")
    if not config.enabled:
        raise ContractError("service_unavailable", "Price Alerts server is disabled")
    if not config.allow_synthetic_quotes:
        raise ContractError("entitlement_invalid", "Synthetic quotes are disabled")


def _symbol_for_metal(metal_id: str) -> str:
    return {
        "Gold": "XAU",
        "Silver": "XAG",
        "Platinum": "XPT",
        "Palladium": "XPD",
    }.get(metal_id, metal_id.upper())
