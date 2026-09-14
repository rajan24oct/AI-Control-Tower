"""Notifications (sections 1, 21, 24).

The platform already detects everything worth knowing about -- budget
thresholds, policy denials, approval requests, provider incidents. Until now
all of it only surfaced by pulling. These models are the push side.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.accounts.roles import Perm
from apps.core.models import BaseModel, OrganizationOwnedModel


class NotificationCategory(models.TextChoices):
    APPROVAL_REQUESTED = "approval_requested", "Approval Requested"
    APPROVAL_DECIDED = "approval_decided", "Approval Decided"
    BUDGET_ALERT = "budget_alert", "Budget Alert"
    POLICY_VIOLATION = "policy_violation", "Policy Violation"
    PROVIDER_HEALTH = "provider_health", "Provider Health"
    EXECUTION_FAILURE = "execution_failure", "Execution Failure"
    RISK_REVIEW = "risk_review", "Risk Review"
    AGENT_SUSPENDED = "agent_suspended", "Agent Suspended"
    GATE_DECISION = "gate_decision", "Gate Decision"
    DRIFT = "drift", "Drift"
    SYSTEM = "system", "System"


class NotificationSeverity(models.TextChoices):
    INFO = "info", "Info"
    WARNING = "warning", "Warning"
    CRITICAL = "critical", "Critical"


class Channel(models.TextChoices):
    IN_APP = "in_app", "In-app"
    EMAIL = "email", "Email"


#: Which capability a category is routed by. Recipients are derived from the
#: RBAC matrix rather than hardcoded role lists, so a new role that gains a
#: permission automatically starts receiving the matching notifications.
CATEGORY_PERMISSIONS = {
    NotificationCategory.APPROVAL_REQUESTED: Perm.APPROVAL_DECIDE,
    NotificationCategory.APPROVAL_DECIDED: Perm.APPROVAL_VIEW,
    NotificationCategory.BUDGET_ALERT: Perm.COST_MANAGE,
    NotificationCategory.POLICY_VIOLATION: Perm.POLICY_MANAGE,
    NotificationCategory.PROVIDER_HEALTH: Perm.PROVIDER_MANAGE,
    NotificationCategory.EXECUTION_FAILURE: Perm.AGENT_MANAGE,
    NotificationCategory.RISK_REVIEW: Perm.RISK_MANAGE,
    NotificationCategory.AGENT_SUSPENDED: Perm.AGENT_MANAGE,
    NotificationCategory.GATE_DECISION: Perm.GATE_DECIDE,
    NotificationCategory.DRIFT: Perm.RISK_MANAGE,
    NotificationCategory.SYSTEM: Perm.ORG_MANAGE,
}

#: Default channel settings. In-app is always on -- it is just a row the user
#: can ignore. Email is reserved for things someone must act on, so the
#: platform does not become noise people filter away.
DEFAULT_CHANNELS = {
    NotificationCategory.APPROVAL_REQUESTED: {"in_app": True, "email": True},
    NotificationCategory.APPROVAL_DECIDED: {"in_app": True, "email": False},
    NotificationCategory.BUDGET_ALERT: {"in_app": True, "email": True},
    NotificationCategory.POLICY_VIOLATION: {"in_app": True, "email": True},
    NotificationCategory.PROVIDER_HEALTH: {"in_app": True, "email": False},
    NotificationCategory.EXECUTION_FAILURE: {"in_app": True, "email": False},
    NotificationCategory.RISK_REVIEW: {"in_app": True, "email": False},
    NotificationCategory.AGENT_SUSPENDED: {"in_app": True, "email": True},
    NotificationCategory.GATE_DECISION: {"in_app": True, "email": True},
    NotificationCategory.DRIFT: {"in_app": True, "email": True},
    NotificationCategory.SYSTEM: {"in_app": True, "email": False},
}


class NotificationQuerySet(models.QuerySet):
    def for_recipient(self, user):
        if not user or not user.is_authenticated:
            return self.none()
        return self.filter(recipient=user)

    def unread(self):
        return self.filter(read_at__isnull=True)


class Notification(OrganizationOwnedModel):
    """One thing one person should know about.

    `dedupe_key` is what stops a recurring condition from notifying on every
    sweep: a budget sitting at 90% produces one notification, not one per
    scheduled check.
    """

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    category = models.CharField(
        max_length=40, choices=NotificationCategory.choices, db_index=True
    )
    severity = models.CharField(
        max_length=20, choices=NotificationSeverity.choices, default=NotificationSeverity.INFO
    )

    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    url = models.CharField(max_length=500, blank=True)

    resource_type = models.CharField(max_length=100, blank=True)
    resource_id = models.CharField(max_length=100, blank=True)

    dedupe_key = models.CharField(max_length=255, blank=True)
    delivered_channels = models.JSONField(default=list, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    objects = NotificationQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["recipient", "dedupe_key"],
                condition=models.Q(dedupe_key__gt=""),
                name="uniq_notification_dedupe_per_recipient",
            )
        ]
        indexes = [
            models.Index(fields=["recipient", "read_at", "-created_at"]),
            models.Index(fields=["organization", "category", "-created_at"]),
        ]

    def __str__(self) -> str:
        return self.title

    @property
    def is_read(self) -> bool:
        return self.read_at is not None


class NotificationPreference(BaseModel):
    """Per-user channel settings, stored as one row rather than one per category.

    Absent categories fall back to `DEFAULT_CHANNELS`, so a user who has never
    touched their settings still receives what matters.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preferences"
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="notification_preferences",
    )
    settings = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organization"], name="uniq_notification_prefs_per_user_org"
            )
        ]

    def __str__(self) -> str:
        return f"Notification settings for {self.user}"

    def channels_for(self, category: str) -> dict[str, bool]:
        defaults = DEFAULT_CHANNELS.get(category, {"in_app": True, "email": False})
        return {**defaults, **(self.settings or {}).get(category, {})}

    def wants(self, category: str, channel: str) -> bool:
        return bool(self.channels_for(category).get(channel, False))

    def set_channel(self, category: str, channel: str, enabled: bool) -> None:
        settings_map = dict(self.settings or {})
        category_settings = dict(settings_map.get(category, {}))
        category_settings[channel] = enabled
        settings_map[category] = category_settings
        self.settings = settings_map
