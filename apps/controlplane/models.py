"""IRGF M13 drift records, and link 07 verifications of what agents did."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.controlplane import irgf
from apps.core.models import OrganizationOwnedModel


class DriftCategory(models.TextChoices):
    STRUCTURAL = irgf.STRUCTURAL, "Structural"
    CONFIGURATION = irgf.CONFIGURATION, "Configuration"
    INTEGRATION = irgf.INTEGRATION, "Integration and dependency"
    SECURITY = irgf.SECURITY, "Security"
    DATA_LINEAGE = irgf.DATA_LINEAGE, "Data lineage and governance"
    BEHAVIORAL = irgf.BEHAVIORAL, "AI-model behavioral"
    POLICY = irgf.POLICY, "Policy and regulatory"
    COST = irgf.COST, "Cost"


class DriftStatus(models.TextChoices):
    OPEN = "open", "Open"
    RATIFYING = "ratifying", "Ratifying at G4"
    EXCEPTING = "excepting", "Exception requested"
    EXCEPTED = "excepted", "Excepted (M17)"
    RESOLVED = "resolved", "Resolved"


class Resolution(models.TextChoices):
    REVERT = "revert", "Revert"
    RATIFY = "ratify", "Ratify"
    EXCEPT = "except", "Except"
    ESCALATE = "escalate", "Escalate"


#: Anything not yet closed. A recurrence of the same divergence adds to it.
UNRESOLVED_STATUSES = (
    DriftStatus.OPEN, DriftStatus.RATIFYING, DriftStatus.EXCEPTING, DriftStatus.EXCEPTED,
)


class DriftRecord(OrganizationOwnedModel):
    """IRGF M13 Drift/Alert Record: running state that differs from the approved state."""

    agent = models.ForeignKey(
        "agents.AIAgent", null=True, blank=True, on_delete=models.CASCADE, related_name="drift_records"
    )
    application = models.ForeignKey(
        "applications_registry.AIApplication", null=True, blank=True, on_delete=models.CASCADE,
        related_name="drift_records",
    )
    authorization = models.ForeignKey(
        "lifecycle.DeploymentAuthorization", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="drift_records", help_text="The approved state the running state was compared with.",
    )

    signal = models.CharField(max_length=40, db_index=True)
    category = models.CharField(max_length=30, choices=DriftCategory.choices, db_index=True)
    is_incident = models.BooleanField(
        default=False, help_text="Agent boundary divergence: an incident at any tier."
    )
    tier = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Empty when unclassified.")
    title = models.CharField(max_length=300)
    detail = models.TextField(blank=True)
    expected = models.JSONField(null=True, blank=True, help_text="The approved state.")
    observed = models.JSONField(null=True, blank=True, help_text="The running state.")
    evidence = models.JSONField(default=list, blank=True)

    #: One unresolved record per divergence: recurrences add to it.
    fingerprint = models.CharField(max_length=255, db_index=True)
    occurrences = models.PositiveIntegerField(default=1)
    first_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_seen_at = models.DateTimeField(default=timezone.now)

    route = models.CharField(max_length=20)
    response_due = models.DateTimeField(null=True, blank=True, db_index=True)
    automatic_action = models.CharField(max_length=30, blank=True)
    auto_suspended = models.BooleanField(default=False)
    notified_at = models.DateTimeField(null=True, blank=True)
    escalated_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=20, choices=DriftStatus.choices, default=DriftStatus.OPEN, db_index=True
    )
    resolution = models.CharField(max_length=20, choices=Resolution.choices, blank=True)
    resolution_note = models.TextField(blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    change_record = models.ForeignKey(
        "lifecycle.ChangeRecord", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="drift_records", help_text="Ratify: the change Gate G4 decides.",
    )
    exception = models.ForeignKey(
        "lifecycle.GovernanceException", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="drift_records", help_text="Except: the time-boxed exception under M17.",
    )
    recurrence_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="recurrences"
    )

    class Meta:
        ordering = ["-last_seen_at"]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["fingerprint", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(agent__isnull=False) | Q(application__isnull=False),
                name="drift_record_has_subject",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.subject} · {self.title}"

    @property
    def subject(self):
        return self.agent or self.application

    @property
    def signal_info(self) -> irgf.Signal | None:
        return irgf.SIGNALS.get(self.signal)

    @property
    def category_info(self) -> irgf.Category:
        return irgf.CATEGORIES[self.category]

    @property
    def response(self) -> irgf.Response:
        return irgf.response_for(self.category, self.tier, incident=self.is_incident)

    @property
    def triage_tier(self) -> int:
        return irgf.effective_tier(self.tier)

    @property
    def is_open(self) -> bool:
        return self.status != DriftStatus.RESOLVED

    @property
    def needs_decision(self) -> bool:
        return self.status == DriftStatus.OPEN

    @property
    def is_overdue(self) -> bool:
        return bool(
            self.status == DriftStatus.OPEN and self.response_due and self.response_due < timezone.now()
        )


class VerificationOutcome(models.TextChoices):
    CONFORMS = "conforms", "Conforms"
    DIVERGES = "diverges", "Diverges"
    UNGOVERNED = "ungoverned", "No authorized state"


class Verification(OrganizationOwnedModel):
    """Agent chain link 07: one action checked against the authorized state."""

    execution = models.OneToOneField(
        "executions.Execution", null=True, blank=True, on_delete=models.CASCADE, related_name="verification"
    )
    tool_call = models.OneToOneField(
        "mcp_gateway.MCPToolCall", null=True, blank=True, on_delete=models.CASCADE,
        related_name="verification",
    )
    #: A message through the agent mailbox before the A2A Gateway, kept for its history.
    message = models.OneToOneField(
        "gateway.GatewayMessage", null=True, blank=True, on_delete=models.CASCADE,
        related_name="verification",
    )
    #: Agent-to-agent hand-off through the A2A Gateway: a delegation (IRGF chapter 16).
    a2a_task = models.OneToOneField(
        "a2a.A2ATask", null=True, blank=True, on_delete=models.CASCADE, related_name="verification",
    )
    agent = models.ForeignKey(
        "agents.AIAgent", null=True, blank=True, on_delete=models.SET_NULL, related_name="verifications"
    )
    tier = models.PositiveSmallIntegerField(null=True, blank=True)
    acted_at = models.DateTimeField()
    recorded_at = models.DateTimeField(help_text="When the platform learned of the action.")
    verified_at = models.DateTimeField(default=timezone.now, db_index=True)
    latency_ms = models.PositiveIntegerField(default=0, help_text="From recorded to verified.")
    ceiling_seconds = models.PositiveIntegerField()
    late = models.BooleanField(default=False, db_index=True)
    outcome = models.CharField(max_length=20, choices=VerificationOutcome.choices, db_index=True)
    findings = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-verified_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(execution__isnull=False, tool_call__isnull=True, message__isnull=True, a2a_task__isnull=True)
                    | Q(execution__isnull=True, tool_call__isnull=False, message__isnull=True, a2a_task__isnull=True)
                    | Q(execution__isnull=True, tool_call__isnull=True, message__isnull=False, a2a_task__isnull=True)
                    | Q(execution__isnull=True, tool_call__isnull=True, message__isnull=True, a2a_task__isnull=False)
                ),
                name="verification_has_one_action",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_outcome_display()} · {self.kind} {self.action_id}"

    @property
    def kind(self) -> str:
        if self.tool_call_id:
            return "tool_call"
        if self.a2a_task_id:
            return "a2a_task"
        return "message" if self.message_id else "execution"

    @property
    def action_id(self):
        return self.tool_call_id or self.a2a_task_id or self.message_id or self.execution_id
