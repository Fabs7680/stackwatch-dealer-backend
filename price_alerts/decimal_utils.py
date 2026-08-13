from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .contracts import ContractError


def parse_decimal(value: Any, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str) or value.strip() != value or value == "":
        raise ContractError("invalid_decimal", "Decimal must be a string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ContractError("invalid_decimal", "Invalid decimal") from exc
    if not parsed.is_finite():
        raise ContractError("invalid_decimal", "Decimal must be finite")
    if positive and parsed <= 0:
        raise ContractError("invalid_decimal", "Decimal must be positive")
    return parsed


def canonical_decimal_string(value: Decimal) -> str:
    if not value.is_finite():
        raise ContractError("invalid_decimal", "Decimal must be finite")
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f").split(".")[0]
    return format(normalized, "f")


def decimal_to_unscaled(value: Decimal) -> tuple[int, int]:
    if not value.is_finite():
        raise ContractError("invalid_decimal", "Decimal must be finite")
    sign, digits, exponent = value.as_tuple()
    scale = max(0, -exponent)
    unscaled = int("".join(str(item) for item in digits) or "0")
    if sign:
        unscaled *= -1
    return unscaled, scale
