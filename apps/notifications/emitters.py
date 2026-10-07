"""Turns domain events into notifications.

All the message shaping lives here so the domain services stay focused: each
needs only a single call, and none of them needs to know who should be told or
through which channel.
"""

from __future__ import annotations

from django.urls import reverse

from apps.notifications.models import NotificationCategory, NotificationSeverity
from apps.notifications.services import NotificationService


def _with_routed(organization, category, extra) -> list:
    """The people a category routes to, plus anyone a record names itself."""
    return [*NotificationService.recipients_for(organization, category), *extra]


def budget_alert(alert) -> list:
    budget = alert.budget
    if alert.is_forecast:
        severity = NotificationSeverity.WARNING
        title = f"{budget.name} is forecast to exceed its budget"
        body = (
            f"At the current rate, spend will reach {alert.currency} {alert.spend:.2f} against a "
            f"limit of {alert.currency} {alert.budget_amount:.2f} ({alert.percentage:.0f}%) by "
            f"{alert.period_end}. There is still time to act."
        )
    else:
        severity = (
            NotificationSeverity.CRITICAL if alert.threshold >= 100 else NotificationSeverity.WARNING
        )
        over = " and is over budget" if alert.threshold >= 100 else ""
        title = f"{budget.name} reached {alert.threshold}% of budget"
        body = (
            f"Spend is {alert.currency} {alert.spend:.2f} against a limit of "
            f"{alert.currency} {alert.budget_amount:.2f}{over}, for the period "
            f"starting {alert.period_start}."
        )

    return NotificationService.notify(
        organization=alert.organization,
        category=NotificationCategory.BUDGET_ALERT,
        severity=severity,
        title=title,
        body=body,
        recipients=_with_routed(
            alert.organization, NotificationCategory.BUDGET_ALERT, budget.alert_recipients()
        ),
        url=reverse("costs:budget_detail", args=[budget.pk]),
        # One notification per threshold crossing, not one per scheduled sweep.
        dedupe_key=f"budget_alert:{alert.pk}",
        resource_type="costs.BudgetAlert",
        resource_id=alert.pk,
    )


def cost_alert(event) -> list:
    rule = event.rule
    extra = list(rule.recipients.filter(is_active=True))
    if rule.owner_id:
        extra.append(rule.owner)
    recipients = (
        _with_routed(event.organization, NotificationCategory.COST_ALERT, extra)
        if rule.notify_cost_managers else extra
    )
    return NotificationService.notify(
        organization=event.organization,
        category=NotificationCategory.COST_ALERT,
        severity=event.severity,
        title=f"Cost alert: {rule.name}",
        body=event.summary,
        recipients=recipients,
        url=reverse("costs:governance"),
        dedupe_key=f"cost_alert:{event.pk}",
        resource_type="costs.CostAlertEvent",
        resource_id=event.pk,
    )


def policy_violation(violation) -> list:
    denied = violation.effect == "deny"
    return NotificationService.notify(
        organization=violation.organization,
        category=NotificationCategory.POLICY_VIOLATION,
        severity=NotificationSeverity.CRITICAL if denied else NotificationSeverity.WARNING,
        title=f"Policy {'blocked an action' if denied else 'flagged an action'}: {violation.policy.name}",
        body=(
            f"{violation.reason}"
            + (f" Requested action: {violation.requested_action}." if violation.requested_action else "")
        ),
        url=reverse("policies:detail", args=[violation.policy_id]),
        dedupe_key=f"policy_violation:{violation.pk}",
        resource_type="policies.PolicyViolation",
        resource_id=violation.pk,
    )


def approval_requested(request) -> list:
    severity = {
        "critical": NotificationSeverity.CRITICAL,
        "high": NotificationSeverity.WARNING,
    }.get(request.risk_level, NotificationSeverity.INFO)

    # An assigned approver owns it; otherwise everyone who can decide is told.
    recipients = [request.assigned_approver] if request.assigned_approver_id else None

    return NotificationService.notify(
        organization=request.organization,
        category=NotificationCategory.APPROVAL_REQUESTED,
        severity=severity,
        title=f"Approval needed: {request.requested_action}",
        body=(
            f"{request.reason}\n\nRaised by "
            f"{request.requested_by_system or request.requested_by or 'the platform'}"
            f" at {request.risk_level} risk."
        ),
        url=reverse("approvals:detail", args=[request.pk]),
        recipients=recipients,
        dedupe_key=f"approval_requested:{request.pk}",
        resource_type="approvals.ApprovalRequest",
        resource_id=request.pk,
    )


def approval_decided(request) -> list:
    """Tell whoever raised it. Nobody else needs an inbox item for this."""
    if not request.requested_by_id:
        return []

    approved = request.status == "approved"
    return NotificationService.notify(
        organization=request.organization,
        category=NotificationCategory.APPROVAL_DECIDED,
        severity=NotificationSeverity.INFO if approved else NotificationSeverity.WARNING,
        title=f"{request.request_id} was {request.get_status_display().lower()}",
        body=(
            f"{request.requested_action}\n\nDecided by "
            f"{request.decision_by or 'the platform'}."
            + (f" Notes: {request.decision_notes}" if request.decision_notes else "")
        ),
        url=reverse("approvals:detail", args=[request.pk]),
        recipients=[request.requested_by],
        dedupe_key=f"approval_decided:{request.pk}",
        resource_type="approvals.ApprovalRequest",
        resource_id=request.pk,
    )


def provider_health_changed(provider, previous_status: str) -> list:
    recovered = provider.health_status == "healthy"
    severity = {
        "unhealthy": NotificationSeverity.CRITICAL,
        "degraded": NotificationSeverity.WARNING,
    }.get(provider.health_status, NotificationSeverity.INFO)

    return NotificationService.notify(
        organization=provider.organization,
        category=NotificationCategory.PROVIDER_HEALTH,
        severity=severity,
        title=(
            f"{provider.name} recovered"
            if recovered
            else f"{provider.name} is {provider.get_health_status_display().lower()}"
        ),
        body=(
            f"Health moved from {previous_status} to {provider.health_status}."
            + (f" {provider.last_health_message}" if provider.last_health_message else "")
        ),
        url=reverse("inventory:provider_detail", args=[provider.pk]),
        # Keyed by the transition, so flapping produces one item per change.
        dedupe_key=f"provider_health:{provider.pk}:{previous_status}:{provider.health_status}",
        resource_type="providers.AIProvider",
        resource_id=provider.pk,
    )


def risk_review_due(risk) -> list:
    return NotificationService.notify(
        organization=risk.organization,
        category=NotificationCategory.RISK_REVIEW,
        severity=NotificationSeverity.WARNING,
        title=f"{risk.risk_id} is overdue for review",
        body=f"{risk.title}\n\nReview was due {risk.review_date}.",
        url=reverse("risks:detail", args=[risk.pk]),
        recipients=[risk.owner] if risk.owner_id else None,
        # Re-notifies each day it stays overdue rather than only once.
        dedupe_key=f"risk_review:{risk.pk}:{risk.review_date}",
        resource_type="risks.Risk",
        resource_id=risk.pk,
    )


def approval_expiring(request) -> list:
    return NotificationService.notify(
        organization=request.organization,
        category=NotificationCategory.APPROVAL_REQUESTED,
        severity=NotificationSeverity.WARNING,
        title=f"{request.request_id} expires soon",
        body=(
            f"{request.requested_action}\n\nThis request expires at "
            f"{request.expires_at} and the run will time out if nobody decides."
        ),
        url=reverse("approvals:detail", args=[request.pk]),
        recipients=[request.assigned_approver] if request.assigned_approver_id else None,
        dedupe_key=f"approval_expiring:{request.pk}",
        resource_type="approvals.ApprovalRequest",
        resource_id=request.pk,
    )


def classification_needs_countersignature(record) -> list:
    """IRGF S2 scores stay provisional until someone independent countersigns (M6)."""
    subject = record.subject
    scorer = f" by {record.scored_by}" if record.scored_by_id else ""
    return NotificationService.notify(
        organization=record.organization,
        category=NotificationCategory.RISK_REVIEW,
        severity=NotificationSeverity.WARNING if record.tier >= 3 else NotificationSeverity.INFO,
        title=f"Countersign the risk classification of {subject}",
        body=(
            f"{subject} was scored Tier {record.tier}{scorer} (impact {record.impact}, "
            f"control deficit {record.control_deficit}). Until someone outside its delivery "
            "line countersigns, the tier is provisional: evidence for Gate G1, not Gate G2."
        ),
        url=reverse("classification:countersign", args=[record.pk]),
        dedupe_key=f"classification_countersign:{record.pk}",
        resource_type="classification.RiskClassification",
        resource_id=record.pk,
    )


def agent_suspended(agent) -> list:
    """The kill-switch was pulled (IRGF agent chain link 06)."""
    return NotificationService.notify(
        organization=agent.organization,
        category=NotificationCategory.AGENT_SUSPENDED,
        severity=NotificationSeverity.CRITICAL,
        title=f"{agent.name} was suspended",
        body=(
            f"The kill-switch was pulled by {agent.suspended_by_label or 'the platform'}. "
            f"Both gateways now refuse this agent.\n\nReason: {agent.suspension_reason}"
        ),
        url=reverse("inventory:agent_detail", args=[agent.pk]),
        dedupe_key=f"agent_suspended:{agent.pk}:{agent.suspended_at.isoformat() if agent.suspended_at else ''}",
        resource_type="agents.AIAgent",
        resource_id=agent.pk,
    )


def agent_resumed(agent, *, resumed_by: str, note: str = "") -> list:
    return NotificationService.notify(
        organization=agent.organization,
        category=NotificationCategory.AGENT_SUSPENDED,
        severity=NotificationSeverity.INFO,
        title=f"{agent.name} was resumed",
        body=(
            f"{resumed_by} released the kill-switch; the gateways serve this agent again."
            + (f"\n\nNote: {note}" if note else "")
        ),
        url=reverse("inventory:agent_detail", args=[agent.pk]),
        resource_type="agents.AIAgent",
        resource_id=agent.pk,
    )


def lifecycle_notice(record, *, category, title, body, url, severity=NotificationSeverity.WARNING,
                     recipients=None, dedupe_key="") -> list:
    """IRGF gate events: a G3, G4 or M17 decision needed or made, an exception
    lapsing, a condition overdue. The lifecycle service picks who is told."""
    return NotificationService.notify(
        organization=record.organization,
        category=category,
        severity=severity,
        title=title,
        body=body,
        url=url,
        recipients=recipients,
        dedupe_key=dedupe_key,
        resource_type=record._meta.label,
        resource_id=record.pk,
    )


def drift_notice(record, *, title, body, recipients, severity=NotificationSeverity.WARNING,
                 category=NotificationCategory.DRIFT, dedupe_key="") -> list:
    """IRGF M13: drift found, or its response window missed. The Control Plane
    routes by category and tier, and picks who is told."""
    return NotificationService.notify(
        organization=record.organization,
        category=category,
        severity=severity,
        title=title,
        body=body,
        url=reverse("controlplane:drift_detail", args=[record.pk]),
        recipients=recipients,
        dedupe_key=dedupe_key,
        resource_type=record._meta.label,
        resource_id=record.pk,
    )


def digest_notice(organization, *, recipient, title, body, url, category,
                  severity=NotificationSeverity.INFO, dedupe_key="") -> list:
    """One notification standing for many findings that wait for a review."""
    return NotificationService.notify(
        organization=organization,
        category=category,
        severity=severity,
        title=title,
        body=body,
        url=url,
        recipients=[recipient],
        dedupe_key=dedupe_key,
    )
