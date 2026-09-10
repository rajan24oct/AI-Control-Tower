from __future__ import annotations

from celery import shared_task

from apps.costs.services import BudgetService, RollupService
from apps.organizations.models import Organization


@shared_task(name="apps.costs.tasks.check_budgets")
def check_budgets() -> int:
    """Raise alerts for budgets that crossed a threshold. Returns alerts raised."""
    return len(BudgetService.evaluate_all())


@shared_task(name="apps.costs.tasks.rebuild_daily_rollups")
def rebuild_daily_rollups(days: int = 7) -> int:
    """Refresh daily usage aggregates for every organization."""
    return sum(
        RollupService.rebuild(organization, days=days)
        for organization in Organization.objects.all()
    )
