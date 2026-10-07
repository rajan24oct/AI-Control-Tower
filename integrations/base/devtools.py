"""AI developer-tool billing contract (GitHub Copilot and the like).

Developer tools are paid for per seat, with metered "premium requests" on top.
That does not fit the per-token `UsageRecord` shape, so these adapters add two
richer calls -- who holds a seat, and how much each seat used per day -- and
still answer `get_usage_records` for anything that only wants totals.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal

from integrations.base.cost import BaseCostAdapter
from integrations.base.types import UsageRecord


@dataclass
class SeatRecord:
    """One assigned seat, as the vendor reports it."""

    login: str
    plan: str = "business"
    assigned_on: date | None = None
    display_name: str = ""
    email: str = ""
    assigning_team: str = ""
    last_activity_at: datetime | None = None
    last_activity_editor: str = ""
    pending_cancellation_on: date | None = None


@dataclass
class SeatUsage:
    """Premium requests one login made on one day."""

    login: str
    day: date
    premium_requests: Decimal = Decimal("0")
    by_model: dict[str, float] = field(default_factory=dict)


class BaseDeveloperToolAdapter(BaseCostAdapter):
    """Reads seats and per-seat usage from a developer-tool vendor."""

    platform: str = ""

    @abc.abstractmethod
    def list_seats(self, account: str) -> list[SeatRecord]:
        """Every seat currently assigned in the account."""

    @abc.abstractmethod
    def get_seat_usage(
        self, account: str, logins: list[str], start: date, end: date
    ) -> list[SeatUsage]:
        """Daily premium-request usage for `logins`, `start` inclusive to `end` exclusive."""

    def get_usage_records(self, start: datetime, end: datetime) -> list[UsageRecord]:
        account = self.config.option("account", "")
        seats = self.list_seats(account)
        usage = self.get_seat_usage(account, [seat.login for seat in seats], start.date(), end.date())
        total = sum((row.premium_requests for row in usage), Decimal("0"))
        return [
            UsageRecord(
                model_id="premium_requests",
                period_start=datetime.combine(start.date(), time.min, tzinfo=timezone.utc),
                period_end=datetime.combine(end.date(), time.min, tzinfo=timezone.utc),
                requests=int(total),
                currency=self.currency,
            )
        ]
