from __future__ import annotations

from celery import shared_task


@shared_task(name="apps.lifecycle.tasks.sweep_lifecycle")
def sweep_lifecycle() -> dict:
    """Expire M17 exceptions, warn before they lapse, escalate overdue G3 conditions."""
    from apps.lifecycle.services import ConditionService, ExceptionService

    result = ExceptionService.sweep()
    result["conditions_escalated"] = ConditionService.escalate_overdue()
    return result
