from __future__ import annotations

from celery import shared_task

from apps.costs.services import BudgetService, RollupService
from apps.organizations.models import Organization


@shared_task(name="apps.costs.tasks.check_budgets")
def check_budgets() -> int:
    """Raise budget alerts and cost-rule alerts. Returns the number raised."""
    from apps.costs.alerting import CostAlertService

    return len(BudgetService.evaluate_all()) + len(CostAlertService.evaluate_all())


@shared_task(name="apps.costs.tasks.rebuild_daily_rollups")
def rebuild_daily_rollups(days: int = 7) -> int:
    """Refresh daily usage aggregates for every organization."""
    return sum(
        RollupService.rebuild(organization, days=days)
        for organization in Organization.objects.all()
    )


@shared_task(name="apps.costs.tasks.sync_developer_tools")
def sync_developer_tools() -> int:
    """Pull GitHub Copilot seats and usage. Returns the number of subscriptions synced."""
    from apps.costs.devtools import DevToolSyncService

    return sum(1 for result in DevToolSyncService.sync_all() if result.ok)


@shared_task(name="apps.costs.tasks.analyse_cost_optimizations")
def analyse_cost_optimizations() -> int:
    """Refresh cost optimization recommendations. Returns how many were found."""
    from apps.costs.optimization import OptimizationService

    return OptimizationService.run_all()
