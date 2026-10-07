"""Cost alert rules: guardrails on how spend behaves right now.

Budgets watch a period's total. Rules watch a rolling window -- the last hour,
day or week -- for a level, a spike against the recent baseline, an expensive
average run, or money spent on failures. Evaluated every 15 minutes with the
budgets, and on demand from the governance page.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.costs.attribution import scope_executions
from apps.costs.models import AlertMetric, CostAlertEvent, CostAlertRule
from apps.executions.models import Execution, ExecutionStatusChoices

logger = logging.getLogger(__name__)
ZERO = Decimal("0")
FAILURE_STATUSES = [ExecutionStatusChoices.FAILED, ExecutionStatusChoices.TIMED_OUT]
#: A spike is measured against the average of this many preceding windows.
BASELINE_WINDOWS = 4
#: How each metric reads at the start of an alert sentence.
METRIC_NOUNS = {
    AlertMetric.SPEND: "Spend",
    AlertMetric.COST_PER_RUN: "Average cost per run",
    AlertMetric.FAILED_SPEND: "Spend on failed runs",
    AlertMetric.TOKENS: "Token use",
}


@dataclass
class Measurement:
    value: Decimal | None
    spend: Decimal
    runs: int
    tokens: int
    failed_spend: Decimal
    baseline: Decimal | None = None


def format_value(rule: CostAlertRule, value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    if rule.is_percentage:
        return f"{value:.0f}%"
    if rule.metric == AlertMetric.TOKENS:
        return f"{int(value):,} tokens"
    places = 4 if rule.metric == AlertMetric.COST_PER_RUN else 2
    return f"{rule.currency} {value:,.{places}f}"


class CostAlertService:
    @staticmethod
    def _window(rule: CostAlertRule, end):
        return end - timedelta(hours=rule.window_hours), end

    @staticmethod
    def _executions(rule: CostAlertRule, start, end):
        queryset = Execution.objects.for_organization(rule.organization).filter(
            started_at__gte=start, started_at__lt=end
        )
        return scope_executions(queryset, rule)

    @classmethod
    def measure(cls, rule: CostAlertRule, now=None) -> Measurement:
        now = now or timezone.now()
        start, end = cls._window(rule, now)
        totals = cls._executions(rule, start, end).aggregate(
            spend=Sum("cost"),
            runs=Count("id"),
            tokens=Sum("total_tokens"),
            failed=Sum("cost", filter=Q(status__in=FAILURE_STATUSES)),
        )
        spend = totals["spend"] or ZERO
        runs = totals["runs"] or 0
        measurement = Measurement(
            value=None, spend=spend, runs=runs, tokens=totals["tokens"] or 0,
            failed_spend=totals["failed"] or ZERO,
        )

        if rule.metric == AlertMetric.SPEND:
            measurement.value = spend
        elif rule.metric == AlertMetric.COST_PER_RUN:
            measurement.value = (spend / runs) if runs else None
        elif rule.metric == AlertMetric.FAILED_SPEND:
            measurement.value = measurement.failed_spend
        elif rule.metric == AlertMetric.TOKENS:
            measurement.value = Decimal(measurement.tokens)
        elif rule.metric == AlertMetric.SPEND_INCREASE:
            width = timedelta(hours=rule.window_hours)
            previous = cls._executions(rule, start - width * BASELINE_WINDOWS, start).aggregate(
                spend=Sum("cost")
            )["spend"] or ZERO
            baseline = previous / BASELINE_WINDOWS
            measurement.baseline = baseline
            # With no baseline a percentage means nothing; a level rule is the
            # right tool for "any spend at all".
            measurement.value = ((spend - baseline) / baseline * 100) if baseline > 0 else None
        return measurement

    @classmethod
    def top_contributors(cls, rule: CostAlertRule, now=None, limit: int = 3) -> list[dict]:
        start, end = cls._window(rule, now or timezone.now())
        rows = (
            cls._executions(rule, start, end)
            .filter(agent__isnull=False)
            .values("agent__name")
            .annotate(cost=Sum("cost"), runs=Count("id"))
            .order_by("-cost")[:limit]
        )
        return [
            {"agent": row["agent__name"], "cost": float(row["cost"] or 0), "runs": row["runs"]}
            for row in rows
        ]

    @classmethod
    def evaluate(cls, rule: CostAlertRule, now=None) -> CostAlertEvent | None:
        now = now or timezone.now()
        measurement = cls.measure(rule, now)
        rule.last_evaluated_at = now
        rule.last_value = measurement.value
        fields = ["last_evaluated_at", "last_value", "updated_at"]

        fires = (
            measurement.value is not None
            and measurement.value >= rule.threshold
            and measurement.spend >= rule.minimum_spend
            and not rule.in_cooldown(now)
        )
        if not fires:
            rule.save(update_fields=fields)
            return None

        start, end = cls._window(rule, now)
        window = rule.get_window_hours_display().lower()
        if rule.metric == AlertMetric.SPEND_INCREASE:
            summary = (
                f"Spend over the {window} for {rule.scope_label} is up {format_value(rule, measurement.value)} "
                f"on the usual ({rule.currency} {measurement.spend:,.2f} against a baseline of "
                f"{rule.currency} {measurement.baseline:,.2f}); the rule alerts at "
                f"{format_value(rule, rule.threshold)}."
            )
        else:
            summary = (
                f"{METRIC_NOUNS[rule.metric]} over the {window} for {rule.scope_label} is "
                f"{format_value(rule, measurement.value)}, at or above the threshold of "
                f"{format_value(rule, rule.threshold)}."
            )
        with transaction.atomic():
            event = CostAlertEvent.objects.create(
                organization=rule.organization,
                rule=rule,
                severity=rule.severity,
                value=measurement.value,
                threshold=rule.threshold,
                window_start=start,
                window_end=end,
                summary=summary[:500],
                details={
                    "spend": float(measurement.spend),
                    "runs": measurement.runs,
                    "tokens": measurement.tokens,
                    "failed_spend": float(measurement.failed_spend),
                    "baseline": float(measurement.baseline) if measurement.baseline is not None else None,
                    "top_agents": cls.top_contributors(rule, now),
                },
            )
            rule.last_triggered_at = now
            rule.save(update_fields=[*fields, "last_triggered_at"])

        from apps.notifications import emitters

        emitters.cost_alert(event)
        return event

    @classmethod
    def evaluate_all(cls, organization=None, now=None) -> list[CostAlertEvent]:
        queryset = CostAlertRule.objects.filter(status="active")
        if organization is not None:
            queryset = queryset.for_organization(organization)
        events = []
        for rule in queryset.select_related("organization", "team", "user", "agent", "application"):
            try:
                event = cls.evaluate(rule, now)
            except Exception:
                # One broken rule must not stop the others being checked.
                logger.exception("Cost alert rule %s failed to evaluate", rule.pk)
                continue
            if event is not None:
                events.append(event)
        return events
