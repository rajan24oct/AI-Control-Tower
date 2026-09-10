from __future__ import annotations

from celery import shared_task

from apps.approvals.services import ApprovalService


@shared_task(name="apps.approvals.tasks.expire_stale_approvals")
def expire_stale_approvals() -> int:
    """Expire approval requests nobody decided in time (section 24)."""
    return ApprovalService.expire_stale()
