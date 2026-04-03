from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Iterable

from models import DealerMetalQuote, DealerPriceSnapshot, DealerSnapshot, utc_now_iso
from source_base import DealerSourceBase
from storage import load_snapshot, save_snapshot


class DealerCollector:
    def __init__(self, sources: Iterable[DealerSourceBase]) -> None:
        self.sources = list(sources)
        self.latest_snapshot = load_snapshot()
        self.source_timeout_seconds = 45

    def collect_now(self) -> DealerPriceSnapshot:
        previous = self.latest_snapshot
        dealers: dict[str, DealerSnapshot] = {}

        for source in self.sources:
            previous_dealer = previous.dealers.get(source.dealer_key)

            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(source.fetch)
                    fresh = future.result(timeout=self.source_timeout_seconds)

                validated = self._validate_snapshot(
                    fresh,
                    previous_dealer=previous_dealer,
                )
                dealers[source.dealer_key] = validated
            except FutureTimeoutError:
                if previous_dealer is not None:
                    stale = DealerSnapshot(
                        dealer_key=previous_dealer.dealer_key,
                        source_url=previous_dealer.source_url,
                        status=f"stale_after_timeout_{self.source_timeout_seconds}s",
                        last_success_at=previous_dealer.last_success_at,
                        metals=previous_dealer.metals,
                    )
                    dealers[source.dealer_key] = stale
                else:
                    dealers[source.dealer_key] = source.build_failed_snapshot(
                        f"timeout_after_{self.source_timeout_seconds}s"
                    )
            except Exception as e:
                if previous_dealer is not None:
                    stale = DealerSnapshot(
                        dealer_key=previous_dealer.dealer_key,
                        source_url=previous_dealer.source_url,
                        status="stale_after_failure",
                        last_success_at=previous_dealer.last_success_at,
                        metals=previous_dealer.metals,
                    )
                    dealers[source.dealer_key] = stale
                else:
                    dealers[source.dealer_key] = source.build_failed_snapshot(e)

        for dealer_key, old_snapshot in previous.dealers.items():
            if dealer_key not in dealers:
                dealers[dealer_key] = old_snapshot

        snapshot = DealerPriceSnapshot(
            updated_at=utc_now_iso(),
            dealers=dealers,
        )

        self.latest_snapshot = snapshot
        save_snapshot(snapshot)
        return snapshot

    def _validate_snapshot(
        self,
        snapshot: DealerSnapshot,
        previous_dealer: DealerSnapshot | None = None,
    ) -> DealerSnapshot:
        cleaned: dict[str, DealerMetalQuote] = {}

        for metal in ("Gold", "Silver", "Platinum", "Palladium"):
            raw = snapshot.metals.get(metal, DealerMetalQuote())

            cleaned[metal] = DealerMetalQuote(
                buy=self._normalize_price(raw.buy),
                sell=self._normalize_price(raw.sell),
            )

        has_any_value = any(
            quote.buy is not None or quote.sell is not None
            for quote in cleaned.values()
        )

        if not has_any_value:
            return DealerSnapshot(
                dealer_key=snapshot.dealer_key,
                source_url=snapshot.source_url,
                status="empty",
                last_success_at=None,
                metals=cleaned,
            )

        return DealerSnapshot(
            dealer_key=snapshot.dealer_key,
            source_url=snapshot.source_url,
            status="ok" if has_any_value else "empty",
            last_success_at=snapshot.last_success_at if has_any_value else snapshot.last_success_at,
            metals=cleaned,
        )

    def _normalize_price(self, value: float | None) -> float | None:
        if value is None:
            return None

        if value <= 0:
            return None

        return round(float(value), 2)