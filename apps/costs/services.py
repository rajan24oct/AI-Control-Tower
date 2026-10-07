"""FinOps services (sections 15, 16, 23)."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Avg, Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.costs.models import (
    Budget,
    BudgetAlert,
    BudgetAlertKind,
    BudgetPeriod,
    DailyUsageRollup,
    ModelPricing,
)
from apps.executions.models import Execution, ExecutionStatusChoices

ZERO = Decimal("0")
SUCCESS = ExecutionStatusChoices.COMPLETED
FAILURE_STATUSES = [ExecutionStatusChoices.FAILED, ExecutionStatusChoices.TIMED_OUT]


@dataclass
class CostBreakdown:
    amount: Decimal = ZERO
    currency: str = "USD"
    input_cost: Decimal = ZERO
    output_cost: Decimal = ZERO
    #: Which rates were used -- a versioned price list, or the model registry.
    source: str = "none"
    pricing_version: str = ""


class PricingService:
    """Resolves which rates applied at a given moment."""

    @staticmethod
    def resolve(model, at=None) -> ModelPricing | None:
        """The ModelPricing in force for `model` at `at`.

        Picks the latest version whose window covers the moment, so a newer
        price list supersedes an older one without needing the older one closed.
        """
        if model is None:
            return None
        at = at or timezone.now()

        return (
            ModelPricing.objects.filter(
                model=model,
                version__organization_id=model.organization_id,
                version__status="active",
                version__effective_from__lte=at,
            )
            .filter(Q(version__effective_to__isnull=True) | Q(version__effective_to__gt=at))
            .select_related("version")
            .order_by("-version__effective_from")
            .first()
        )


class CostCalculationService:
    """The single place cost is computed.

    Rates come from the pricing registry, never from a literal in application
    logic (section 15). Falls back to the model record's own rates when no
    versioned price list covers the moment, so cost is still derivable before
    any pricing versions exist.
    """

    @classmethod
    def calculate(cls, model, input_tokens: int, output_tokens: int, at=None) -> CostBreakdown:
        if model is None or (not input_tokens and not output_tokens):
            return CostBreakdown()

        pricing = PricingService.resolve(model, at)
        if pricing is not None:
            input_rate, output_rate = pricing.input_cost_per_1k, pricing.output_cost_per_1k
            currency = pricing.currency
            source, version = "pricing_version", pricing.version.name
        else:
            input_rate, output_rate = model.input_cost_per_1k, model.output_cost_per_1k
            currency = model.currency
            source, version = "model_registry", ""

        input_cost = (Decimal(input_tokens) / Decimal(1000) * input_rate).quantize(
            Decimal("0.000001")
        )
        output_cost = (Decimal(output_tokens) / Decimal(1000) * output_rate).quantize(
            Decimal("0.000001")
        )

        return CostBreakdown(
            amount=input_cost + output_cost,
            currency=currency,
            input_cost=input_cost,
            output_cost=output_cost,
            source=source,
            pricing_version=version,
        )


def period_window(period: str, on: date | None = None) -> tuple[date, date]:
    """Calendar window containing `on`, as (start, end_exclusive)."""
    on = on or timezone.now().date()

    if period == BudgetPeriod.DAILY:
        return on, on + timedelta(days=1)
    if period == BudgetPeriod.WEEKLY:
        start = on - timedelta(days=on.weekday())
        return start, start + timedelta(days=7)
    if period == BudgetPeriod.QUARTERLY:
        first_month = 3 * ((on.month - 1) // 3) + 1
        start = date(on.year, first_month, 1)
        end_month = first_month + 3
        end = date(on.year + 1, 1, 1) if end_month > 12 else date(on.year, end_month, 1)
        return start, end
    if period == BudgetPeriod.ANNUAL:
        return date(on.year, 1, 1), date(on.year + 1, 1, 1)

    start = date(on.year, on.month, 1)
    last_day = calendar.monthrange(on.year, on.month)[1]
    return start, start + timedelta(days=last_day)


class BudgetHealth:
    OVER = "over"
    AT_RISK = "at_risk"  # forecast to pass the budget by period end
    WATCH = "watch"  # past the first alert threshold
    ON_TRACK = "on_track"

    LABELS = {OVER: "Over budget", AT_RISK: "Forecast to exceed", WATCH: "Watch", ON_TRACK: "On track"}
    STYLES = {OVER: "danger", AT_RISK: "warning", WATCH: "info", ON_TRACK: "success"}


class BudgetService:
    """Evaluates budgets and raises threshold alerts (section 16)."""

    #: A forecast from the first day or so of a period is mostly noise.
    MIN_FORECAST_FRACTION = Decimal("0.1")

    @classmethod
    def executions_for(cls, budget: Budget, start: date, end: date):
        from apps.costs.attribution import scope_executions

        queryset = Execution.objects.for_organization(budget.organization).filter(
            started_at__date__gte=start, started_at__date__lt=end
        )
        return scope_executions(queryset, budget)

    @classmethod
    def seats_for(cls, budget: Budget):
        from apps.costs.attribution import scope_seats
        from apps.costs.models import DevToolSeat

        seats = DevToolSeat.objects.for_organization(budget.organization).filter(
            subscription__status="active"
        )
        return scope_seats(seats, budget)

    @classmethod
    def status_for(cls, budget: Budget, on: date | None = None) -> dict:
        start, end = period_window(budget.period, on)
        today = on or timezone.now().date()

        runtime = ZERO
        if budget.counts_runtime:
            runtime = cls.executions_for(budget, start, end).aggregate(total=Sum("cost"))["total"] or ZERO
        devtools = ZERO
        if budget.counts_developer_tools:
            from apps.costs.devtools import DevToolCostService

            # Up to today: a licence is not spent before the day it covers.
            devtools = DevToolCostService.total(
                cls.seats_for(budget), start, min(end, today + timedelta(days=1))
            )
        spend = runtime + devtools
        amount = budget.amount or ZERO
        percentage = (spend / amount * 100) if amount else ZERO

        total_days = (end - start).days
        elapsed = min(max((today - start).days + 1, 1), total_days)
        forecast = (spend / Decimal(elapsed) * Decimal(total_days)).quantize(Decimal("0.01"))
        forecast_percentage = (forecast / amount * 100) if amount else ZERO
        forecast_ready = Decimal(elapsed) / Decimal(total_days) >= cls.MIN_FORECAST_FRACTION

        crossed = [t for t in budget.thresholds if percentage >= t]
        is_over = spend > amount and amount > 0
        if is_over:
            health = BudgetHealth.OVER
        elif forecast_ready and forecast_percentage >= 100:
            health = BudgetHealth.AT_RISK
        elif crossed:
            health = BudgetHealth.WATCH
        else:
            health = BudgetHealth.ON_TRACK

        return {
            "budget": budget,
            "period_start": start,
            "period_end": end,
            #: The last day in the period, for display: `period_end` is exclusive.
            "period_last": end - timedelta(days=1),
            "runtime_spend": runtime,
            "devtools_spend": devtools,
            "spend": spend,
            "amount": amount,
            "remaining": amount - spend,
            "percentage": percentage,
            "forecast": forecast,
            "forecast_percentage": forecast_percentage,
            "forecast_ready": forecast_ready,
            "elapsed_days": elapsed,
            "total_days": total_days,
            "highest_threshold": max(crossed) if crossed else None,
            "is_over": is_over,
            "health": health,
            "health_label": BudgetHealth.LABELS[health],
            "health_style": BudgetHealth.STYLES[health],
        }

    @classmethod
    def evaluate(cls, budget: Budget, on: date | None = None) -> list[BudgetAlert]:
        """Raise alerts for thresholds newly crossed this period.

        `get_or_create` on (budget, period, kind, threshold) makes this safe to
        run on a schedule: a budget sitting at 95% alerts once, not on every
        sweep. A forecast alert is raised at most once per period, and only
        while the budget is not already over -- by then it has nothing to add.
        """
        status = cls.status_for(budget, on)
        raised: list[BudgetAlert] = []

        if (
            budget.forecast_alerts
            and status["forecast_ready"]
            and not status["is_over"]
            and status["forecast_percentage"] >= 100
        ):
            alert, created = BudgetAlert.objects.get_or_create(
                budget=budget,
                period_start=status["period_start"],
                kind=BudgetAlertKind.FORECAST,
                threshold=100,
                defaults={
                    "organization": budget.organization,
                    "period_end": status["period_end"],
                    "spend": status["forecast"],
                    "budget_amount": status["amount"],
                    "percentage": status["forecast_percentage"],
                    "currency": budget.currency,
                },
            )
            if created:
                raised.append(alert)

        for threshold in budget.thresholds:
            if status["percentage"] < threshold:
                continue

            alert, created = BudgetAlert.objects.get_or_create(
                budget=budget,
                period_start=status["period_start"],
                kind=BudgetAlertKind.THRESHOLD,
                threshold=threshold,
                defaults={
                    "organization": budget.organization,
                    "period_end": status["period_end"],
                    "spend": status["spend"],
                    "budget_amount": status["amount"],
                    "percentage": status["percentage"],
                    "currency": budget.currency,
                },
            )
            if created:
                raised.append(alert)

        if raised:
            from apps.notifications import emitters

            # Every threshold crossing is recorded, but a budget that jumps
            # straight past three thresholds warrants one notification about
            # the worst of them, not three. A crossed threshold outranks a
            # forecast: what happened beats what might.
            emitters.budget_alert(
                max(raised, key=lambda alert: (not alert.is_forecast, alert.threshold))
            )

        return raised

    @classmethod
    def evaluate_all(cls, organization=None) -> list[BudgetAlert]:
        queryset = Budget.objects.filter(status="active")
        if organization is not None:
            queryset = queryset.for_organization(organization)

        raised: list[BudgetAlert] = []
        for budget in queryset.select_related("organization"):
            raised.extend(cls.evaluate(budget))
        return raised


class RollupService:
    """Builds the daily aggregates the trend charts read (section 24)."""

    @staticmethod
    def rebuild_day(organization, day: date) -> int:
        rows = (
            Execution.objects.for_organization(organization)
            .filter(started_at__date=day)
            .values("application", "agent", "provider", "model")
            .annotate(
                executions=Count("id"),
                successful=Count("id", filter=Q(status=SUCCESS)),
                failed=Count("id", filter=Q(status__in=FAILURE_STATUSES)),
                input_tokens=Sum("input_tokens"),
                output_tokens=Sum("output_tokens"),
                total_tokens=Sum("total_tokens"),
                cost=Sum("cost"),
            )
        )

        DailyUsageRollup.objects.filter(organization=organization, date=day).delete()
        created = [
            DailyUsageRollup(
                organization=organization,
                date=day,
                application_id=row["application"],
                agent_id=row["agent"],
                provider_id=row["provider"],
                model_id=row["model"],
                executions=row["executions"],
                successful_executions=row["successful"],
                failed_executions=row["failed"],
                input_tokens=row["input_tokens"] or 0,
                output_tokens=row["output_tokens"] or 0,
                total_tokens=row["total_tokens"] or 0,
                cost=row["cost"] or ZERO,
            )
            for row in rows
        ]
        DailyUsageRollup.objects.bulk_create(created)
        return len(created)

    @classmethod
    def rebuild(cls, organization, days: int = 30) -> int:
        today = timezone.now().date()
        return sum(
            cls.rebuild_day(organization, today - timedelta(days=offset)) for offset in range(days)
        )


class CostAnalyticsService:
    """Cost breakdowns for the FinOps dashboard (section 15)."""

    def __init__(self, organization, days: int = 30):
        self.organization = organization
        self.days = days
        self.now = timezone.now()
        self.today = self.now.date()
        self.window_start = self.today - timedelta(days=days - 1)

    @property
    def executions(self):
        return Execution.objects.for_organization(self.organization)

    @property
    def window(self):
        return self.executions.filter(started_at__date__gte=self.window_start)

    def summary(self) -> dict:
        month_start, month_end = period_window(BudgetPeriod.MONTHLY)
        week_start, _ = period_window(BudgetPeriod.WEEKLY)

        today_spend = self.executions.filter(started_at__date=self.today).aggregate(
            total=Sum("cost")
        )["total"] or ZERO
        week_spend = self.executions.filter(started_at__date__gte=week_start).aggregate(
            total=Sum("cost")
        )["total"] or ZERO
        month = self.executions.filter(
            started_at__date__gte=month_start, started_at__date__lt=month_end
        ).aggregate(total=Sum("cost"), runs=Count("id"))
        month_spend = month["total"] or ZERO

        totals = self.window.aggregate(
            total=Sum("cost"), runs=Count("id"), avg=Avg("cost"), tokens=Sum("total_tokens")
        )
        successful = self.window.filter(status=SUCCESS).aggregate(
            total=Sum("cost"), runs=Count("id")
        )

        return {
            "today": today_spend,
            "week": week_spend,
            "month": month_spend,
            "projected_month": self._project(month_spend, month_start, month_end),
            "window_total": totals["total"] or ZERO,
            "window_runs": totals["runs"] or 0,
            "window_tokens": totals["tokens"] or 0,
            "avg_per_request": totals["avg"] or ZERO,
            "avg_per_successful": (
                (successful["total"] or ZERO) / successful["runs"] if successful["runs"] else ZERO
            ),
            "derived_share": self._derived_share(),
        }

    def _project(self, month_spend: Decimal, start: date, end: date) -> Decimal:
        """Straight-line projection from the month so far."""
        elapsed = (self.today - start).days + 1
        total_days = (end - start).days
        if elapsed <= 0 or not month_spend:
            return ZERO
        return (month_spend / Decimal(elapsed) * Decimal(total_days)).quantize(Decimal("0.01"))

    def _derived_share(self) -> Decimal:
        """Share of spend the Control Tower priced itself, rather than the
        engine reporting it. A high number is worth knowing about.
        """
        counts = self.window.aggregate(
            total=Count("id"), derived=Count("id", filter=Q(cost_is_derived=True))
        )
        if not counts["total"]:
            return ZERO
        return Decimal(counts["derived"]) / Decimal(counts["total"]) * 100

    def by(self, field: str, label_field: str, limit: int = 10) -> list[dict]:
        rows = (
            self.window.filter(**{f"{field}__isnull": False})
            .values(label_field)
            .annotate(cost=Sum("cost"), runs=Count("id"), tokens=Sum("total_tokens"))
            .order_by("-cost")[:limit]
        )
        return [
            {
                "label": row[label_field],
                "cost": row["cost"] or ZERO,
                "runs": row["runs"],
                "tokens": row["tokens"] or 0,
            }
            for row in rows
        ]

    def by_team(self, limit: int = 10) -> list[dict]:
        rows = (
            self.window.filter(application__team__isnull=False)
            .values("application__team__name")
            .annotate(cost=Sum("cost"), runs=Count("id"), tokens=Sum("total_tokens"))
            .order_by("-cost")[:limit]
        )
        return [
            {
                "label": row["application__team__name"],
                "cost": row["cost"] or ZERO,
                "runs": row["runs"],
                "tokens": row["tokens"] or 0,
            }
            for row in rows
        ]

    def daily_series(self) -> dict:
        rows = (
            self.window.annotate(day=TruncDate("started_at"))
            .values("day")
            .annotate(cost=Sum("cost"), tokens=Sum("total_tokens"))
        )
        by_day = {row["day"]: row for row in rows}
        days = [self.window_start + timedelta(days=offset) for offset in range(self.days)]

        return {
            "labels": [day.strftime("%d %b") for day in days],
            "cost": [float(by_day.get(day, {}).get("cost") or 0) for day in days],
            "tokens": [int(by_day.get(day, {}).get("tokens") or 0) for day in days],
        }
