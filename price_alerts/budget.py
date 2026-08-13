from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


@dataclass(frozen=True)
class ProviderBudgetDecision:
    allowed: bool
    state: str
    used: int
    remaining: int
    warning: bool
    cycle_start_utc: datetime
    cycle_end_utc: datetime


class ProviderUsageRepository(Protocol):
    def count_provider_attempts(
        self,
        *,
        provider_id: str,
        cycle_start_utc: datetime,
        cycle_end_utc: datetime,
    ) -> int:
        ...

    def record_provider_attempt(
        self,
        *,
        provider_id: str,
        attempted_at_utc: datetime,
        result: str,
        reason: str,
    ) -> None:
        ...


class ProviderCallBudget:
    def __init__(
        self,
        *,
        repository: ProviderUsageRepository,
        provider_id: str,
        plan_limit: int = 5000,
        hard_limit: int = 4800,
        warning_threshold: int = 4500,
        anchor_day: int = 1,
    ) -> None:
        self._repository = repository
        self._provider_id = provider_id
        self._plan_limit = plan_limit
        self._hard_limit = min(hard_limit, plan_limit)
        self._warning_threshold = min(warning_threshold, self._hard_limit)
        self._anchor_day = max(1, min(28, anchor_day))

    def decision(self, now_utc: datetime) -> ProviderBudgetDecision:
        now = now_utc.astimezone(timezone.utc)
        start, end = billing_cycle(now, self._anchor_day)
        used = self._repository.count_provider_attempts(
            provider_id=self._provider_id,
            cycle_start_utc=start,
            cycle_end_utc=end,
        )
        remaining = max(0, self._hard_limit - used)
        allowed = used < self._hard_limit
        state = "ok"
        if not allowed:
            state = "budget-paused"
        elif used >= self._warning_threshold:
            state = "warning"
        return ProviderBudgetDecision(
            allowed=allowed,
            state=state,
            used=used,
            remaining=remaining,
            warning=used >= self._warning_threshold,
            cycle_start_utc=start,
            cycle_end_utc=end,
        )

    def record_attempt(self, *, now_utc: datetime, result: str, reason: str = "") -> None:
        self._repository.record_provider_attempt(
            provider_id=self._provider_id,
            attempted_at_utc=now_utc.astimezone(timezone.utc),
            result=result,
            reason=reason,
        )


def billing_cycle(now_utc: datetime, anchor_day: int) -> tuple[datetime, datetime]:
    now = now_utc.astimezone(timezone.utc)
    anchor = max(1, min(28, anchor_day))
    start = datetime(now.year, now.month, anchor, tzinfo=timezone.utc)
    if now < start:
        previous_month = now.month - 1
        year = now.year
        if previous_month == 0:
            previous_month = 12
            year -= 1
        start = datetime(year, previous_month, anchor, tzinfo=timezone.utc)
    next_month = start.month + 1
    next_year = start.year
    if next_month == 13:
        next_month = 1
        next_year += 1
    last_day = calendar.monthrange(next_year, next_month)[1]
    end_day = min(anchor, last_day)
    end = datetime(next_year, next_month, end_day, tzinfo=timezone.utc)
    return start, end
