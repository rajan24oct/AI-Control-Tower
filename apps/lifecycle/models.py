"""IRGF lifecycle records: Deployment Authorization (S7 · G3), AI Change (S9 · G4)
and Architecture Exception (M17).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.constants import Environment
from apps.core.models import OrganizationOwnedModel
from apps.lifecycle import irgf


class AuthorizationStatus(models.TextChoices):
    PENDING = "pending", "Awaiting decision"
    AUTHORIZED = "authorized", "In force"
    SUSPENDED = "suspended", "Suspended"
    REJECTED = "rejected", "Rejected"
    REVOKED = "revoked", "Revoked"
    SUPERSEDED = "superseded", "Superseded"


#: An authorization that is, or was until suspended, the agent's baseline.
BASELINE_STATUSES = (AuthorizationStatus.AUTHORIZED, AuthorizationStatus.SUSPENDED)


class SubjectMixin:
    """IRGF governs systems: here an agent, or an application and the agents it composes."""

    @property
    def subject(self):
        return self.agent if self.agent_id else self.application

    @property
    def subject_kind(self) -> str:
        return "agent" if self.agent_id else "application"

    @property
    def subject_owner(self):
        if self.agent_id:
            return self.agent.owner
        return self.application.business_owner or self.application.technical_owner

    @property
    def subject_owner_id(self):
        if self.agent_id:
            return self.agent.owner_id
        return self.application.business_owner_id or self.application.technical_owner_id


def _one_subject(name: str) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=(
            Q(agent__isnull=False, application__isnull=True)
            | Q(agent__isnull=True, application__isnull=False)
        ),
        name=name,
    )


class DeploymentAuthorization(SubjectMixin, OrganizationOwnedModel):
    """The Deployment Authorization Record: this configuration, this tier, this owner."""

    agent = models.ForeignKey(
        "agents.AIAgent", null=True, blank=True, on_delete=models.CASCADE, related_name="authorizations"
    )
    application = models.ForeignKey(
        "applications_registry.AIApplication", null=True, blank=True, on_delete=models.CASCADE,
        related_name="authorizations",
    )
    environment = models.CharField(max_length=20, choices=Environment.choices)
    status = models.CharField(
        max_length=20, choices=AuthorizationStatus.choices,
        default=AuthorizationStatus.PENDING, db_index=True,
    )

    tier = models.PositiveSmallIntegerField()
    classification = models.ForeignKey(
        "classification.RiskClassification", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="authorizations",
    )
    classification_confirmed_at = models.DateTimeField(null=True, blank=True)

    #: The authorized configuration: what the Control Plane compares against,
    #: and what change detection diffs the agent with.
    configuration = models.JSONField(default=dict)
    #: The G3 entry checks as they stood when the request was made.
    evidence = models.JSONField(default=list, blank=True)

    assurance_summary = models.TextField(blank=True)
    oversight_point = models.TextField(blank=True)
    monitoring_commitments = models.TextField(blank=True)
    accountable_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="accountable_authorizations",
    )

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="requested_authorizations",
    )
    requested_at = models.DateTimeField(default=timezone.now, db_index=True)
    signatures = models.JSONField(default=list, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    dissent = models.TextField(blank=True)

    suspended_at = models.DateTimeField(null=True, blank=True)
    suspension_reason = models.TextField(blank=True)
    #: Suspended by changes awaiting G4, and so restored once they are decided or
    #: reverted. Other suspensions end only with a new G3 authorization.
    suspended_for_changes = models.BooleanField(default=False)
    superseded_by = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="supersedes"
    )

    class Meta:
        ordering = ["-requested_at"]
        verbose_name = "deployment authorization"
        indexes = [
            models.Index(fields=["agent", "status"]),
            models.Index(fields=["application", "status"]),
            models.Index(fields=["organization", "status"]),
        ]
        constraints = [_one_subject("authorization_has_one_subject")]

    def __str__(self) -> str:
        return f"{self.subject} · {self.get_environment_display()} · {self.get_status_display()}"

    gate = irgf.AUTHORIZATION

    @property
    def is_in_force(self) -> bool:
        return self.status == AuthorizationStatus.AUTHORIZED

    @property
    def is_pending(self) -> bool:
        return self.status == AuthorizationStatus.PENDING

    @property
    def requester_id(self):
        return self.requested_by_id


class AuthorizationCondition(OrganizationOwnedModel):
    """A condition attached at G3, with an owner and a deadline.

    An unmet condition does not quietly lapse: at its deadline it escalates to
    the people who authorized the system.
    """

    authorization = models.ForeignKey(
        DeploymentAuthorization, on_delete=models.CASCADE, related_name="conditions"
    )
    description = models.TextField()
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="authorization_conditions",
    )
    due_date = models.DateField()
    met_at = models.DateTimeField(null=True, blank=True)
    met_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    met_note = models.TextField(blank=True)
    escalated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["due_date"]

    def __str__(self) -> str:
        return self.description[:80]

    @property
    def is_met(self) -> bool:
        return self.met_at is not None

    @property
    def is_overdue(self) -> bool:
        return not self.is_met and self.due_date < timezone.now().date()


class ChangeStatus(models.TextChoices):
    ACCEPTED = "accepted", "Accepted, no gate"
    AWAITING_CONFIRMATION = "awaiting_confirmation", "Awaiting second-person confirmation"
    AWAITING_G4 = "awaiting_g4", "Awaiting Gate G4"
    APPROVED = "approved", "Approved at G4"
    REJECTED = "rejected", "Rejected at G4; revert it"
    REVERTED = "reverted", "Reverted"
    SUPERSEDED = "superseded", "Resolved by re-authorization"


#: Changes still being tracked against the authorized configuration.
OPEN_CHANGE_STATUSES = (
    ChangeStatus.AWAITING_CONFIRMATION,
    ChangeStatus.AWAITING_G4,
    ChangeStatus.REJECTED,
)


class ChangeRecord(SubjectMixin, OrganizationOwnedModel):
    """The AI Change Record: one difference from the authorized configuration."""

    agent = models.ForeignKey(
        "agents.AIAgent", null=True, blank=True, on_delete=models.CASCADE, related_name="change_records"
    )
    application = models.ForeignKey(
        "applications_registry.AIApplication", null=True, blank=True, on_delete=models.CASCADE,
        related_name="change_records",
    )
    authorization = models.ForeignKey(
        DeploymentAuthorization, on_delete=models.CASCADE, related_name="changes"
    )
    field = models.CharField(max_length=40)
    change_type = models.CharField(max_length=40)
    classification = models.CharField(
        max_length=20, choices=[(c, c.title()) for c in irgf.CLASSES], db_index=True
    )
    classification_reason = models.TextField(blank=True)
    auto_classified = models.BooleanField(default=True)
    reclassified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reclassification_reason = models.TextField(blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)

    description = models.TextField()
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    tier_at_change = models.PositiveSmallIntegerField()
    autonomy_change = models.BooleanField(default=False)
    requires_rescore = models.BooleanField(default=False)
    requires_reauthorization = models.BooleanField(default=False)

    status = models.CharField(
        max_length=30, choices=ChangeStatus.choices, default=ChangeStatus.AWAITING_G4, db_index=True
    )
    detected_at = models.DateTimeField(default=timezone.now, db_index=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="changes_made",
    )
    changed_by_label = models.CharField(max_length=255, blank=True)

    delta_assurance_scope = models.TextField(blank=True)
    rollback_plan = models.TextField(blank=True)
    signatures = models.JSONField(default=list, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-detected_at"]
        verbose_name = "AI change record"
        indexes = [models.Index(fields=["authorization", "status"])]
        constraints = [_one_subject("change_record_has_one_subject")]

    def __str__(self) -> str:
        return f"{self.subject} · {self.change_label} ({self.classification})"

    gate = irgf.CHANGE

    @property
    def change_label(self) -> str:
        return irgf.CHANGE_TYPES.get(self.change_type, self.change_type)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_CHANGE_STATUSES

    @property
    def is_gated(self) -> bool:
        return self.classification in irgf.GATED

    @property
    def requester_id(self):
        return self.changed_by_id

    @property
    def tier(self) -> int:
        """The post-change tier routes the decision, as IRGF asks."""
        return max(self.tier_at_change or 1, self.subject.risk_tier or 1)


class ExceptionStatus(models.TextChoices):
    PENDING = "pending", "Awaiting decision"
    ACTIVE = "active", "Active"
    REJECTED = "rejected", "Rejected"
    EXPIRED = "expired", "Expired"
    REVOKED = "revoked", "Revoked"


class GovernanceException(SubjectMixin, OrganizationOwnedModel):
    """An Architecture Exception (M17), for one agent and for a while: one policy set
    aside, or one divergence the Control Plane found (M13's "Except") allowed to stay."""

    agent = models.ForeignKey(
        "agents.AIAgent", on_delete=models.CASCADE, related_name="governance_exceptions"
    )
    policy = models.ForeignKey(
        "policies.Policy", null=True, blank=True, on_delete=models.CASCADE, related_name="exceptions"
    )
    divergence = models.TextField(
        blank=True, help_text="The drift allowed to persist, when no policy is set aside."
    )
    justification = models.TextField()
    compensating_control = models.TextField(blank=True)
    duration_days = models.PositiveSmallIntegerField()
    tier = models.PositiveSmallIntegerField()

    status = models.CharField(
        max_length=20, choices=ExceptionStatus.choices, default=ExceptionStatus.PENDING, db_index=True
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="requested_exceptions",
    )
    requested_at = models.DateTimeField(default=timezone.now)
    signatures = models.JSONField(default=list, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    renewal_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="renewals"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    revoke_reason = models.TextField(blank=True)
    expiry_warned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-requested_at"]
        verbose_name = "governance exception"
        indexes = [models.Index(fields=["agent", "status", "expires_at"])]

    def __str__(self) -> str:
        return f"{self.agent} · {self.subject_label} ({self.get_status_display().lower()})"

    gate = irgf.EXCEPTION

    @property
    def subject_label(self) -> str:
        """What is set aside: a policy by name, or the divergence."""
        if self.policy_id:
            return f"'{self.policy.name}'"
        return self.divergence or "a divergence"

    @property
    def is_active(self) -> bool:
        return (
            self.status == ExceptionStatus.ACTIVE
            and self.expires_at is not None
            and self.expires_at > timezone.now()
        )

    @property
    def requester_id(self):
        return self.requested_by_id

    @property
    def can_renew(self) -> bool:
        return self.tier < irgf.NO_RENEWAL_FROM_TIER and self.status in (
            ExceptionStatus.ACTIVE, ExceptionStatus.EXPIRED
        )
