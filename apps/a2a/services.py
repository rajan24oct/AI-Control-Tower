"""The A2A Gateway request path.

A message from one agent to another arrives here and passes through, in order:

    identity -> registry -> access -> content -> policy -> limits -> breaker -> deliver -> record

The gateway exists for the middle of that. An agent that calls another
directly can hand it anything -- including data the other was never cleared
to see -- and nobody can say afterwards who asked whom for what. Through the
gateway a message reaches only an agent governance published, for a skill
someone reviewed, under a contract that says who may ask, how often and with
how sensitive a payload; the work cannot be handed round in a circle or down
an endless chain; and the credential for an external agent stays with the
platform.

The rules the other gateways follow for being in the request path hold here:
every outbound call has a timeout, an agent that keeps failing is taken out of
the path by its breaker, and a refusal is recorded, never silently dropped.
Delivery to a mailbox *is* the record, so it is written or nothing was
delivered. Recording an exchange with an agent reached over HTTP is best
effort, because the Control Tower failing to write its own log must never lose
an answer the other agent already gave.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.a2a.content import Content, inspect
from apps.a2a.models import (
    TERMINAL_STATES,
    A2AAccessMode,
    A2AAgentCard,
    A2AChannel,
    A2AContract,
    A2ASkill,
    A2ATask,
    A2ATransport,
    DeliveryStatus,
    SkillStatus,
    TaskState,
    sensitivity,
)
from apps.a2a.wire import (
    CLASSIFICATION_KEYS,
    EXECUTION_KEYS,
    GATEWAY_KEY,
    PARENT_KEYS,
    SKILL_KEYS,
    MessageInvalid,
    first_reference,
    is_artifact,
    meta,
    validate_message,
)
from apps.core.constants import Environment, LifecycleStatus
from apps.mcp_gateway.services import ServerBreaker
from integrations.base.a2a import A2AProtocolError, A2ATaskResult
from integrations.base.exceptions import AdapterConfigurationError

logger = logging.getLogger(__name__)

#: The action name policies and violations see for an agent-to-agent message.
POLICY_ACTION = "a2a.message"

#: What a mailbox agent may report about a task it collected. Cancelling is the sender's.
ANSWER_STATES = (
    TaskState.WORKING, TaskState.INPUT_REQUIRED, TaskState.AUTH_REQUIRED,
    TaskState.COMPLETED, TaskState.FAILED, TaskState.REJECTED,
)


def setting(name: str, default):
    return settings.CONTROL_TOWER.get("A2A_GATEWAY", {}).get(name, default)


class UnknownRecipient(Exception):
    """The address names no agent the registry knows."""

    def __init__(self, address: str):
        super().__init__(f"Unknown agent: {address}")
        self.address = address


class TaskNotFound(Exception):
    """No such task -- or none this caller may see, which it cannot tell apart."""


class TaskNotCancelable(Exception):
    """The task is finished, or was never delivered."""


class A2AGatewayError(ValueError):
    """A request the gateway will not act on, for a reason the caller can fix."""


class AgentBreaker(ServerBreaker):
    """The MCP Gateway's breaker rules, held to the A2A Gateway's settings."""

    label = "A2A"

    @staticmethod
    def threshold() -> int:
        return int(setting("CIRCUIT_FAILURE_THRESHOLD", 5))

    @staticmethod
    def cool_off() -> int:
        return int(setting("CIRCUIT_COOL_OFF_SECONDS", 30))


@dataclass
class SendRequest:
    """One message from one agent to another, in A2A's own shape."""

    sender: object = None
    message: dict = field(default_factory=dict)
    skill: str = ""
    data_classification: str = ""
    environment: str = ""
    execution_id: str = ""
    parent_task_id: str = ""
    channel: str = A2AChannel.JSONRPC
    subject: str = ""
    configuration: dict = field(default_factory=dict)

    @classmethod
    def from_message(cls, message: dict, *, params_metadata: dict | None = None, **fields) -> "SendRequest":
        """Read what the gateway needs from the metadata A2A lets a message carry.

        A field given explicitly wins; the message's own metadata comes next, then
        the request's. A reference to an earlier task stands in for a parent.
        """
        extra = params_metadata or {}
        fields["skill"] = fields.get("skill") or meta(message, SKILL_KEYS, extra)
        fields["data_classification"] = fields.get("data_classification") or meta(message, CLASSIFICATION_KEYS, extra)
        fields["parent_task_id"] = (
            fields.get("parent_task_id") or meta(message, PARENT_KEYS, extra) or first_reference(message)
        )
        fields["execution_id"] = fields.get("execution_id") or meta(message, EXECUTION_KEYS, extra)
        return cls(message=message, **fields)


@dataclass
class Refusal:
    status: str
    reason: str
    policies: list[str] = field(default_factory=list)
    limit: str = ""


@dataclass
class SendOutcome:
    status: str
    card: A2AAgentCard | None
    task_id: uuid.UUID = field(default_factory=uuid.uuid4)
    #: The registry agent addressed, when it was never published.
    recipient_agent: object = None
    skill: A2ASkill | None = None
    contract: A2AContract | None = None
    content: Content | None = None
    parent: A2ATask | None = None
    hops: int = 1
    trail: list[str] = field(default_factory=list)
    #: A follow-up on a task waiting for the sender, rather than a new task.
    continued: A2ATask | None = None
    reason: str = ""
    policies: list[str] = field(default_factory=list)
    limit: str = ""
    latency_ms: int = 0
    dispatched: bool = False
    remote: A2ATaskResult | None = None
    task: A2ATask | None = None

    @property
    def delivered(self) -> bool:
        return self.status == DeliveryStatus.DELIVERED

    @property
    def state(self) -> str:
        """The A2A state the exchange ends the request in."""
        if self.status == DeliveryStatus.BLOCKED:
            return TaskState.REJECTED
        if self.status != DeliveryStatus.DELIVERED:
            return TaskState.FAILED
        if self.remote is not None:
            return self.remote.state if self.remote.state in TaskState.values else TaskState.UNKNOWN
        return TaskState.SUBMITTED

    @property
    def http_status(self) -> int:
        """For the REST surface: "you may not" and "nothing answered" differ."""
        return {
            DeliveryStatus.BLOCKED: 403,
            DeliveryStatus.UNAVAILABLE: 503,
            DeliveryStatus.FAILED: 502,
        }.get(self.status, 200)


@dataclass
class DirectoryEntry:
    """One agent a sender may reach, with the skills it may ask for."""

    card: A2AAgentCard
    skills: list[A2ASkill]
    contract: A2AContract | None

    def as_payload(self, *, url: str) -> dict:
        contract = self.contract
        return {
            "address": self.card.slug,
            "name": self.card.name,
            "description": self.card.description,
            "origin": self.card.origin,
            "url": url,
            "agent_card_url": f"{url}.well-known/agent-card.json",
            "clearance": self.card.data_classification,
            "skills": [skill.to_wire() for skill in self.skills],
            "contract": {
                "skills": contract.allowed_skills,
                "max_data_classification": contract.ceiling,
                "max_messages_per_hour": contract.max_messages_per_hour,
                "expires_at": contract.expires_at.isoformat() if contract.expires_at else None,
            } if contract else None,
        }


def _step(state: str, by: str, at=None) -> dict:
    return {"state": state, "at": (at or timezone.now()).isoformat(), "by": by}


def _bounded(value, limit: int):
    """Content as the log keeps it: whole if small, a truncated preview if not."""
    encoded = json.dumps(value, default=str)
    if len(encoded) <= limit:
        return value
    return {"_truncated": True, "preview": encoded[:limit]}


def _capped_artifacts(artifacts, limit_bytes: int) -> list:
    """An answer as kept for the sender: whole, up to the gateway's message size."""
    encoded = json.dumps(artifacts or [], default=str)
    if len(encoded.encode("utf-8")) <= limit_bytes:
        return artifacts or []
    return [{
        "artifactId": "truncated",
        "name": "truncated",
        "parts": [{"kind": "text", "text": encoded[: limit_bytes // 2]}],
        "metadata": {GATEWAY_KEY: {"truncated": True}},
    }]


class A2AGateway:
    """Delivers agent-to-agent messages from the registry, and follows the tasks they start."""

    # -- Addressing ---------------------------------------------------------

    @staticmethod
    def resolve(organization, address) -> A2AAgentCard:
        """The card an address names: its gateway address, its agent's agent_id, or its UUID."""
        from apps.agents.models import AIAgent

        cards = A2AAgentCard.objects.for_organization(organization).select_related("agent")
        if isinstance(address, A2AAgentCard):
            return address
        if isinstance(address, AIAgent):
            card = cards.filter(agent=address).first()
            if card is None:
                raise UnknownRecipient(address.agent_id)
            return card
        value = str(address or "").strip()
        card = None
        try:
            card = cards.filter(pk=uuid.UUID(value)).first()
        except ValueError:
            pass
        card = card or cards.filter(slug=value).first() or cards.filter(agent__agent_id=value).first()
        if card is None or not value:
            raise UnknownRecipient(value)
        return card

    @staticmethod
    def _environment(agent, environment: str) -> str:
        return environment or getattr(agent, "environment", "") or Environment.PRODUCTION

    # -- Directory ----------------------------------------------------------

    @classmethod
    def directory(cls, organization, sender, *, environment: str = "") -> list[DirectoryEntry]:
        """Every agent this sender may reach right now, and the skills it may ask each for.

        Answered from the registry, never from the agents themselves. Agents a
        deny policy puts out of reach are left out, so a model is not offered a
        colleague it will be refused the moment it tries.
        """
        if organization is None or sender is None:
            return []
        cards = [
            card
            for card in A2AAgentCard.objects.for_organization(organization)
            .filter(status=LifecycleStatus.ACTIVE, is_approved=True)
            .select_related("agent")
            if card.is_gateway_reachable and card.agent_id != sender.pk and not cls._registry_denial(card)
        ]
        contracts = {
            contract.recipient_id: contract
            for contract in A2AContract.objects.filter(sender=sender, recipient__in=cards).select_related("recipient")
        }
        denied = cls._policy_filter(organization, sender, cls._environment(sender, environment))
        skills: dict = {}
        for skill in A2ASkill.objects.filter(card__in=cards, status=SkillStatus.ENABLED).order_by("skill_id"):
            skills.setdefault(skill.card_id, []).append(skill)

        entries = []
        for card in cards:
            contract = contracts.get(card.pk)
            if contract is None and card.access_mode != A2AAccessMode.OPEN:
                continue
            if contract is not None and contract.is_expired:
                continue
            if denied(card):
                continue
            offered = [skill for skill in skills.get(card.pk, []) if contract is None or contract.permits(skill.skill_id)]
            entries.append(DirectoryEntry(card, offered, contract))
        return entries

    @staticmethod
    def _policy_filter(organization, sender, environment):
        """A predicate for agents a deny policy forbids this sender to reach outright.

        Runs the policy engine's own agent-communication evaluator over the deny
        policies, fetched once -- so the directory and the send agree, IRGF tier
        scoping and M17 exceptions included.
        """
        from apps.policies.models import PolicyEffect, PolicyType
        from apps.policies.services import (
            EVALUATORS,
            PolicyContext,
            PolicyEngine,
            excepted_policy_ids,
            in_tier_scope,
        )

        tier = getattr(sender, "risk_tier", None)
        excepted = excepted_policy_ids(organization, sender)
        policies = [
            policy
            for policy in PolicyEngine.policies_for(organization).filter(
                policy_type=PolicyType.AGENT_COMMUNICATION, effect=PolicyEffect.DENY
            )
            if in_tier_scope(policy, tier) and policy.pk not in excepted
        ]
        evaluator = EVALUATORS[PolicyType.AGENT_COMMUNICATION]

        def denied(card) -> bool:
            context = PolicyContext(agent=sender, action=POLICY_ACTION, environment=environment, recipient=card)
            for policy in policies:
                try:
                    if evaluator(policy, context):
                        return True
                except Exception:
                    logger.exception("Policy %s raised while filtering the A2A directory", policy.pk)
            return False

        return denied

    # -- Sending ------------------------------------------------------------

    @classmethod
    def send(cls, organization, request: SendRequest, *, recipient, service_account=None) -> SendOutcome:
        """Deliver one message, or record why it was refused.

        Raises UnknownRecipient only for an address the registry has never seen.
        A registry agent that was never published is answered and recorded as
        blocked: "an agent tried to reach one nobody published" is exactly what
        governance wants to be able to see.
        """
        from apps.agents.models import AIAgent

        try:
            card = cls.resolve(organization, recipient)
            addressed = card.agent
        except UnknownRecipient:
            if not isinstance(recipient, AIAgent):
                raise
            card, addressed = None, recipient
        cls._same_tenant(organization, request.sender, card, addressed)

        outcome = SendOutcome(DeliveryStatus.DELIVERED, card, recipient_agent=addressed)
        refusal = cls._identity(request.sender)
        if refusal is None and card is None:
            refusal = Refusal(
                DeliveryStatus.BLOCKED,
                f"Agent '{addressed.name}' is not published to the A2A Gateway, so no agent can send it work.",
            )
        if refusal is None:
            outcome.continued = cls._continuation(organization, request, card)
            refusal = cls._gate(organization, card, request, outcome)

        if refusal is not None:
            outcome.status, outcome.reason = refusal.status, refusal.reason[:1000]
            outcome.policies, outcome.limit = refusal.policies, refusal.limit
        else:
            cls._deliver(card, request, outcome)
        if outcome.content is None:
            # The facts are recorded even for a message refused before they were needed.
            outcome.content = inspect(
                request.message, declared=request.data_classification,
                fallback=getattr(request.sender, "data_classification", ""),
            )
        cls._record(organization, request, outcome, service_account)
        return outcome

    @staticmethod
    def _same_tenant(organization, sender, card, addressed) -> None:
        """Both ends must belong to the caller's organization.

        Checked here rather than trusted to the lookups that found them, because
        a hand-off that crosses tenants is a data breach, not a display glitch.
        Nothing is recorded: the record itself would cross the boundary.
        """
        for party in (sender, card, addressed):
            if party is not None and party.organization_id != getattr(organization, "pk", None):
                raise A2AGatewayError("Unknown sender or recipient.")

    @staticmethod
    def _identity(sender) -> Refusal | None:
        """The sending agent: named, and free to act. The kill-switch is checked first."""
        if sender is None:
            return Refusal(
                DeliveryStatus.BLOCKED,
                "Agent-to-agent traffic needs the agent sending it: name it, or call with a key bound to it.",
            )
        if sender.is_suspended:
            return Refusal(
                DeliveryStatus.BLOCKED,
                f"Agent '{sender.name}' is suspended by its kill-switch and cannot send: "
                f"{sender.suspension_reason or 'no reason recorded'}.",
            )
        if not sender.is_active:
            return Refusal(DeliveryStatus.BLOCKED, f"Agent '{sender.name}' is {sender.get_status_display().lower()}.")
        return None

    @staticmethod
    def _continuation(organization, request, card) -> A2ATask | None:
        """A follow-up on a task waiting for the sender, named by the message's taskId."""
        task_id = request.message.get("taskId") if isinstance(request.message, dict) else None
        if not task_id:
            return None
        try:
            task = A2ATask.objects.for_organization(organization).select_related("parent").filter(
                pk=uuid.UUID(str(task_id))
            ).first()
        except ValueError:
            task = None
        if task is None or task.recipient_id != card.pk or task.sender_id != getattr(request.sender, "pk", None):
            raise TaskNotFound(str(task_id))
        if not task.awaits_sender:
            raise A2AGatewayError(
                f"Task {task.pk} is {task.get_state_display().lower()}; only a task waiting for "
                "the sender takes a follow-up message."
            )
        return task

    @classmethod
    def _gate(cls, organization, card, request, outcome) -> Refusal | None:
        """The first refusal that applies, or None if the message may go."""
        sender = request.sender
        blocked = DeliveryStatus.BLOCKED

        reason = cls._registry_denial(card)
        if reason:
            return Refusal(blocked, reason)

        skill_id = (request.skill or "").strip()[:128]
        if skill_id:
            outcome.skill = card.skills.filter(skill_id=skill_id).first()
            reason = cls._skill_denial(card, outcome.skill, skill_id)
            if reason:
                return Refusal(blocked, reason)

        outcome.contract = A2AContract.objects.filter(sender=sender, recipient=card).select_related("recipient").first()
        reason = cls._access_denial(card, sender, outcome.contract, skill_id)
        if reason:
            return Refusal(blocked, reason)

        outcome.content = inspect(
            request.message, declared=request.data_classification, fallback=sender.data_classification
        )
        refusal = cls._content_denial(card, outcome.contract, outcome.content)
        if refusal is not None:
            return refusal

        cls._chain(organization, request, outcome)
        decision = cls._policy(organization, request, card, outcome)
        if not decision.allowed:
            return Refusal(blocked, decision.reason or "Blocked by policy.", decision.policy_names)
        if decision.requires_approval:
            # Nobody can wait on a human in the middle of a hand-off: the
            # sender's turn would hang. Requiring approval means refusing, with
            # the reason, so the run can raise an approval request and return.
            return Refusal(
                blocked,
                decision.reason or "This message requires human approval, which a hand-off cannot await.",
                decision.policy_names,
            )

        if outcome.continued is None:
            refusal = cls._chain_denial(card, outcome) or cls._rate_denial(card, sender, outcome.contract)
            if refusal is not None:
                return refusal

        if not card.uses_mailbox and not AgentBreaker.available(card):
            return Refusal(
                DeliveryStatus.UNAVAILABLE,
                f"'{card.name}' was taken out of rotation after repeated failures; "
                "a probe is let through after the cool-off.",
            )
        return None

    @staticmethod
    def _registry_denial(card) -> str:
        if not card.is_active:
            return f"'{card.name}' is {card.get_status_display().lower()} in the A2A registry."
        if not card.is_approved:
            return f"'{card.name}' has not been approved for agent-to-agent traffic."
        if not card.is_gateway_reachable:
            return f"'{card.name}' uses {card.get_transport_display()}, which the gateway cannot reach."
        agent = card.agent
        if agent is not None:
            if agent.is_suspended:
                return f"Agent '{agent.name}' is suspended by its kill-switch and cannot take work."
            if not agent.is_active:
                return f"Agent '{agent.name}' is {agent.get_status_display().lower()}."
        return ""

    @staticmethod
    def _skill_denial(card, skill, skill_id: str) -> str:
        if skill is None:
            return f"'{card.name}' offers no skill '{skill_id}'."
        if skill.status == SkillStatus.PENDING_REVIEW:
            if skill.changed_since_review:
                return (
                    f"Skill '{skill_id}' changed on '{card.name}' after it was reviewed, "
                    "and is awaiting review again."
                )
            return f"Skill '{skill_id}' of '{card.name}' is awaiting governance review."
        if skill.status == SkillStatus.DISABLED:
            return f"Skill '{skill_id}' of '{card.name}' has been disabled by governance."
        if skill.status == SkillStatus.REMOVED:
            return f"'{card.name}' no longer offers '{skill_id}'."
        return ""

    @staticmethod
    def _access_denial(card, sender, contract, skill_id: str) -> str:
        if card.agent_id and card.agent_id == sender.pk:
            return "An agent does not hand work to itself through the gateway."
        if contract is None:
            if card.access_mode == A2AAccessMode.OPEN:
                return ""
            return f"Agent '{sender.name}' holds no contract to send work to '{card.name}'."
        if contract.is_expired:
            return (
                f"The contract for '{sender.name}' to reach '{card.name}' expired on "
                f"{timezone.localtime(contract.expires_at):%d %b %Y}."
            )
        if skill_id and not contract.permits(skill_id):
            return f"The contract for '{sender.name}' to reach '{card.name}' does not include '{skill_id}'."
        if not skill_id and contract.allowed_skills:
            return (
                f"The contract for '{sender.name}' to reach '{card.name}' covers only "
                f"{', '.join(contract.allowed_skills)}: name the skill this message is for."
            )
        return ""

    @staticmethod
    def _content_denial(card, contract, content: Content) -> Refusal | None:
        limit = int(setting("MAX_MESSAGE_BYTES", 256 * 1024))
        if content.size > limit:
            return Refusal(
                DeliveryStatus.BLOCKED,
                f"The message is {content.size:,} bytes; the gateway takes at most {limit:,}.",
                limit="size",
            )
        ceiling = contract.ceiling if contract is not None else card.data_classification
        if sensitivity(content.classification) > sensitivity(ceiling):
            found = f" (personal data found: {content.personal_data_labels})" if content.raised else ""
            where = (
                "this contract allows"
                if contract is not None and ceiling != card.data_classification
                else "it is cleared for"
            )
            return Refusal(
                DeliveryStatus.BLOCKED,
                f"{content.classification.capitalize()} data{found} may not be sent to '{card.name}': "
                f"{where} {ceiling} data at most.",
                limit="data_classification",
            )
        return None

    @staticmethod
    def _chain(organization, request, outcome) -> None:
        """Where this message sits in a chain of hand-offs."""
        sender = request.sender
        if outcome.continued is not None:
            task = outcome.continued
            outcome.parent, outcome.hops, outcome.trail = task.parent, task.hops, list(task.trail or [])
            return
        parent = None
        if request.parent_task_id:
            try:
                parent = A2ATask.objects.for_organization(organization).filter(
                    pk=uuid.UUID(str(request.parent_task_id))
                ).first()
            except ValueError:
                parent = None
        # Only work the sender was given counts: it is handing that work on.
        if parent is not None and parent.recipient_agent_id == sender.pk and parent.reached_recipient:
            outcome.parent = parent
            outcome.trail = [*(parent.trail or []), sender.agent_id]
        else:
            outcome.trail = [sender.agent_id]
        outcome.hops = len(outcome.trail)

    @staticmethod
    def _chain_denial(card, outcome) -> Refusal | None:
        if card.address in outcome.trail:
            path = " → ".join([*outcome.trail, card.address])
            return Refusal(
                DeliveryStatus.BLOCKED,
                f"The work would come back to '{card.name}', which already handled it: {path}.",
                limit="loop",
            )
        maximum = int(setting("MAX_HOPS", 4))
        if outcome.hops > maximum:
            path = " → ".join([*outcome.trail, card.address])
            return Refusal(
                DeliveryStatus.BLOCKED,
                f"Work may be handed on {maximum} times at most; this would be hand-off {outcome.hops} ({path}).",
                limit="hops",
            )
        return None

    @staticmethod
    def _rate_denial(card, sender, contract) -> Refusal | None:
        if contract is None or not contract.max_messages_per_hour:
            return None
        allowed = contract.max_messages_per_hour
        sent = A2ATask.objects.filter(
            contract=contract, status=DeliveryStatus.DELIVERED, created_at__gte=timezone.now() - timedelta(hours=1)
        ).count()
        if sent < allowed:
            return None
        return Refusal(
            DeliveryStatus.BLOCKED,
            f"The contract for '{sender.name}' to reach '{card.name}' allows {allowed} message"
            f"{'s' if allowed != 1 else ''} an hour, and {sent} went in the last hour.",
            limit="rate",
        )

    @classmethod
    def _policy(cls, organization, request, card, outcome):
        from apps.policies.services import PolicyContext, PolicyEngine

        context = PolicyContext(
            agent=request.sender,
            action=POLICY_ACTION,
            tool=f"a2a:{card.slug}",
            data_classification=outcome.content.classification,
            environment=cls._environment(request.sender, request.environment),
            estimated_cost=Decimal("0"),
            recipient=card,
            hops=outcome.hops,
        )
        # Agent-level policies only: no model serves a message, so gating one on
        # the sender's declared model would check something unrelated.
        return PolicyEngine.check_agent(organization, context, requested_action=POLICY_ACTION)

    # -- Delivering ---------------------------------------------------------

    @classmethod
    def _deliver(cls, card, request, outcome) -> None:
        if card.uses_mailbox:
            return  # queued as it is recorded; the agent collects it
        started = time.monotonic()
        try:
            adapter = card.get_adapter(timeout=float(setting("CALL_TIMEOUT_SECONDS", 30)))
            result = adapter.send_message(cls._outbound(request, outcome), configuration=cls._configuration(request))
        except A2AProtocolError as exc:
            # The agent answered, so it is up -- it refused this request. That is
            # no reason to take it out of rotation for everyone else.
            AgentBreaker.record_success(card)
            outcome.status = DeliveryStatus.FAILED
            outcome.reason = f"'{card.name}' returned error {exc.code}: {exc.message}"[:1000]
        except AdapterConfigurationError as exc:
            # Our configuration, not their outage: a missing credential must not
            # trip the breaker and hide a healthy agent from everyone.
            outcome.status = DeliveryStatus.UNAVAILABLE
            outcome.reason = str(exc)[:1000]
        except Exception as exc:  # unreachable, timed out, broke mid-response
            error = f"{type(exc).__name__}: {exc}"[:500]
            AgentBreaker.record_failure(card, error)
            logger.warning("A2A delivery to %s failed: %s", card.name, error)
            outcome.status = DeliveryStatus.FAILED
            outcome.reason = error
        else:
            AgentBreaker.record_success(card)
            outcome.remote = result
        outcome.dispatched = True
        outcome.latency_ms = int((time.monotonic() - started) * 1000)

    @staticmethod
    def _outbound(request, outcome) -> dict:
        """The message as the other agent receives it: annotated by the gateway, never rewritten."""
        message = json.loads(json.dumps(request.message, default=str))
        metadata = dict(message.get("metadata") or {})
        if request.skill:
            metadata.setdefault(SKILL_KEYS[0], request.skill)
        metadata[GATEWAY_KEY] = {
            "from": request.sender.agent_id,
            "task": str(outcome.task_id),
            "hops": outcome.hops,
            "dataClassification": outcome.content.classification,
        }
        message["metadata"] = metadata
        # The other agent's task and context ids are its own; the gateway's mean nothing to it.
        message.pop("taskId", None)
        message.pop("contextId", None)
        continued = outcome.continued
        if continued is not None:
            if continued.remote_task_id:
                message["taskId"] = continued.remote_task_id
            if continued.remote_context_id:
                message["contextId"] = continued.remote_context_id
        return message

    @staticmethod
    def _configuration(request) -> dict:
        # The gateway answers the sender when the call returns; it offers no push.
        wanted = {key: request.configuration[key] for key in ("acceptedOutputModes", "historyLength")
                  if key in request.configuration}
        return {**wanted, "blocking": True}

    # -- Recording ----------------------------------------------------------

    @classmethod
    def _record(cls, organization, request, outcome, service_account) -> None:
        mailbox = outcome.card is not None and outcome.card.uses_mailbox and outcome.delivered
        try:
            with transaction.atomic():
                if outcome.continued is not None and outcome.delivered:
                    outcome.task = cls._write_turn(request, outcome)
                else:
                    outcome.task = cls._write(organization, request, outcome, service_account)
        except Exception:
            logger.exception("A2A Gateway could not record a message to %s", outcome.card or outcome.recipient_agent)
            if mailbox:
                # The mailbox is the record: if it could not be written, nothing was delivered.
                outcome.status = DeliveryStatus.FAILED
                outcome.reason = "The gateway could not queue the message; nothing was delivered."
                outcome.task = None

    @staticmethod
    def _keeps_content(card, outcome) -> bool:
        """A mailbox message is the delivery itself; anything else is only the log."""
        mailbox = card is not None and card.uses_mailbox and outcome.delivered
        return mailbox or bool(setting("RECORD_CONTENT", True))

    @classmethod
    def _write(cls, organization, request, outcome, service_account) -> A2ATask:
        card = outcome.card
        content = outcome.content or Content()
        limit = int(setting("MAX_RECORDED_CHARS", 2000))
        mailbox = card is not None and card.uses_mailbox and outcome.delivered
        record = cls._keeps_content(card, outcome)
        remote = outcome.remote
        now = timezone.now()
        state = outcome.state
        recipient_agent = card.agent if card is not None and card.agent_id else outcome.recipient_agent
        sender = request.sender

        task = A2ATask.objects.create(
            id=outcome.task_id,
            organization=organization,
            sender=sender,
            recipient=card,
            recipient_agent=recipient_agent,
            recipient_name=(card.address if card is not None else recipient_agent.agent_id)[:200],
            skill=outcome.skill,
            skill_name=(request.skill or "")[:128],
            contract=outcome.contract,
            service_account=service_account,
            execution=cls._execution(organization, request.execution_id),
            channel=request.channel,
            context_id=cls._context(request, outcome),
            parent=outcome.parent,
            hops=outcome.hops,
            trail=outcome.trail or ([sender.agent_id] if sender is not None else []),
            status=outcome.status,
            state=state,
            reason=outcome.reason[:1000],
            policies=outcome.policies,
            limit_breached=outcome.limit,
            latency_ms=outcome.latency_ms if outcome.dispatched else None,
            data_classification=content.classification,
            personal_data=content.personal_data_kinds,
            subject=(request.subject or "")[:255],
            message=(request.message if mailbox else _bounded(request.message, limit)) if record else {},
            text_preview=content.text[:limit] if record else "",
            status_message=remote.status_message if remote is not None and record else None,
            artifacts=_capped_artifacts(remote.artifacts, int(setting("MAX_MESSAGE_BYTES", 256 * 1024)))
            if remote is not None and record else [],
            history=[_step(state, "gateway" if not outcome.delivered or mailbox else (card.address if card else ""), now)],
            remote_task_id=(remote.task_id if remote is not None else "")[:200],
            remote_context_id=(remote.context_id if remote is not None else "")[:200],
            delivered_at=now if outcome.delivered else None,
            completed_at=now if outcome.delivered and state in TERMINAL_STATES else None,
        )
        if task.execution_id:
            cls._append_to_run(task)
        return task

    @classmethod
    def _write_turn(cls, request, outcome) -> A2ATask:
        """A follow-up on a task waiting for the sender: the same task, one more turn."""
        task = outcome.continued
        content = outcome.content
        limit = int(setting("MAX_RECORDED_CHARS", 2000))
        record = cls._keeps_content(task.recipient, outcome)
        now = timezone.now()
        task.turns += 1
        if record:
            task.message = request.message if task.recipient.uses_mailbox else _bounded(request.message, limit)
            task.text_preview = content.text[:limit]
        if sensitivity(content.classification) > sensitivity(task.data_classification):
            task.data_classification = content.classification
        task.personal_data = sorted(set(task.personal_data or []) | set(content.personal_data_kinds))
        if outcome.remote is not None:
            cls._apply(task, outcome.remote, by=task.recipient_name, keep=record)
        else:
            task.state = TaskState.SUBMITTED
            task.collected_at = None
            task.history = [*task.history, _step(TaskState.SUBMITTED, request.sender.agent_id, now)]
        task.save()
        return task

    @staticmethod
    def _context(request, outcome) -> str:
        supplied = request.message.get("contextId") if isinstance(request.message, dict) else ""
        if isinstance(supplied, str) and supplied.strip():
            return supplied.strip()[:200]
        if outcome.parent is not None and outcome.parent.context_id:
            return outcome.parent.context_id
        return str(uuid.uuid4())

    @staticmethod
    def _execution(organization, execution_id: str):
        if not execution_id:
            return None
        from apps.executions.models import Execution

        return Execution.objects.for_organization(organization).filter(external_id=execution_id[:200]).first()

    @staticmethod
    def _append_to_run(task) -> None:
        """Put the hand-off on the sender's run timeline, under the normal event vocabulary.

        Message content stays in the task, where its display is gated; the
        timeline carries only what happened.
        """
        from apps.executions.services import ExecutionService

        ExecutionService.append_events(
            task.execution,
            [
                {
                    "event_type": "AGENT_STEP_COMPLETED" if task.reached_recipient else "ERROR",
                    "status": task.status,
                    "error_message": "" if task.reached_recipient else task.reason,
                    "metadata": {
                        "a2a_gateway": True,
                        "a2a_task_id": str(task.pk),
                        "recipient": task.recipient_name,
                        "skill": task.skill_name,
                        "hops": task.hops,
                    },
                }
            ],
        )

    @staticmethod
    def _apply(task, result: A2ATaskResult, *, by: str, keep: bool = True) -> None:
        """What the other agent said about the task, onto the record."""
        state = result.state if result.state in TaskState.values else TaskState.UNKNOWN
        now = timezone.now()
        if state != task.state:
            task.history = [*task.history, _step(state, by, now)]
        task.state = state
        if keep:
            if result.status_message is not None:
                task.status_message = result.status_message
            if result.artifacts:
                task.artifacts = _capped_artifacts(result.artifacts, int(setting("MAX_MESSAGE_BYTES", 256 * 1024)))
        if result.task_id:
            task.remote_task_id = result.task_id[:200]
        if result.context_id:
            task.remote_context_id = result.context_id[:200]
        if state in TERMINAL_STATES and task.completed_at is None:
            task.completed_at = now

    # -- Following a task ---------------------------------------------------

    @staticmethod
    def _find(organization, task_id) -> A2ATask:
        try:
            pk = uuid.UUID(str(task_id))
        except ValueError:
            raise TaskNotFound(str(task_id)) from None
        task = A2ATask.objects.for_organization(organization).select_related(
            "sender", "recipient", "recipient_agent", "skill"
        ).filter(pk=pk).first()
        if task is None:
            raise TaskNotFound(str(task_id))
        return task

    @classmethod
    def get(cls, organization, task_id, *, agent) -> A2ATask:
        """A task, for the agent that sent it or the one that received it.

        Nobody else can tell it exists: a task names who asked whom for what,
        which is not every agent's business.
        """
        task = cls._find(organization, task_id)
        if agent is None or agent.pk not in {task.sender_id, task.recipient_agent_id}:
            raise TaskNotFound(str(task_id))
        if agent.pk == task.sender_id:
            cls._refresh(task)
        return task

    @classmethod
    def _refresh(cls, task) -> None:
        """Ask an agent reached over HTTP where an unfinished task stands. Best effort."""
        card = task.recipient
        if (card is None or card.uses_mailbox or not task.reached_recipient or task.is_terminal
                or not task.remote_task_id or not AgentBreaker.available(card)):
            return
        try:
            result = card.get_adapter(timeout=float(setting("CALL_TIMEOUT_SECONDS", 30))).get_task(task.remote_task_id)
        except A2AProtocolError:
            AgentBreaker.record_success(card)
            return
        except AdapterConfigurationError:
            return
        except Exception as exc:
            AgentBreaker.record_failure(card, f"{type(exc).__name__}: {exc}"[:500])
            return
        AgentBreaker.record_success(card)
        cls._apply(task, result, by=card.address, keep=bool(setting("RECORD_CONTENT", True)))
        task.save()

    @classmethod
    def cancel(cls, organization, task_id, *, agent) -> A2ATask:
        """The sender withdraws a task it no longer wants done."""
        task = cls._find(organization, task_id)
        if agent is None or agent.pk != task.sender_id:
            raise TaskNotFound(str(task_id))
        if not task.reached_recipient or task.is_terminal:
            raise TaskNotCancelable(f"Task {task.pk} is {task.get_state_display().lower()}.")
        card = task.recipient
        if card is not None and not card.uses_mailbox and task.remote_task_id:
            try:
                result = card.get_adapter(timeout=float(setting("CALL_TIMEOUT_SECONDS", 30))).cancel_task(
                    task.remote_task_id
                )
            except A2AProtocolError as exc:
                raise TaskNotCancelable(f"'{card.name}' would not cancel it: {exc.message}") from exc
            except Exception as exc:
                raise A2AGatewayError(f"'{card.name}' could not be reached to cancel it: {exc}") from exc
            cls._apply(task, result, by=card.address)
        else:
            now = timezone.now()
            task.state = TaskState.CANCELED
            task.completed_at = now
            task.history = [*task.history, _step(TaskState.CANCELED, agent.agent_id, now)]
        task.save()
        return task

    # -- The mailbox --------------------------------------------------------

    @staticmethod
    def inbox(organization, agent):
        return (
            A2ATask.objects.for_organization(organization)
            .filter(
                recipient_agent=agent, status=DeliveryStatus.DELIVERED, state=TaskState.SUBMITTED,
                recipient__transport=A2ATransport.MAILBOX,
            )
            .select_related("sender", "recipient", "skill")
            .order_by("created_at")
        )

    @classmethod
    def collect(cls, organization, agent, *, limit: int = 50) -> list[A2ATask]:
        """Hand an agent the tasks waiting in its mailbox.

        Delivery is a pull, not a push: the gateway never calls into a mailbox
        agent, so one that is down cannot make the gateway hang, and a task
        cannot be lost because nothing was listening. The agent answers each
        with `respond`.
        """
        tasks = list(cls.inbox(organization, agent)[:limit])
        now = timezone.now()
        for task in tasks:
            # A message through the old agent-messaging endpoints had no answer
            # to wait for: collecting it is the end of it.
            state = TaskState.COMPLETED if task.channel == A2AChannel.MAILBOX else TaskState.WORKING
            task.state = state
            task.collected_at = now
            task.updated_at = now
            task.history = [*task.history, _step(state, agent.agent_id, now)]
            if state in TERMINAL_STATES:
                task.completed_at = now
        if tasks:
            A2ATask.objects.bulk_update(tasks, ["state", "collected_at", "history", "completed_at", "updated_at"])
        return tasks

    @classmethod
    def respond(cls, organization, task_id, *, agent, state: str, message=None, artifacts=None) -> A2ATask:
        """A mailbox agent reports progress on a task it collected, or gives its answer."""
        task = cls._find(organization, task_id)
        if agent is None or task.recipient_agent_id != agent.pk:
            raise TaskNotFound(str(task_id))
        if not task.reached_recipient or task.recipient is None or not task.recipient.uses_mailbox:
            raise A2AGatewayError("Only a task delivered to this agent's mailbox takes an answer here.")
        if task.is_terminal:
            raise A2AGatewayError(f"Task {task.pk} is already {task.get_state_display().lower()}.")
        if state not in ANSWER_STATES:
            raise A2AGatewayError(f"state is one of: {', '.join(ANSWER_STATES)}.")
        if message is not None:
            try:
                validate_message({**message, "role": "user", "messageId": message.get("messageId") or "answer"})
            except MessageInvalid as exc:
                raise A2AGatewayError(f"message: {exc}") from None
        artifacts = artifacts or []
        if not isinstance(artifacts, list) or not all(is_artifact(item) for item in artifacts):
            raise A2AGatewayError("artifacts is a list of A2A artifacts, each with parts.")
        limit = int(setting("MAX_MESSAGE_BYTES", 256 * 1024))
        if len(json.dumps({"message": message, "artifacts": artifacts}, default=str).encode("utf-8")) > limit:
            raise A2AGatewayError(f"The answer is larger than the gateway takes ({limit:,} bytes).")

        now = timezone.now()
        if message is not None:
            task.status_message = {
                **message, "kind": "message", "role": "agent",
                "messageId": message.get("messageId") or f"{task.pk}-{len(task.history)}",
                "taskId": str(task.pk), "contextId": task.context_id,
            }
        if artifacts:
            task.artifacts = [*(task.artifacts or []), *artifacts]
        if state != task.state:
            task.history = [*task.history, _step(state, agent.agent_id, now)]
        task.state = state
        if state in TERMINAL_STATES:
            task.completed_at = now
        task.save()
        return task

    # -- Read models ----------------------------------------------------------

    @staticmethod
    def agent_summary(agent) -> dict:
        """An agent's place in the communication graph, for its page."""
        card = A2AAgentCard.objects.filter(agent=agent).first()
        tasks = A2ATask.objects.filter(Q(sender=agent) | Q(recipient_agent=agent))
        since = timezone.now() - timedelta(hours=24)
        return {
            "card": card,
            "inbound": list(card.contracts.select_related("sender")) if card else [],
            "outbound": list(agent.a2a_contracts.select_related("recipient")),
            "recent": tasks.select_related("sender", "recipient").order_by("-created_at")[:6],
            "sent_24h": tasks.filter(sender=agent, created_at__gte=since).count(),
            "blocked_24h": tasks.filter(sender=agent, created_at__gte=since, status=DeliveryStatus.BLOCKED).count(),
        }
