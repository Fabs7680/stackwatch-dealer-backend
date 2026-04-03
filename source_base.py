from __future__ import annotations

from abc import ABC, abstractmethod

from models import DealerMetalQuote, DealerSnapshot, utc_now_iso


class DealerSourceBase(ABC):
    @property
    @abstractmethod
    def dealer_key(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def source_url(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def fetch(self) -> DealerSnapshot:
        raise NotImplementedError

    def build_success_snapshot(
        self,
        metals: dict[str, DealerMetalQuote],
    ) -> DealerSnapshot:
        normalized = {
            "Gold": metals.get("Gold", DealerMetalQuote()),
            "Silver": metals.get("Silver", DealerMetalQuote()),
            "Platinum": metals.get("Platinum", DealerMetalQuote()),
            "Palladium": metals.get("Palladium", DealerMetalQuote()),
        }

        return DealerSnapshot(
            dealer_key=self.dealer_key,
            source_url=self.source_url,
            status="ok",
            last_success_at=utc_now_iso(),
            metals=normalized,
        )

    def build_failed_snapshot(self, error: object) -> DealerSnapshot:
        snapshot = DealerSnapshot.empty(
            dealer_key=self.dealer_key,
            source_url=self.source_url,
        )
        snapshot.status = f"failed: {error}"
        return snapshot