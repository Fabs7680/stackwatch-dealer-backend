from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .contracts import ContractError


PROJECT_DIR = Path(__file__).resolve().parents[2]
UNIT_MANIFEST_PATH = PROJECT_DIR / "test" / "fixtures" / "price_alert_units_v1.json"


@dataclass(frozen=True)
class MetalUnit:
    unit_id: str
    grams_per_unit: Decimal
    aliases: tuple[str, ...] = ()


def load_unit_manifest(path: Path = UNIT_MANIFEST_PATH) -> dict[str, MetalUnit]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schemaVersion") != 1:
        raise ContractError("unsupported_schema", "Unsupported unit manifest schema")
    units: dict[str, MetalUnit] = {}
    aliases: set[str] = set()
    for item in raw.get("units", []):
        unit_id = str(item["id"]).strip()
        grams = Decimal(str(item["gramsPerUnit"]))
        if not unit_id or grams <= 0 or not grams.is_finite():
            raise ContractError("malformed_request", "Invalid unit manifest entry")
        if unit_id in units:
            raise ContractError("conflict", "Duplicate unit manifest ID")
        unit_aliases = tuple(str(value).strip().lower() for value in item.get("aliases", []))
        for alias in unit_aliases:
            if not alias or alias in units or alias in aliases:
                raise ContractError("conflict", "Duplicate unit alias")
            aliases.add(alias)
        units[unit_id] = MetalUnit(unit_id, grams, unit_aliases)
    return units


SUPPORTED_UNITS = load_unit_manifest()
GRAMS_PER_TROY_OUNCE = SUPPORTED_UNITS["oz"].grams_per_unit


def grams_per_unit(unit_id: str) -> Decimal:
    key = unit_id.strip().lower()
    if key in SUPPORTED_UNITS:
        return SUPPORTED_UNITS[key].grams_per_unit
    for unit in SUPPORTED_UNITS.values():
        if key in unit.aliases:
            return unit.grams_per_unit
    raise ContractError("malformed_request", "Unsupported metal unit")


def price_per_troy_ounce_to_unit(price_per_troy_ounce: Decimal, unit_id: str) -> Decimal:
    if not price_per_troy_ounce.is_finite() or price_per_troy_ounce <= 0:
        raise ContractError("invalid_decimal", "Price must be positive")
    return price_per_troy_ounce * grams_per_unit(unit_id) / GRAMS_PER_TROY_OUNCE
