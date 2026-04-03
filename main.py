from __future__ import annotations

import time

from collector import DealerCollector
from sources.abc_bullion_source import ABCBullionSource
from sources.bullion_now_source import BullionNowSource
from sources.perth_mint_source import PerthMintSource
from sources.pbx_source import PBXSource
from storage import OUTPUT_FILE


def build_collector() -> DealerCollector:
    return DealerCollector(
        sources=[
            PerthMintSource(),
            ABCBullionSource(),
            BullionNowSource(),
            PBXSource(),
        ]
    )


def run_once() -> None:
    collector = build_collector()
    snapshot = collector.collect_now()

    print("Dealer collector run complete.")
    print(f"Updated at: {snapshot.updated_at}")
    print(f"Output file: {OUTPUT_FILE}")

    for dealer_key, dealer in snapshot.dealers.items():
        print(f"\n[{dealer_key}] status={dealer.status}")
        for metal, quote in dealer.metals.items():
            print(
                f"  {metal}: buy={quote.buy if quote.buy is not None else 'N/A'} "
                f"sell={quote.sell if quote.sell is not None else 'N/A'}"
            )


def run_loop(interval_seconds: int = 300) -> None:
    print(f"Dealer collector loop started. Interval: {interval_seconds} seconds")
    print(f"Output file: {OUTPUT_FILE}")

    while True:
        try:
            run_once()
        except Exception as e:
            print(f"Collector loop error: {e}")

        print(f"\nSleeping for {interval_seconds} seconds...\n")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    run_once()