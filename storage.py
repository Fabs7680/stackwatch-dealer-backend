from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from models import DealerPriceSnapshot


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_FILE = OUTPUT_DIR / "dealer_prices.json"
TMP_FILE = OUTPUT_DIR / "dealer_prices.tmp.json"


def ensure_output_dir() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def load_snapshot() -> DealerPriceSnapshot:
    ensure_output_dir()

    if not OUTPUT_FILE.exists():
        return DealerPriceSnapshot.empty()

    try:
        raw = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return DealerPriceSnapshot.empty()

    return snapshot_from_dict(raw)


def save_snapshot(snapshot: DealerPriceSnapshot) -> None:
    ensure_output_dir()

    payload = json.dumps(
        snapshot.to_dict(),
        indent=2,
        ensure_ascii=False,
    )

    TMP_FILE.write_text(payload, encoding="utf-8")
    TMP_FILE.replace(OUTPUT_FILE)


def snapshot_from_dict(data: Dict[str, Any]) -> DealerPriceSnapshot:
    empty = DealerPriceSnapshot.empty()

    dealers: Dict[str, Any] = {}

    raw_dealers = data.get("dealers", {})
    if not isinstance(raw_dealers, dict):
        raw_dealers = {}

    for dealer_key, empty_snapshot in empty.dealers.items():
        raw_snapshot = raw_dealers.get(dealer_key, {})

        if not isinstance(raw_snapshot, dict):
            raw_snapshot = {}

        raw_metals = raw_snapshot.get("metals", {})
        if not isinstance(raw_metals, dict):
            raw_metals = {}

        metals = {}
        for metal_name, empty_quote in empty_snapshot.metals.items():
            raw_quote = raw_metals.get(metal_name, {})
            if not isinstance(raw_quote, dict):
                raw_quote = {}

            from models import DealerMetalQuote, DealerSnapshot

            metals[metal_name] = DealerMetalQuote(
                buy=_to_float_or_none(raw_quote.get("buy")),
                sell=_to_float_or_none(raw_quote.get("sell")),
            )

        from models import DealerSnapshot

        dealers[dealer_key] = DealerSnapshot(
            dealer_key=raw_snapshot.get("dealerKey", empty_snapshot.dealer_key),
            source_url=raw_snapshot.get("sourceUrl", empty_snapshot.source_url),
            status=raw_snapshot.get("status", empty_snapshot.status),
            last_success_at=raw_snapshot.get(
                "lastSuccessAt",
                empty_snapshot.last_success_at,
            ),
            metals=metals,
        )

    return DealerPriceSnapshot(
        updated_at=str(data.get("updated_at", empty.updated_at)),
        dealers=dealers,
    )


def _to_float_or_none(value: Any) -> float | None:
    if value is None:
        return None

    try:
        parsed = float(value)
    except Exception:
        return None

    if parsed <= 0:
        return None

    return round(parsed, 2)