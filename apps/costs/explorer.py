"""The cost explorer: spend grouped by any dimension, under any drill-down.

A drill-down is a stack of filters -- department "Claims", then team
"Customer Experience", then agent "Claims Triage Agent" -- and a dimension to
group what is left by. Every answer carries the previous period alongside, so
a big number can be told apart from a growing one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import CharField, Count, F, Q, Sum, Value
from django.db.models.functions import Coalesce, Concat, NullIf, Trim, TruncDate
from django.utils import timezone

from apps.costs.attribution import (
    UNASSIGNED,
    department_expression,
    team_id_expression,
    team_name_expression,
)
from apps.executions.models import Execution, ExecutionStatusChoices

ZERO = Decimal("0")
FAILURE_STATUSES = [ExecutionStatusChoices.FAILED, ExecutionStatusChoices.TIMED_OUT]
METRICS = {"cost": "Cost", "tokens": "Tokens", "runs": "Runs"}
UNASSIGNED_LABEL = "Unattributed"
#: Series beyond this many are folded into "Other" on the trend chart.
SERIES_LIMIT = 5


@dataclass(frozen=True)
class Dimension:
    key: str
    label: str
    plural: str
    icon: str

    def key_expression(self):
        return DIMENSION_EXPRESSIONS[self.key][0]()

    def label_expression(self):
        return DIMENSION_EXPRESSIONS[self.key][1]()


DIMENSION_EXPRESSIONS = {
    "department": (department_expression, department_expression),
    "team": (team_id_expression, team_name_expression),
    "application": (lambda: F("application_id"), lambda: F("application__name")),
    "agent": (lambda: F("agent_id"), lambda: F("agent__name")),
    "model": (lambda: F("model_id"), lambda: F("model__model_name")),
    "provider": (lambda: F("provider_id"), lambda: F("provider__name")),
    "user": (
        lambda: F("user_id"),
        lambda: Coalesce(
            NullIf(Trim(Concat("user__first_name", Value(" "), "user__last_name")), Value("")),
            F("user__email"),
            output_field=CharField(),
        ),
    ),
    "environment": (lambda: F("environment"), lambda: F("environment")),
}

DIMENSIONS = {
    dimension.key: dimension
    for dimension in [
        Dimension("department", "Department", "Departments", "bi-building"),
        Dimension("team", "Team", "Teams", "bi-people"),
        Dimension("application", "Application", "Applications", "bi-window-stack"),
        Dimension("agent", "Agent", "Agents", "bi-robot"),
        Dimension("model", "Model", "Models", "bi-cpu"),
        Dimension("provider", "Provider", "Providers", "bi-hdd-network"),
        Dimension("user", "Individual", "Individuals", "bi-person"),
        Dimension("environment", "Environment", "Environments", "bi-layers"),
    ]
}

#: The natural drill path: from who owns the spend down to who supplies it.
DEFAULT_PATH = ["department", "team", "agent", "model", "provider"]


def _num(value) -> float:
    return float(value or 0)


def _change(current: Decimal, previous: Decimal) -> float | None:
    if not previous:
        return None
    return round(float((current - previous) / previous * 100), 1)


class CostExplorerService:
    def __init__(self, organization, days: int = 30, filters: dict[str, str] | None = None, now=None):
        self.organization = organization
        self.days = days
        self.now = now or timezone.now()
        today = timezone.localdate(self.now)
        self.start = today - timedelta(days=days - 1)
        self.end = today + timedelta(days=1)
        self.previous_start = self.start - timedelta(days=days)
        self.filters = {key: value for key, value in (filters or {}).items() if key in DIMENSIONS and value}

    # -- Querysets ----------------------------------------------------------------

    def _bounds(self, start, end):
        tz = timezone.get_current_timezone()
        return (
            timezone.make_aware(datetime.combine(start, time.min), tz),
            timezone.make_aware(datetime.combine(end, time.min), tz),
        )

    def _queryset(self, start, end, *dimensions: str):
        lower, upper = self._bounds(start, end)
        queryset = Execution.objects.for_organization(self.organization).filter(
            started_at__gte=lower, started_at__lt=upper
        )
        needed = set(dimensions) | set(self.filters)
        annotations = {}
        for key in needed:
            dimension = DIMENSIONS[key]
            annotations[f"k_{key}"] = dimension.key_expression()
            annotations[f"l_{key}"] = dimension.label_expression()
        if annotations:
            queryset = queryset.annotate(**annotations)
        for key, value in self.filters.items():
            if value == UNASSIGNED:
                queryset = queryset.filter(**{f"k_{key}__isnull": True})
            else:
                queryset = queryset.filter(**{f"k_{key}": value})
        return queryset

    # -- Answers -------------------------------------------------------------------

    def breadcrumbs(self) -> list[dict]:
        crumbs = []
        for key, value in self.filters.items():
            label = UNASSIGNED_LABEL
            if value != UNASSIGNED:
                row = (
                    self._queryset(self.previous_start, self.end, key)
                    .values(f"l_{key}").first()
                )
                label = (row or {}).get(f"l_{key}") or value
            crumbs.append({
                "dimension": key, "dimension_label": DIMENSIONS[key].label,
                "key": value, "label": label,
            })
        return crumbs

    def suggested_next(self, current: str) -> str | None:
        """The next step down the natural path that is not already pinned."""
        pinned = set(self.filters) | {current}
        start = DEFAULT_PATH.index(current) + 1 if current in DEFAULT_PATH else 0
        for key in DEFAULT_PATH[start:]:
            if key not in pinned:
                return key
        for key in DIMENSIONS:
            if key not in pinned:
                return key
        return None

    def breakdown(self, dimension: str, metric: str = "cost", limit: int = 50) -> dict:
        if dimension not in DIMENSIONS:
            dimension = "department"
        metric = metric if metric in METRICS else "cost"
        key_field, label_field = f"k_{dimension}", f"l_{dimension}"

        rows = list(
            self._queryset(self.start, self.end, dimension)
            .values(key_field, label_field)
            .annotate(
                # Not `cost=`: a later Sum("cost") in the same call would sum the alias.
                spend=Sum("cost"), runs=Count("id"), tokens=Sum("total_tokens"),
                failed_cost=Sum("cost", filter=Q(status__in=FAILURE_STATUSES)),
            )
        )
        previous = {
            row[key_field]: row["cost"] or ZERO
            for row in self._queryset(self.previous_start, self.start, dimension)
            .values(key_field).annotate(cost=Sum("cost"))
        }

        total_cost = sum((row["spend"] or ZERO for row in rows), ZERO)
        total_runs = sum(row["runs"] for row in rows)
        total_tokens = sum(row["tokens"] or 0 for row in rows)
        total_metric = {"cost": total_cost, "runs": total_runs, "tokens": total_tokens}[metric]
        previous_total = sum(previous.values(), ZERO)

        result_rows = []
        for row in rows:
            key = row[key_field]
            cost = row["spend"] or ZERO
            value = {"cost": cost, "runs": row["runs"], "tokens": row["tokens"] or 0}[metric]
            result_rows.append({
                "key": str(key) if key is not None else UNASSIGNED,
                "label": row[label_field] or UNASSIGNED_LABEL,
                "cost": round(_num(cost), 4),
                "runs": row["runs"],
                "tokens": int(row["tokens"] or 0),
                "failed_cost": round(_num(row["failed_cost"]), 4),
                "avg_cost": round(_num(cost / row["runs"]) if row["runs"] else 0, 5),
                "share": round(_num(value) / _num(total_metric) * 100, 1) if total_metric else 0,
                "prev_cost": round(_num(previous.get(key)), 4),
                "change_pct": _change(cost, previous.get(key) or ZERO),
                "value": _num(value),
            })
        result_rows.sort(key=lambda row: row["value"], reverse=True)
        result_rows = result_rows[:limit]

        return {
            "dimension": dimension,
            "dimension_label": DIMENSIONS[dimension].label,
            "dimension_plural": DIMENSIONS[dimension].plural,
            "metric": metric,
            "metric_label": METRICS[metric],
            "days": self.days,
            "period": {
                "start": self.start.isoformat(),
                "end": (self.end - timedelta(days=1)).isoformat(),
            },
            "totals": {
                "cost": round(_num(total_cost), 2),
                "runs": total_runs,
                "tokens": total_tokens,
                "prev_cost": round(_num(previous_total), 2),
                "change_pct": _change(total_cost, previous_total),
                "avg_cost": round(_num(total_cost / total_runs) if total_runs else 0, 5),
                "items": len(rows),
            },
            "rows": result_rows,
            "series": self.series(dimension, metric, [row["key"] for row in result_rows[:SERIES_LIMIT]]),
            "breadcrumbs": self.breadcrumbs(),
            "filters": self.filters,
            "next": self.suggested_next(dimension),
            "dimensions": [
                {"key": d.key, "label": d.label, "icon": d.icon, "pinned": d.key in self.filters}
                for d in DIMENSIONS.values()
            ],
            "insights": self.insights(dimension, result_rows, total_cost),
        }

    def series(self, dimension: str, metric: str, top_keys: list[str]) -> dict:
        key_field = f"k_{dimension}"
        aggregate = {"cost": Sum("cost"), "runs": Count("id"), "tokens": Sum("total_tokens")}[metric]
        rows = (
            self._queryset(self.start, self.end, dimension)
            .annotate(day=TruncDate("started_at"))
            .values("day", key_field)
            .annotate(value=aggregate)
        )
        days = [self.start + timedelta(days=offset) for offset in range(self.days)]
        index = {day: position for position, day in enumerate(days)}
        buckets = {key: [0.0] * len(days) for key in top_keys}
        other = [0.0] * len(days)
        for row in rows:
            position = index.get(row["day"])
            if position is None:
                continue
            key = str(row[key_field]) if row[key_field] is not None else UNASSIGNED
            target = buckets.get(key, other)
            target[position] += _num(row["value"])
        datasets = [{"key": key, "data": [round(v, 4) for v in values]} for key, values in buckets.items()]
        if any(other):
            datasets.append({"key": "__other__", "data": [round(v, 4) for v in other]})
        return {"labels": [day.strftime("%d %b") for day in days], "datasets": datasets}

    def insights(self, dimension: str, rows: list[dict], total_cost: Decimal) -> list[str]:
        """Two or three sentences a reader would otherwise have to work out."""
        if not rows or not total_cost:
            return []
        plural = DIMENSIONS[dimension].plural.lower()
        notes = []
        by_cost = sorted(rows, key=lambda row: row["cost"], reverse=True)
        top = by_cost[0]
        notes.append(f"{top['label']} accounts for {top['cost'] / _num(total_cost) * 100:.0f}% of spend here.")
        if len(by_cost) >= 5:
            top_three = sum(row["cost"] for row in by_cost[:3]) / _num(total_cost) * 100
            notes.append(f"The top 3 of {len(by_cost)} {plural} make up {top_three:.0f}% of spend.")
        growing = [row for row in rows if row["change_pct"] is not None and row["cost"] >= 1]
        if growing:
            fastest = max(growing, key=lambda row: row["change_pct"])
            if fastest["change_pct"] >= 20:
                notes.append(
                    f"{fastest['label']} grew fastest: {fastest['change_pct']:+.0f}% on the previous {self.days} days."
                )
        new = [row for row in rows if row["prev_cost"] == 0 and row["cost"] >= 1]
        if new:
            notes.append(f"{len(new)} of these {plural} had no spend in the previous period.")
        failed = sum(row["failed_cost"] for row in rows)
        if failed / _num(total_cost) >= 0.05:
            notes.append(f"{failed / _num(total_cost) * 100:.0f}% of this spend went on runs that failed.")
        return notes
