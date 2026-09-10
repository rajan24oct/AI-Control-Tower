from __future__ import annotations

from celery import shared_task

from apps.providers.services import ProviderHealthService


@shared_task(name="apps.providers.tasks.run_provider_health_checks")
def run_provider_health_checks() -> dict[str, int]:
    """Periodic provider liveness sweep (scheduled in aig_tower.celery)."""
    return ProviderHealthService.check_all()
