from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext
from typing import Any, Protocol
from urllib.parse import urlencode

from .contracts import ContractError, utc_iso
from .decimal_utils import canonical_decimal_string, parse_decimal

getcontext().prec = 48


SUPPORTED_SPOT_METALS = {
    "Gold": "XAU",
    "Silver": "XAG",
    "Platinum": "XPT",
    "Palladium": "XPD",
}


class JsonHttpClient(Protocol):
    def get_json(self, url: str, *, headers: dict[str, str], timeout_seconds: int) -> dict[str, Any]:
        ...


class UrllibJsonHttpClient:
    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        import urllib.request

        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            if response.status < 200 or response.status >= 300:
                raise ContractError("service_unavailable", "Provider HTTP error")
            decoded = json.loads(response.read().decode("utf-8"))
            if not isinstance(decoded, dict):
                raise ContractError("malformed_request", "Provider JSON must be an object")
            return decoded


@dataclass(frozen=True)
class ProviderObservation:
    observation_id: str
    provider_id: str
    metal_id: str
    symbol: str
    usd_per_troy_ounce: Decimal
    provider_timestamp_utc: datetime
    received_at_utc: datetime
    raw_base: str
    source_url: str

    def to_metadata(self) -> dict[str, str]:
        return {
            "observationId": self.observation_id,
            "providerId": self.provider_id,
            "metalId": self.metal_id,
            "symbol": self.symbol,
            "usdPerTroyOunce": canonical_decimal_string(self.usd_per_troy_ounce),
            "providerTimestampUtc": utc_iso(self.provider_timestamp_utc),
            "receivedAtUtc": utc_iso(self.received_at_utc),
            "base": self.raw_base,
        }


@dataclass(frozen=True)
class SpotProviderSnapshot:
    provider_id: str
    observations: tuple[ProviderObservation, ...]
    provider_timestamp_utc: datetime
    received_at_utc: datetime


class MetalsApiProvider:
    provider_id = "metals-api"
    latest_url = "https://metals-api.com/api/latest"

    def __init__(
        self,
        *,
        access_key: str,
        http_client: JsonHttpClient | None = None,
        timeout_seconds: int = 12,
    ) -> None:
        if not access_key.strip():
            raise ContractError("service_unavailable", "Metals-API key is not configured")
        self._access_key = access_key.strip()
        self._http = http_client or UrllibJsonHttpClient()
        self._timeout_seconds = timeout_seconds

    def fetch_latest(
        self,
        *,
        now_utc: datetime | None = None,
        freshness_window: timedelta,
    ) -> SpotProviderSnapshot:
        received_at = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
        symbols = ",".join(SUPPORTED_SPOT_METALS.values())
        url = f"{self.latest_url}?{urlencode({'access_key': self._access_key, 'base': 'USD', 'symbols': symbols})}"
        data = self._http.get_json(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Bullionova-PriceAlerts/1.0",
            },
            timeout_seconds=self._timeout_seconds,
        )
        if data.get("success") is not True:
            raise ContractError("service_unavailable", "Metals-API returned unsuccessful response")
        if str(data.get("base", "")).upper() != "USD":
            raise ContractError("malformed_request", "Metals-API base must be USD")
        timestamp = _provider_timestamp(data.get("timestamp"))
        if received_at - timestamp > freshness_window:
            raise ContractError("stale_observation", "Metals-API response is stale")
        rates = data.get("rates")
        if not isinstance(rates, dict):
            raise ContractError("malformed_request", "Metals-API rates missing")

        observations: list[ProviderObservation] = []
        for metal_id, symbol in SUPPORTED_SPOT_METALS.items():
            if symbol not in rates:
                raise ContractError("malformed_request", "Metals-API response is partial")
            provider_rate = parse_decimal(str(rates[symbol]), positive=True)
            usd_per_troy_ounce = Decimal("1") / provider_rate
            if not usd_per_troy_ounce.is_finite() or usd_per_troy_ounce <= 0:
                raise ContractError("invalid_decimal", "Invalid normalized metal price")
            observations.append(
                ProviderObservation(
                    observation_id=(
                        f"metals-api:{symbol}:{int(timestamp.timestamp())}:"
                        f"{canonical_decimal_string(usd_per_troy_ounce)}"
                    ),
                    provider_id=self.provider_id,
                    metal_id=metal_id,
                    symbol=symbol,
                    usd_per_troy_ounce=usd_per_troy_ounce,
                    provider_timestamp_utc=timestamp,
                    received_at_utc=received_at,
                    raw_base="USD",
                    source_url=self.latest_url,
                )
            )
        return SpotProviderSnapshot(
            provider_id=self.provider_id,
            observations=tuple(observations),
            provider_timestamp_utc=timestamp,
            received_at_utc=received_at,
        )


@dataclass(frozen=True)
class FxSnapshot:
    provider_id: str
    base_currency_code: str
    rates: dict[str, Decimal]
    provider_timestamp_utc: datetime
    received_at_utc: datetime

    def rate_from_usd(self, currency_code: str) -> Decimal:
        code = currency_code.strip().upper()
        if code == self.base_currency_code:
            return Decimal("1")
        value = self.rates.get(code)
        if value is None or not value.is_finite() or value <= 0:
            raise ContractError("service_unavailable", "Required FX rate missing")
        return value


class FxSnapshotProvider:
    provider_id = "open-er-api"
    latest_url = "https://open.er-api.com/v6/latest/USD"

    def __init__(
        self,
        *,
        http_client: JsonHttpClient | None = None,
        timeout_seconds: int = 12,
    ) -> None:
        self._http = http_client or UrllibJsonHttpClient()
        self._timeout_seconds = timeout_seconds

    def fetch_latest(
        self,
        *,
        required_currencies: set[str],
        now_utc: datetime | None = None,
        freshness_window: timedelta,
    ) -> FxSnapshot:
        received_at = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
        data = self._http.get_json(
            self.latest_url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Bullionova-PriceAlerts/1.0",
            },
            timeout_seconds=self._timeout_seconds,
        )
        if str(data.get("base_code", data.get("base", ""))).upper() != "USD":
            raise ContractError("malformed_request", "FX base must be USD")
        timestamp = _provider_timestamp(data.get("time_last_update_unix") or data.get("timestamp"))
        if received_at - timestamp > freshness_window:
            raise ContractError("stale_observation", "FX response is stale")
        raw_rates = data.get("rates")
        if not isinstance(raw_rates, dict):
            raise ContractError("malformed_request", "FX rates missing")
        rates: dict[str, Decimal] = {"USD": Decimal("1")}
        for code, value in raw_rates.items():
            normalized = str(code).strip().upper()
            if len(normalized) != 3:
                continue
            rates[normalized] = parse_decimal(str(value), positive=True)
        missing = {code for code in required_currencies if code not in rates}
        if missing:
            raise ContractError("service_unavailable", "Required FX rates missing")
        return FxSnapshot(
            provider_id=self.provider_id,
            base_currency_code="USD",
            rates=rates,
            provider_timestamp_utc=timestamp,
            received_at_utc=received_at,
        )


def _provider_timestamp(value: Any) -> datetime:
    if isinstance(value, int):
        return datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromtimestamp(int(value), timezone.utc)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ContractError("invalid_timestamp", "Invalid provider timestamp") from exc
            return parsed.astimezone(timezone.utc)
    raise ContractError("invalid_timestamp", "Provider timestamp missing")
