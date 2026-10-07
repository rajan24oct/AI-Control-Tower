"""Budget governance: how the estate's budgets stand, and how much spend they cover."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.core.constants import LifecycleStatus
from apps.costs.devtools import DevToolCostService
from apps.costs.models import Budget, BudgetPeriod, BudgetScope
from apps.costs.services import BudgetHealth, BudgetService, period_window
from apps.executions.models import Execution

ZERO = Decimal("0")


class GovernanceService:
    def __init__(self, organization, now=None):
        self.organization = organization
        self.now = now or timezone.now()
        self.today = self.now.date()

    def active_budgets(self):
        return (
            Budget.objects.for_organization(self.organization)
            .filter(status=LifecycleStatus.ACTIVE)
            .select_related("team", "user", "application", "agent", "provider", "model", "owner", "organization")
        )

    def budget_states(self) -> list[dict]:
        order = [BudgetHealth.OVER, BudgetHealth.AT_RISK, BudgetHealth.WATCH, BudgetHealth.ON_TRACK]
        states = [BudgetService.status_for(budget, self.today) for budget in self.active_budgets()]
        return sorted(states, key=lambda s: (order.index(s["health"]), -s["forecast_percentage"]))

    @staticmethod
    def health_counts(states) -> dict:
        counts = Counter(state["health"] for state in states)
        return {health: counts.get(health, 0) for health in BudgetHealth.LABELS}

    def coverage(self) -> dict:
        """Share of this month's runtime spend that a budget narrower than the
        organization's watches. Organization-wide budgets catch a total, but
        cannot say whose spend moved.
        """
        start, end = period_window(BudgetPeriod.MONTHLY, self.today)
        month = Execution.objects.for_organization(self.organization).filter(
            started_at__date__gte=start, started_at__date__lt=end
        )
        total = month.aggregate(total=Sum("cost"))["total"] or ZERO
        covered_ids: set = set()
        for budget in self.active_budgets().exclude(scope=BudgetScope.ORGANIZATION):
            if budget.counts_runtime:
                covered_ids.update(
                    BudgetService.executions_for(budget, start, end).values_list("pk", flat=True)
                )
        covered = month.filter(pk__in=covered_ids).aggregate(total=Sum("cost"))["total"] or ZERO
        return {
            "total": total,
            "covered": covered,
            "uncovered": total - covered,
            "percentage": (covered / total * 100) if total else ZERO,
        }

    @staticmethod
    def burn_series(budget: Budget, state: dict) -> dict:
        """Cumulative spend by day against the budget line, plus the forecast."""
        start, end = state["period_start"], state["period_end"]
        today = min(timezone.now().date(), end - timedelta(days=1))
        daily = {}
        if budget.counts_runtime:
            for row in (
                BudgetService.executions_for(budget, start, end)
                .annotate(day=TruncDate("started_at")).values("day").annotate(cost=Sum("cost"))
            ):
                daily[row["day"]] = daily.get(row["day"], ZERO) + (row["cost"] or ZERO)
        # Developer-tool spend is priced cumulatively from the period start, as
        # the status figure is: day-by-day windows would round each day's
        # prorated licence and drift from the total.
        devtools_to = {}
        if budget.counts_developer_tools:
            seats = BudgetService.seats_for(budget)
            day = start
            while day <= today:
                devtools_to[day] = DevToolCostService.total(seats, start, day + timedelta(days=1))
                day += timedelta(days=1)

        labels, actual, forecast = [], [], []
        running = ZERO
        days = (end - start).days
        rate = state["spend"] / state["elapsed_days"] if state["elapsed_days"] else ZERO
        for offset in range(days):
            day = start + timedelta(days=offset)
            labels.append(day.strftime("%d %b") if days <= 92 else day.strftime("%b %Y"))
            if day <= today:
                running += daily.get(day, ZERO)
                so_far = round(float(running + devtools_to.get(day, ZERO)), 2)
                actual.append(so_far)
                forecast.append(so_far if day == today else None)
            else:
                actual.append(None)
                forecast.append(round(float(state["spend"] + rate * (day - today).days), 2))
        return {
            "labels": labels,
            "actual": actual,
            "forecast": forecast,
            "budget": float(state["amount"]),
            "thresholds": [
                {"pct": t, "value": float(state["amount"] * t / 100)} for t in budget.thresholds if t <= 100
            ],
        }

    @staticmethod
    def contributors(budget: Budget, state: dict, limit: int = 8) -> dict:
        start, end = state["period_start"], state["period_end"]
        agents = []
        if budget.counts_runtime:
            agents = [
                {"label": row["agent__name"] or "Unattributed", "cost": row["cost"] or ZERO, "runs": row["runs"]}
                for row in BudgetService.executions_for(budget, start, end)
                .values("agent__name").annotate(cost=Sum("cost"), runs=Count("id")).order_by("-cost")[:limit]
            ]
        seats = []
        if budget.counts_developer_tools:
            today = timezone.now().date()
            costs = DevToolCostService.costs(BudgetService.seats_for(budget), start, min(end, today + timedelta(days=1)))
            seats = sorted(
                ({"label": c.seat.person_label, "login": c.seat.login, "cost": c.total} for c in costs),
                key=lambda row: row["cost"], reverse=True,
            )[:limit]
        return {"agents": agents, "seats": seats}
