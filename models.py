from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional, Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DealerMetalQuote:
    buy: Optional[float] = None
    sell: Optional[float] = None

    def to_dict(self) -> Dict[str, Optional[float]]:
        return {
            "buy": round(self.buy, 2) if self.buy is not None else None,
            "sell": round(self.sell, 2) if self.sell is not None else None,
        }


@dataclass
class DealerSnapshot:
    dealer_key: str
    source_url: str
    status: str = "unavailable"
    last_success_at: Optional[str] = None
    metals: Dict[str, DealerMetalQuote] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dealerKey": self.dealer_key,
            "sourceUrl": self.source_url,
            "status": self.status,
            "lastSuccessAt": self.last_success_at,
            "metals": {k: v.to_dict() for k, v in self.metals.items()},
        }

    @staticmethod
    def empty(dealer_key: str, source_url: str) -> "DealerSnapshot":
        return DealerSnapshot(
            dealer_key=dealer_key,
            source_url=source_url,
            status="unavailable",
            last_success_at=None,
            metals={
                "Gold": DealerMetalQuote(),
                "Silver": DealerMetalQuote(),
                "Platinum": DealerMetalQuote(),
                "Palladium": DealerMetalQuote(),
            },
        )


@dataclass
class DealerPriceSnapshot:
    updated_at: str
    dealers: Dict[str, DealerSnapshot]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "updated_at": self.updated_at,
            "dealers": {k: v.to_dict() for k, v in self.dealers.items()},
        }

    @staticmethod
    def empty() -> "DealerPriceSnapshot":
        return DealerPriceSnapshot(
            updated_at=utc_now_iso(),
            dealers={
                "perthMint": DealerSnapshot.empty(
                    "perthMint",
                    "https://www.perthmint.com/invest/information-for-investors/metal-prices/",
                ),
                "abcBullion": DealerSnapshot.empty(
                    "abcBullion",
                    "https://www.abcbullion.com.au/",
                ),
                "bullionNow": DealerSnapshot.empty(
                    "bullionNow",
                    "https://bullionnow.com.au/",
                ),
                "pbx": DealerSnapshot.empty(
                    "pbx",
                    "https://www.perthbullion.com/",
                ),
            },
        )