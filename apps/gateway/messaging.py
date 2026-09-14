"""Agent-to-agent messaging through the gateway.

A mailbox, deliberately, and not an orchestrator. The gateway accepts a message
from one registered agent addressed to another, applies the same governance a
model call gets, records it, and holds it until the recipient collects it. It
does not decide what the recipient should do, chain steps together, run a graph,
or wake anything up. Section 40 puts multi-agent orchestration out of scope, and
this stays on the near side of that line on purpose: the moment the platform
starts deciding what happens next, it owns the behaviour of every agent
conversation in the estate.

What the platform gets in exchange for brokering is what it was always after --
agent-to-agent traffic becomes visible, attributable and governable instead of
being a set of direct calls nobody can see.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from django.utils import timezone

from apps.core.constants import Environment
from apps.gateway.models import GatewayMessage

logger = logging.getLogger(__name__)


class MessageBlocked(Exception):
    """Policy refused delivery. Recorded, not silently dropped."""

    def __init__(self, message: str, *, policies=None):
        super().__init__(message)
        self.message = message
        self.policies = policies or []


@dataclass
class MessageRequest:
    sender: object
    recipient: object
    body: str
    subject: str = ""
    correlation_id: str = ""
    environment: str = Environment.PRODUCTION
    metadata: dict = field(default_factory=dict)


class MessagingService:
    @classmethod
    def send(cls, organization, request: MessageRequest, *, service_account=None) -> GatewayMessage:
        """Broker one message, or record why it was refused."""
        cls._same_tenant(organization, request)

        if request.sender.is_suspended:
            # The kill-switch stops everything the agent does, messages included.
            blocked_reason, policies = (
                f"Agent '{request.sender.name}' is suspended by its kill-switch and cannot send.",
                [],
            )
        else:
            blocked_reason, policies = cls._policy_verdict(organization, request)
        message = GatewayMessage.objects.create(
            organization=organization,
            sender=request.sender,
            recipient=request.recipient,
            service_account=service_account,
            subject=request.subject[:255],
            body=request.body,
            correlation_id=request.correlation_id[:200],
            metadata={**request.metadata, "policies": policies} if policies else request.metadata,
            status=GatewayMessage.Status.BLOCKED if blocked_reason else GatewayMessage.Status.QUEUED,
            blocked_reason=blocked_reason[:500],
        )

        if blocked_reason:
            # A refused message is kept, not discarded. "Which agent tried to
            # tell which other agent what, and why were they stopped" is the
            # question this whole module exists to be able to answer.
            raise MessageBlocked(blocked_reason, policies=policies)
        return message

    @staticmethod
    def _same_tenant(organization, request: MessageRequest) -> None:
        """Both ends must belong to the caller's organization.

        Checked here rather than relying on the queryset that fetched them,
        because a messaging bug that crosses tenants is a data breach, not a
        display glitch.
        """
        for role, agent in (("sender", request.sender), ("recipient", request.recipient)):
            if agent is None or agent.organization_id != organization.pk:
                raise MessageBlocked(f"Unknown {role}.")

    @staticmethod
    def _policy_verdict(organization, request: MessageRequest) -> tuple[str, list]:
        """Run the message past the policy engine before it is delivered."""
        from apps.policies.services import PolicyContext, PolicyEngine

        context = PolicyContext(
            agent=request.sender,
            action="gateway.message",
            tool=f"message:{request.recipient.agent_id}",
            data_classification=getattr(request.sender, "data_classification", "") or "",
            environment=request.environment,
            estimated_cost=Decimal("0"),
        )
        # Agent-level policies only: no model serves a message, so gating one
        # on the sender's declared model would be checking something that has
        # nothing to do with whether the message may be sent.
        decision = PolicyEngine.check_agent(
            organization, context, requested_action="gateway.message"
        )

        if not decision.allowed:
            return (decision.reason or "Blocked by policy.", decision.policy_names)
        if decision.requires_approval:
            # There is nobody to wait for a decision here: a message is either
            # deliverable now or it is not. Requiring approval means refusing.
            return (
                decision.reason or "This message requires human approval, which messaging cannot await.",
                decision.policy_names,
            )
        return ("", decision.policy_names)

    @classmethod
    def inbox(cls, organization, agent, *, unread_only: bool = True):
        queryset = (
            GatewayMessage.objects.for_organization(organization)
            .filter(recipient=agent)
            .exclude(status=GatewayMessage.Status.BLOCKED)
            .select_related("sender", "recipient")
        )
        if unread_only:
            queryset = queryset.exclude(status=GatewayMessage.Status.READ)
        return queryset.order_by("created_at")

    @classmethod
    def collect(cls, organization, agent, *, limit: int = 50) -> list[GatewayMessage]:
        """Hand the recipient its waiting messages and mark them read.

        Delivery is a pull, not a push: the gateway never calls out to an agent,
        so an agent that is down cannot make the gateway hang, and a message
        cannot be lost because nothing was listening.
        """
        messages = list(cls.inbox(organization, agent)[:limit])
        now = timezone.now()
        for message in messages:
            message.status = GatewayMessage.Status.READ
            message.read_at = now
            if message.delivered_at is None:
                message.delivered_at = now
        if messages:
            GatewayMessage.objects.bulk_update(
                messages, ["status", "read_at", "delivered_at", "updated_at"]
            )
        return messages
