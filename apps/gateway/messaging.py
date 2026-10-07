"""Agent-to-agent messaging: the original mailbox, now one door into the A2A Gateway.

The first agent-to-agent channel was a mailbox: one agent posted a message, the
other collected it. It is kept for the engines that speak it, but it is no
longer a path of its own. A message sent here is an A2A task through the A2A
Gateway, held to exactly the rules an A2A client's message is held to -- the
recipient published and approved, a contract for the pair, the contract's data
ceiling, the hop limit, policy, the kill-switch -- and recorded in the same log.
One set of rules for every hand-off between agents, whichever door it used.

Still a mailbox, and deliberately not an orchestrator: the gateway records,
governs and delivers. It does not decide what happens next, chain steps
together or wake anything up. Section 40 puts multi-agent orchestration out of
scope, and this stays on the near side of that line on purpose.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from apps.core.constants import Environment


class MessageBlocked(Exception):
    """The gateway refused delivery. Recorded, not silently dropped."""

    def __init__(self, message: str, *, policies=None, task=None):
        super().__init__(message)
        self.message = message
        self.policies = policies or []
        self.task = task


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
    def send(cls, organization, request: MessageRequest, *, service_account=None):
        """Send one message as an A2A task, or record why it was refused. Returns the task."""
        from apps.a2a.models import A2AChannel
        from apps.a2a.services import A2AGateway, A2AGatewayError, SendRequest

        cls._same_tenant(organization, request)
        metadata = request.metadata if isinstance(request.metadata, dict) else {"value": request.metadata}
        message = {
            "kind": "message",
            "role": "user",
            "messageId": str(uuid.uuid4()),
            "parts": [{"kind": "text", "text": request.body}],
            "metadata": metadata,
        }
        if request.correlation_id:
            # The old thread id is A2A's context: the exchanges of one conversation.
            message["contextId"] = request.correlation_id[:200]
        try:
            outcome = A2AGateway.send(
                organization,
                SendRequest(
                    sender=request.sender, message=message, environment=request.environment,
                    channel=A2AChannel.MAILBOX, subject=request.subject,
                ),
                recipient=request.recipient,
                service_account=service_account,
            )
        except A2AGatewayError as exc:
            raise MessageBlocked(str(exc)) from None
        if not outcome.delivered:
            raise MessageBlocked(outcome.reason, policies=outcome.policies, task=outcome.task)
        return outcome.task

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

    @classmethod
    def collect(cls, organization, agent, *, limit: int = 50):
        """Hand the recipient its waiting messages. A message collected here is finished."""
        from apps.a2a.services import A2AGateway

        return A2AGateway.collect(organization, agent, limit=limit)
