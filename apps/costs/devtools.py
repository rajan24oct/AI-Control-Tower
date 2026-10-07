"""Developer-tool spend: syncing GitHub Copilot seats and usage, and pricing them.

Copilot is billed as a monthly seat licence plus premium requests beyond each
seat's monthly allowance. Both are attributed to the person holding the seat
and, through them, to a team and department -- which is what lets the
developer-tools view go from the whole estate to one team to one individual.
"""

from __future__ import annotations

import calendar
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from apps.costs.models import DevToolSeat, DevToolSubscription, DevToolUsage
from apps.organizations.models import Team, TeamMembership
from integrations.base.config import AdapterConfig
from integrations.base.exceptions import AdapterError
from integrations.registry import registry

logger = logging.getLogger(__name__)
ZERO = Decimal("0")
DAYS_PER_YEAR = Decimal("365")
#: Days re-read on every sync: vendors settle the last day or two late.
RESYNC_DAYS = 2


def month_start(day: date) -> date:
    return day.replace(day=1)


def next_month(day: date) -> date:
    last = calendar.monthrange(day.year, day.month)[1]
    return day.replace(day=1) + timedelta(days=last)


def add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year = day.year + month // 12
    return date(year, month % 12 + 1, 1)


# -- Pricing ----------------------------------------------------------------------


@dataclass
class SeatCost:
    seat: DevToolSeat
    license: Decimal = ZERO
    premium_requests: Decimal = ZERO
    overage_requests: Decimal = ZERO
    premium_cost: Decimal = ZERO
    #: Highest share of the monthly allowance used in any month of the window.
    peak_allowance_use: Decimal = ZERO
    by_model: dict = field(default_factory=dict)

    @property
    def total(self) -> Decimal:
        return self.license + self.premium_cost


class DevToolCostService:
    """Prices seats over any date window, billing-accurately.

    The licence is prorated by day. Premium requests are only charged beyond
    the seat's monthly allowance, so the cost of a window depends on what was
    already used earlier in the same month -- this replays each month from its
    first day and charges the window only for the overage that fell inside it.
    """

    @staticmethod
    def license_cost(seat: DevToolSeat, start: date, end: date) -> Decimal:
        first = max(start, seat.assigned_on)
        last = min(end, seat.removed_on) if seat.removed_on else end
        days = (last - first).days
        if days <= 0:
            return ZERO
        return (seat.monthly_price * 12 / DAYS_PER_YEAR * days).quantize(Decimal("0.01"))

    @classmethod
    def costs(cls, seats, start: date, end: date) -> list[SeatCost]:
        seats = list(seats.select_related("subscription", "user", "team"))
        if not seats:
            return []
        usage = defaultdict(list)
        for row in (
            DevToolUsage.objects.filter(seat__in=seats, date__gte=month_start(start), date__lt=end)
            .order_by("date")
            .values("seat_id", "date", "premium_requests", "by_model")
        ):
            usage[row["seat_id"]].append(row)

        results = []
        for seat in seats:
            cost = SeatCost(seat=seat, license=cls.license_cost(seat, start, end))
            allowance = Decimal(seat.included_requests)
            price = seat.subscription.premium_request_price
            used: dict[date, Decimal] = defaultdict(lambda: ZERO)

            for row in usage[seat.pk]:
                month = month_start(row["date"])
                before = used[month]
                after = before + row["premium_requests"]
                used[month] = after
                if not (start <= row["date"] < end):
                    continue
                overage = max(ZERO, after - allowance) - max(ZERO, before - allowance)
                cost.premium_requests += row["premium_requests"]
                cost.overage_requests += overage
                for model, quantity in (row["by_model"] or {}).items():
                    cost.by_model[model] = cost.by_model.get(model, 0) + quantity

            if allowance and used:
                cost.peak_allowance_use = max(used.values()) / allowance * 100
            cost.premium_cost = (cost.overage_requests * price).quantize(Decimal("0.01"))
            results.append(cost)
        return results

    @classmethod
    def total(cls, seats, start: date, end: date) -> Decimal:
        return sum((cost.total for cost in cls.costs(seats, start, end)), ZERO)


# -- Analytics --------------------------------------------------------------------


UNASSIGNED_TEAM = "Unassigned"


class DevToolAnalytics:
    """Team and individual breakdowns for the developer-tools view."""

    def __init__(self, organization, start: date, end: date, now=None):
        self.organization = organization
        self.start = start
        self.end = end
        self.now = now or timezone.now()

    @property
    def seats(self):
        return DevToolSeat.objects.for_organization(self.organization).filter(
            subscription__status="active"
        )

    def individuals(self) -> list[dict]:
        rows = []
        for cost in DevToolCostService.costs(self.seats, self.start, self.end):
            seat = cost.seat
            if cost.total <= 0 and not seat.is_active:
                continue
            idle = seat.idle_days(self.now)
            rows.append({
                "seat_id": str(seat.pk),
                "login": seat.login,
                "name": seat.person_label,
                "email": seat.user.email if seat.user_id else seat.email,
                "team": seat.team.name if seat.team_id else UNASSIGNED_TEAM,
                "team_id": str(seat.team_id) if seat.team_id else "",
                "department": (seat.team.department if seat.team_id else "") or "Unassigned",
                "plan": seat.get_plan_display(),
                "is_active": seat.is_active,
                "license": float(cost.license),
                "premium_cost": float(cost.premium_cost),
                "total": float(cost.total),
                "premium_requests": float(cost.premium_requests),
                "overage_requests": float(cost.overage_requests),
                "allowance": seat.included_requests,
                "allowance_use": float(round(cost.peak_allowance_use, 1)),
                "idle_days": idle,
                "last_activity": seat.last_activity_at.isoformat() if seat.last_activity_at else "",
                "editor": seat.last_activity_editor,
                "by_model": cost.by_model,
            })
        rows.sort(key=lambda row: row["total"], reverse=True)
        return rows

    @staticmethod
    def by_team(individuals: list[dict], idle_after_days: int = 30) -> list[dict]:
        teams: dict[str, dict] = {}
        for row in individuals:
            team = teams.setdefault(row["team"], {
                "team": row["team"], "team_id": row["team_id"], "department": row["department"],
                "seats": 0, "idle_seats": 0, "license": 0.0, "premium_cost": 0.0, "total": 0.0,
                "premium_requests": 0.0,
            })
            team["seats"] += 1
            if row["idle_days"] is None or row["idle_days"] >= idle_after_days:
                team["idle_seats"] += 1
            for key in ("license", "premium_cost", "total", "premium_requests"):
                team[key] += row[key]
        result = sorted(teams.values(), key=lambda team: team["total"], reverse=True)
        for team in result:
            team["per_seat"] = round(team["total"] / team["seats"], 2) if team["seats"] else 0
        return result

    def monthly_trend(self, months: int = 6) -> dict:
        """Licence and premium spend per month, per team, for the trend chart."""
        first = add_months(month_start(self.now.date()), -(months - 1))
        labels, series = [], defaultdict(lambda: [0.0] * months)
        for index in range(months):
            start = add_months(first, index)
            end = min(next_month(start), self.now.date() + timedelta(days=1))
            labels.append(start.strftime("%b %Y"))
            for cost in DevToolCostService.costs(self.seats, start, end):
                team = cost.seat.team.name if cost.seat.team_id else UNASSIGNED_TEAM
                series[team][index] += float(cost.total)
        return {"labels": labels, "series": [{"label": k, "data": v} for k, v in sorted(series.items())]}


# -- Sync -------------------------------------------------------------------------


@dataclass
class SyncResult:
    ok: bool = True
    message: str = ""
    seats_seen: int = 0
    seats_added: int = 0
    seats_removed: int = 0
    usage_rows: int = 0


class DevToolSyncService:
    """Pulls seats and usage from the vendor into the platform."""

    @staticmethod
    def adapter_for(subscription: DevToolSubscription):
        options = {**(subscription.options or {}), "account": subscription.account}
        config = AdapterConfig(
            key=subscription.adapter_key,
            name=subscription.name,
            endpoint=subscription.endpoint,
            credential_ref=subscription.credential_ref,
            options=options,
        )
        return registry.get_cost(subscription.adapter_key, config)

    @staticmethod
    def _usage_window(subscription: DevToolSubscription, today: date, days: int | None) -> tuple[date, date]:
        end = today + timedelta(days=1)
        if days:
            return today - timedelta(days=days - 1), end
        latest = (
            DevToolUsage.objects.filter(seat__subscription=subscription)
            .order_by("-date").values_list("date", flat=True).first()
        )
        if latest is None:
            # First sync: back to the start of last month, so this month's
            # overage is computed from a complete month of usage.
            return add_months(month_start(today), -1), end
        return min(latest - timedelta(days=RESYNC_DAYS), today), end

    @classmethod
    def _match_user(cls, organization, record, login_map: dict):
        User = get_user_model()
        members = User.objects.filter(organization_memberships__organization=organization)
        email = login_map.get(record.login) or record.email
        if email:
            return members.filter(email__iexact=email).first()
        return None

    @staticmethod
    def _match_team(organization, record, user):
        if record.assigning_team:
            team = Team.objects.filter(organization=organization, slug__iexact=record.assigning_team).first()
            if team:
                return team
        if user is not None:
            membership = (
                TeamMembership.objects.filter(team__organization=organization, user=user)
                .select_related("team").order_by("created_at").first()
            )
            if membership:
                return membership.team
        return None

    @classmethod
    def sync(cls, subscription: DevToolSubscription, days: int | None = None) -> SyncResult:
        result = SyncResult()
        organization = subscription.organization
        today = timezone.now().date()
        try:
            adapter = cls.adapter_for(subscription)
            records = adapter.list_seats(subscription.account)
            start, end = cls._usage_window(subscription, today, days)
            usage = adapter.get_seat_usage(subscription.account, [r.login for r in records], start, end)
        except AdapterError as exc:
            result.ok = False
            result.message = str(exc)[:500]
            subscription.last_sync_message = result.message
            subscription.save(update_fields=["last_sync_message", "updated_at"])
            logger.warning("Developer-tool sync failed for %s: %s", subscription, exc)
            return result

        login_map = (subscription.options or {}).get("login_map") or {}
        with transaction.atomic():
            seen = set()
            existing = {seat.login: seat for seat in subscription.seats.all()}
            for record in records:
                seen.add(record.login)
                seat = existing.get(record.login)
                creating = seat is None
                if creating:
                    seat = DevToolSeat(
                        organization=organization, subscription=subscription, login=record.login,
                        assigned_on=record.assigned_on or today,
                    )
                seat.plan = record.plan
                seat.display_name = record.display_name or seat.display_name
                seat.email = record.email or seat.email
                seat.assigning_team = record.assigning_team
                seat.last_activity_at = record.last_activity_at
                seat.last_activity_editor = record.last_activity_editor
                seat.pending_cancellation_on = record.pending_cancellation_on
                seat.is_active, seat.removed_on = True, None
                if record.assigned_on:
                    seat.assigned_on = record.assigned_on
                # A mapping somebody made by hand is kept; only fill gaps.
                if seat.user_id is None:
                    seat.user = cls._match_user(organization, record, login_map)
                if seat.team_id is None:
                    seat.team = cls._match_team(organization, record, seat.user)
                seat.save()
                existing[record.login] = seat
                result.seats_added += int(creating)

            for login, seat in existing.items():
                if login not in seen and seat.is_active:
                    seat.is_active, seat.removed_on = False, today
                    seat.save(update_fields=["is_active", "removed_on", "updated_at"])
                    result.seats_removed += 1

            for row in usage:
                seat = existing.get(row.login)
                if seat is None:
                    continue
                DevToolUsage.objects.update_or_create(
                    seat=seat, date=row.day,
                    defaults={
                        "organization": organization,
                        "premium_requests": row.premium_requests,
                        "by_model": row.by_model,
                    },
                )
                result.usage_rows += 1

            result.seats_seen = len(records)
            result.message = (
                f"{result.seats_seen} seats ({result.seats_added} new, {result.seats_removed} removed), "
                f"{result.usage_rows} days of usage from {start} to {end - timedelta(days=1)}."
            )
            subscription.last_synced_at = timezone.now()
            subscription.last_sync_message = result.message
            subscription.save(update_fields=["last_synced_at", "last_sync_message", "updated_at"])
        return result

    @classmethod
    def sync_all(cls, organization=None) -> list[SyncResult]:
        queryset = DevToolSubscription.objects.filter(status="active")
        if organization is not None:
            queryset = queryset.for_organization(organization)
        return [cls.sync(subscription) for subscription in queryset.select_related("organization")]
