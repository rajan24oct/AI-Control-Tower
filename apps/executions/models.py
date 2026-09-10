"""Execution and event records (sections 4 and 13).

These are the Control Tower's observability primitives. Executions are almost
always created by an *external* engine reporting in -- the Control Tower does
not run AI itself (section 46) -- so every field here must be populatable from
a standardized payload rather than from local state.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.constants import Environment
from apps.core.models import BaseModel, OrganizationOwnedModel
from integrations.base.events import EventType, ExecutionStatus, TERMINAL_STATUSES


class ExecutionStatusChoices(models.TextChoices):
    """Mirrors `integrations.base.events.ExecutionStatus` for the ORM.

    The adapter layer stays framework-free; this is the Django-facing view of
    the same vocabulary.
    """

    QUEUED = ExecutionStatus.QUEUED, "Queued"
    RUNNING = ExecutionStatus.RUNNING, "Running"
    WAITING_FOR_APPROVAL = ExecutionStatus.WAITING_FOR_APPROVAL, "Waiting for Approval"
    COMPLETED = ExecutionStatus.COMPLETED, "Completed"
    FAILED = ExecutionStatus.FAILED, "Failed"
    CANCELLED = ExecutionStatus.CANCELLED, "Cancelled"
    TIMED_OUT = ExecutionStatus.TIMED_OUT, "Timed Out"


class ApprovalState(models.TextChoices):
    NOT_REQUIRED = "not_required", "Not Required"
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    EXPIRED = "expired", "Expired"
    CANCELLED = "cancelled", "Cancelled"


class EventTypeChoices(models.TextChoices):
    EXECUTION_STARTED = EventType.EXECUTION_STARTED, "Execution Started"
    PROMPT_CREATED = EventType.PROMPT_CREATED, "Prompt Created"
    MODEL_REQUEST_STARTED = EventType.MODEL_REQUEST_STARTED, "Model Request Started"
    MODEL_REQUEST_COMPLETED = EventType.MODEL_REQUEST_COMPLETED, "Model Request Completed"
    TOOL_SELECTED = EventType.TOOL_SELECTED, "Tool Selected"
    TOOL_CALL_STARTED = EventType.TOOL_CALL_STARTED, "Tool Call Started"
    TOOL_CALL_COMPLETED = EventType.TOOL_CALL_COMPLETED, "Tool Call Completed"
    AGENT_STARTED = EventType.AGENT_STARTED, "Agent Started"
    AGENT_STEP_STARTED = EventType.AGENT_STEP_STARTED, "Agent Step Started"
    AGENT_STEP_COMPLETED = EventType.AGENT_STEP_COMPLETED, "Agent Step Completed"
    APPROVAL_REQUESTED = EventType.APPROVAL_REQUESTED, "Approval Requested"
    APPROVAL_GRANTED = EventType.APPROVAL_GRANTED, "Approval Granted"
    APPROVAL_REJECTED = EventType.APPROVAL_REJECTED, "Approval Rejected"
    POLICY_VIOLATION = EventType.POLICY_VIOLATION, "Policy Violation"
    ERROR = EventType.ERROR, "Error"
    EXECUTION_COMPLETED = EventType.EXECUTION_COMPLETED, "Execution Completed"
    EXECUTION_CANCELLED = EventType.EXECUTION_CANCELLED, "Execution Cancelled"


class ExecutionTrace(OrganizationOwnedModel):
    """Correlates several executions that belong to one logical AI request.

    A user question may fan out into an orchestrator run plus several
    sub-agent runs; the trace is what stitches them back together.
    """

    trace_id = models.CharField(max_length=200, db_index=True)
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "trace_id"], name="uniq_trace_per_org")
        ]
        indexes = [models.Index(fields=["organization", "-started_at"])]

    def __str__(self) -> str:
        return self.trace_id

    @property
    def execution_count(self) -> int:
        return self.executions.count()


class Execution(OrganizationOwnedModel):
    """One run of an AI application, agent or model call.

    Every relationship is nullable on purpose: telemetry frequently arrives
    before the corresponding asset has been registered, and dropping the record
    would be worse than storing it partially resolved.
    """

    external_id = models.CharField(
        max_length=200,
        db_index=True,
        help_text="The execution identifier used by the originating engine.",
    )
    trace = models.ForeignKey(
        ExecutionTrace, null=True, blank=True, on_delete=models.SET_NULL, related_name="executions"
    )

    application = models.ForeignKey(
        "applications_registry.AIApplication",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="executions",
    )
    agent = models.ForeignKey(
        "agents.AIAgent", null=True, blank=True, on_delete=models.SET_NULL, related_name="executions"
    )
    provider = models.ForeignKey(
        "providers.AIProvider",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="executions",
    )
    model = models.ForeignKey(
        "models_registry.AIModel",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="executions",
    )
    framework = models.CharField(max_length=40, blank=True)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="executions",
        help_text="The person on whose behalf the run happened, when known.",
    )
    service_account = models.ForeignKey(
        "api.ServiceAccount",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="executions",
        help_text="The external system that submitted this telemetry.",
    )
    source_adapter = models.CharField(max_length=64, blank=True)

    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)

    status = models.CharField(
        max_length=32,
        choices=ExecutionStatusChoices.choices,
        default=ExecutionStatusChoices.QUEUED,
        db_index=True,
    )
    environment = models.CharField(
        max_length=20, choices=Environment.choices, default=Environment.PRODUCTION, db_index=True
    )

    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    total_tokens = models.PositiveIntegerField(default=0)

    cost = models.DecimalField(max_digits=14, decimal_places=6, default=Decimal("0"))
    currency = models.CharField(max_length=3, default="USD")
    cost_is_derived = models.BooleanField(
        default=False,
        help_text="True when the Control Tower priced this run from the model registry "
        "because the engine reported no cost.",
    )

    error_message = models.TextField(blank=True)

    approval_required = models.BooleanField(default=False)
    approval_status = models.CharField(
        max_length=20, choices=ApprovalState.choices, default=ApprovalState.NOT_REQUIRED
    )

    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "external_id"], name="uniq_execution_per_org"
            )
        ]
        indexes = [
            models.Index(fields=["organization", "-started_at"]),
            models.Index(fields=["organization", "status", "-started_at"]),
            models.Index(fields=["organization", "agent", "-started_at"]),
            models.Index(fields=["organization", "application", "-started_at"]),
            models.Index(fields=["organization", "provider", "-started_at"]),
            models.Index(fields=["organization", "model", "-started_at"]),
            models.Index(fields=["organization", "environment"]),
            models.Index(fields=["trace"]),
        ]

    def __str__(self) -> str:
        return self.external_id

    @property
    def trace_key(self) -> str:
        """The engine's correlation id.

        Named `trace_key` rather than `trace_id` because Django already owns
        that attribute as the trace foreign key's raw value.
        """
        return self.trace.trace_id if self.trace_id else ""



    def save(self, *args, **kwargs):
        self.total_tokens = self.input_tokens + self.output_tokens
        super().save(*args, **kwargs)

    # -- Derived state ------------------------------------------------------

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_live(self) -> bool:
        return self.status in {
            ExecutionStatusChoices.QUEUED,
            ExecutionStatusChoices.RUNNING,
            ExecutionStatusChoices.WAITING_FOR_APPROVAL,
        }

    @property
    def duration_ms(self) -> int | None:
        """Latency as reported, or measured from the timestamps."""
        if self.latency_ms is not None:
            return self.latency_ms
        if self.ended_at and self.started_at:
            return int((self.ended_at - self.started_at).total_seconds() * 1000)
        return None

    @property
    def duration_display(self) -> str:
        duration = self.duration_ms
        if duration is None:
            return "—"
        if duration < 1000:
            return f"{duration} ms"
        return f"{duration / 1000:.2f} s"

    @property
    def label(self) -> str:
        return self.external_id[:8] if len(self.external_id) > 8 else self.external_id


class ExecutionEvent(BaseModel):
    """One normalized point on an execution timeline (section 4).

    The primary key doubles as the engine-supplied `event_id`, which makes
    at-least-once delivery safe: a replayed event collides instead of
    duplicating.
    """

    execution = models.ForeignKey(Execution, on_delete=models.CASCADE, related_name="events")
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="execution_events"
    )

    event_type = models.CharField(max_length=40, choices=EventTypeChoices.choices, db_index=True)
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    sequence = models.PositiveIntegerField(default=0)

    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cost = models.DecimalField(max_digits=14, decimal_places=6, default=Decimal("0"))

    # A single run can hit several models; this is per-event, not derivable
    # from the parent execution.
    model_id = models.CharField(max_length=200, blank=True)

    status = models.CharField(max_length=32, blank=True)
    error_message = models.TextField(blank=True)
    trace_id = models.CharField(max_length=200, blank=True, db_index=True)
    parent_event = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children"
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["execution_id", "sequence", "timestamp"]
        indexes = [
            models.Index(fields=["execution", "sequence"]),
            models.Index(fields=["organization", "-timestamp"]),
            models.Index(fields=["event_type", "-timestamp"]),
        ]

    def __str__(self) -> str:
        return f"{self.event_type} @ {self.timestamp:%H:%M:%S}"

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def is_error(self) -> bool:
        return self.event_type in {EventTypeChoices.ERROR, EventTypeChoices.POLICY_VIOLATION}
