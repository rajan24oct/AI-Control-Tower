"""Scheduled notification sweeps (section 24).

These cover conditions with no natural trigger -- nothing *happens* when a risk
review date passes, so something has to look.
"""

from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from apps.approvals.models import ApprovalRequest, ApprovalStatus
from apps.notifications import emitters
from apps.risks.models import OPEN_STATUSES, Risk

#: How close to expiry an approval has to be before its approver is chased.
EXPIRY_WARNING_HOURS = 4


@shared_task(name="apps.notifications.tasks.notify_overdue_risk_reviews")
def notify_overdue_risk_reviews() -> int:
    """Chase risk owners whose review date has passed."""
    overdue = Risk.objects.filter(
        status__in=OPEN_STATUSES, review_date__lte=timezone.now().date()
    ).select_related("organization", "owner")

    return sum(len(emitters.risk_review_due(risk)) for risk in overdue)


@shared_task(name="apps.notifications.tasks.notify_expiring_approvals")
def notify_expiring_approvals() -> int:
    """Warn approvers before a pending request times out."""
    cutoff = timezone.now() + timedelta(hours=EXPIRY_WARNING_HOURS)
    expiring = ApprovalRequest.objects.filter(
        status=ApprovalStatus.PENDING,
        expires_at__isnull=False,
        expires_at__lte=cutoff,
        expires_at__gt=timezone.now(),
    ).select_related("organization", "assigned_approver")

    return sum(len(emitters.approval_expiring(request)) for request in expiring)
