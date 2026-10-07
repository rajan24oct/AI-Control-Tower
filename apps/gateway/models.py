"""AI Gateway state.

The gateway puts the Control Tower in the live call path: agents send their
model requests here, and the platform applies policy, budget and routing before
forwarding to a provider. That is a deliberate departure from the observe-only
posture of the rest of the platform, and it changes what "correct" means for
this module -- everything here is written on the assumption that a caller is
waiting and that an upstream may be broken.

Only two things need to be persisted. Everything else about a gateway call is
recorded as an ordinary Execution, so gateway traffic shows up in the live
monitor, the cost dashboard and the audit trail exactly like reported traffic.
"""

from __future__ import annotations

from datetime import timedelta

from django.db import models
from django.utils import timezone

from apps.core.models import BaseModel, OrganizationOwnedModel


class CircuitState(models.TextChoices):
    CLOSED = "closed", "Closed"
    OPEN = "open", "Open"
    HALF_OPEN = "half_open", "Half open"


class ProviderCircuit(BaseModel):
    """A breaker per provider, so one dead upstream cannot slow every request.

    Persisted rather than held in memory: the failure a worker learns about is
    only useful if the other workers learn it too, and the local-memory cache is
    per-process. The write happens on state *changes*, not on every call.
    """

    provider = models.OneToOneField(
        "providers.AIProvider", on_delete=models.CASCADE, related_name="circuit"
    )
    state = models.CharField(
        max_length=20, choices=CircuitState.choices, default=CircuitState.CLOSED, db_index=True
    )
    consecutive_failures = models.PositiveIntegerField(default=0)
    opened_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    trips = models.PositiveIntegerField(
        default=0, help_text="Times this breaker has opened, ever."
    )

    class Meta:
        ordering = ["provider__name"]
        verbose_name = "provider circuit"

    def __str__(self) -> str:
        return f"{self.provider.name}: {self.get_state_display()}"

    def is_available(self, *, cool_off_seconds: int) -> bool:
        """Whether a call may be attempted now.

        An open breaker becomes half-open once the cool-off has passed: the
        next request is allowed through as a probe, so recovery is discovered
        by traffic rather than by a timer nobody is watching.
        """
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.HALF_OPEN:
            return True
        if self.opened_at and timezone.now() - self.opened_at >= timedelta(seconds=cool_off_seconds):
            return True
        return False


class GatewayMessage(OrganizationOwnedModel):
    """One agent-to-agent message brokered by the gateway's original mailbox.

    Superseded by the A2A Gateway: every hand-off between agents is now an
    `apps.a2a.A2ATask`, whichever endpoint it came through, and nothing writes
    these any more. Kept, read-only, so the messages sent before it -- and the
    verifications that point at them -- stay on the record.
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        DELIVERED = "delivered", "Delivered"
        READ = "read", "Read"
        BLOCKED = "blocked", "Blocked by policy"

    sender = models.ForeignKey(
        "agents.AIAgent", on_delete=models.CASCADE, related_name="sent_messages"
    )
    recipient = models.ForeignKey(
        "agents.AIAgent", on_delete=models.CASCADE, related_name="received_messages"
    )
    execution = models.ForeignKey(
        "executions.Execution", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="gateway_messages",
    )
    service_account = models.ForeignKey(
        "api.ServiceAccount", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="gateway_messages",
    )

    subject = models.CharField(max_length=255, blank=True)
    body = models.TextField()
    correlation_id = models.CharField(
        max_length=200, blank=True, db_index=True,
        help_text="Groups a request with its replies.",
    )
    metadata = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.QUEUED, db_index=True
    )
    blocked_reason = models.CharField(max_length=500, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "recipient", "status"]),
            models.Index(fields=["organization", "correlation_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.sender_id} -> {self.recipient_id}: {self.subject or self.body[:40]}"
