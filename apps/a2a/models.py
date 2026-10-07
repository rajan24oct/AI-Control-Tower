"""A2A Gateway state: who may hand work to whom, and what they sent.

Agent2Agent communication has the same two halves the MCP Gateway has. The
*registry* -- the agents published for A2A, the skills their cards advertise,
and the contracts that let one agent send work to another -- is what an
enterprise governs in one place. The *gateway* delivers from that registry: a
message reaches an agent only once governance approved its card, reviewed the
skill it asks for, and the sender holds a contract that covers it and the data
it carries.

Every exchange is kept as an A2A task, the refused ones included: "which agent
tried to hand which other agent what, and why was it stopped" is the question
this module exists to be able to answer.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from apps.core.constants import DataClassification, HealthStatus, RiskLevel
from apps.core.models import OrganizationOwnedModel, OrganizationScopedModel
from apps.gateway.models import CircuitState
from integrations.base.a2a import parts_text
from integrations.base.config import AdapterConfig
from integrations.registry import AdapterKind, registry

#: Data classifications in order of sensitivity, least first.
SENSITIVITY = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}


def sensitivity(value: str) -> int:
    return SENSITIVITY.get(value or "", -1)


def least_sensitive(*values: str) -> str:
    """The lowest of several classifications: a ceiling is only as high as its lowest part."""
    present = [value for value in values if value in SENSITIVITY]
    return min(present, key=sensitivity) if present else ""


class CardOrigin(models.TextChoices):
    INTERNAL = "internal", "An agent in this registry"
    EXTERNAL = "external", "An external agent"


class A2ATransport(models.TextChoices):
    """How the gateway delivers to an agent. The HTTP values double as adapter keys.

    The mailbox is the platform's own: the gateway holds the task and the agent
    collects it, so an agent needs no endpoint to take part, and one that is
    down cannot make a sender wait. An agent that serves A2A itself is reached
    at its endpoint instead.
    """

    MAILBOX = "mailbox", "Gateway mailbox (the agent collects)"
    JSONRPC = "jsonrpc", "A2A JSON-RPC over HTTP"
    MOCK = "mock", "Mock / Demo"


class A2AAuthMethod(models.TextChoices):
    NONE = "none", "None"
    BEARER = "bearer", "Bearer token"
    HEADER = "header", "API key header"


class A2AAccessMode(models.TextChoices):
    OPEN = "open", "Any agent in the organization"
    RESTRICTED = "restricted", "Only agents with a contract"


#: What an agent takes and gives when its card does not say.
DEFAULT_MODES = ["text/plain", "application/json"]


class A2AAgentCard(OrganizationScopedModel):
    """An agent the organization's agents may send work to, as the gateway publishes it.

    An internal agent's card is written by the platform from the registry and
    the skills its owner declares; an external agent's is read from where it
    publishes it. Either way governance approves the card and reviews each skill
    before anything reaches the agent, and the card other agents are shown is
    the reviewed one, never whatever the agent last said about itself.

    Credentials are never stored here. As with MCP servers, `credential_ref`
    names an environment variable the gateway resolves at call time, so no
    agent ever holds the credential for another.
    """

    name = models.CharField(max_length=200)
    slug = models.SlugField(
        max_length=200, blank=True,
        help_text="The agent's address at the gateway: /api/v1/a2a/<address>/.",
    )
    description = models.TextField(blank=True)
    origin = models.CharField(
        max_length=20, choices=CardOrigin.choices, default=CardOrigin.INTERNAL, db_index=True
    )
    agent = models.OneToOneField(
        "agents.AIAgent", null=True, blank=True, on_delete=models.CASCADE, related_name="a2a_card",
        help_text="The registry agent this card publishes. Empty for an external agent.",
    )
    publisher = models.CharField(
        max_length=200, blank=True, help_text="Who runs the agent, for an external one."
    )
    documentation_url = models.URLField(max_length=500, blank=True)

    transport = models.CharField(
        max_length=20, choices=A2ATransport.choices, default=A2ATransport.MAILBOX, db_index=True
    )
    endpoint = models.URLField(max_length=500, blank=True, help_text="Where the gateway sends A2A requests.")
    card_url = models.URLField(
        max_length=500, blank=True,
        help_text="Where the agent publishes its Agent Card. Blank: its well-known path.",
    )
    auth_method = models.CharField(
        max_length=20, choices=A2AAuthMethod.choices, default=A2AAuthMethod.NONE
    )
    credential_ref = models.CharField(
        max_length=200, blank=True,
        help_text=(
            "Name of the environment variable or secret-manager key holding the "
            "credential. Never paste the credential itself."
        ),
    )
    auth_header = models.CharField(
        max_length=100, blank=True,
        help_text="Header carrying the credential for 'API key header' auth. Defaults to X-API-Key.",
    )
    options = models.JSONField(
        default=dict, blank=True,
        help_text="Adapter-specific settings (non-secret), e.g. static headers or a timeout.",
    )
    declared_skills = models.JSONField(
        default=list, blank=True,
        help_text="For a mailbox agent: the skills its owner declares, as [{id, name, description}].",
    )

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="owned_a2a_cards",
    )
    team = models.ForeignKey(
        "organizations.Team", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="a2a_cards",
    )

    data_classification = models.CharField(
        max_length=20, choices=DataClassification.choices, default=DataClassification.INTERNAL,
        help_text="Its clearance: the most sensitive data it may be sent.",
    )
    risk_level = models.CharField(
        max_length=20, choices=RiskLevel.choices, default=RiskLevel.MODERATE, db_index=True
    )
    is_approved = models.BooleanField(
        default=False,
        help_text="Approved by governance. No agent can reach an unapproved card.",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="approved_a2a_cards",
    )
    # Restricted by default: an agent every other agent can hand work to should
    # be a decision somebody made, not an accident of registration.
    access_mode = models.CharField(
        max_length=20, choices=A2AAccessMode.choices, default=A2AAccessMode.RESTRICTED
    )

    # What the card said at the last sync.
    card = models.JSONField(default=dict, blank=True)
    card_hash = models.CharField(max_length=64, blank=True)
    protocol_version = models.CharField(max_length=20, blank=True)
    agent_version = models.CharField(max_length=50, blank=True)
    capabilities = models.JSONField(default=dict, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_sync_error = models.CharField(max_length=500, blank=True)

    # Health, from explicit probes.
    health_status = models.CharField(
        max_length=20, choices=HealthStatus.choices, default=HealthStatus.UNKNOWN, db_index=True
    )
    last_health_check = models.DateTimeField(null=True, blank=True)
    last_health_latency_ms = models.PositiveIntegerField(null=True, blank=True)
    last_health_message = models.CharField(max_length=500, blank=True)

    # The breaker, from live traffic. Written on state changes, not every call.
    circuit_state = models.CharField(
        max_length=20, choices=CircuitState.choices, default=CircuitState.CLOSED, db_index=True
    )
    consecutive_failures = models.PositiveIntegerField(default=0)
    circuit_opened_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    circuit_trips = models.PositiveIntegerField(
        default=0, help_text="Times this agent's breaker has opened, ever."
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "A2A agent card"
        constraints = [
            models.UniqueConstraint(fields=["organization", "slug"], name="uniq_a2a_card_slug_per_org")
        ]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "is_approved"]),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if self.agent_id and self.origin == CardOrigin.INTERNAL:
            # An internal agent is addressed by its agent_id, which never changes
            # once saved -- so neither does its address.
            self.name = self.name or self.agent.name
            self.slug = self.agent.agent_id
        self.slug = self.clean_slug(self.slug or self.name)
        super().save(*args, **kwargs)

    @staticmethod
    def clean_slug(value: str) -> str:
        return slugify(value)[:200].strip("-_") or "agent"

    def clean(self):
        errors = {}
        if self.origin == CardOrigin.INTERNAL:
            if not self.agent_id:
                errors["agent"] = "Choose the agent this card publishes."
            elif self.organization_id and self.agent.organization_id != self.organization_id:
                errors["agent"] = "The agent belongs to another organization."
        else:
            if self.agent_id:
                errors["agent"] = "An external agent is not one of the registry's agents."
            if self.transport == A2ATransport.MAILBOX:
                errors["transport"] = "An external agent is reached at its own endpoint, not the gateway's mailbox."
        if self.transport == A2ATransport.JSONRPC and not self.endpoint:
            errors["endpoint"] = "An agent reached over HTTP needs an endpoint."
        if self.needs_credential and not self.credential_ref:
            errors["credential_ref"] = "Name the environment variable that holds the credential."
        if errors:
            raise ValidationError(errors)

    # -- Adapter binding ----------------------------------------------------

    def adapter_config(self, *, timeout: float | None = None) -> AdapterConfig:
        options = dict(self.options or {})
        options.update({"auth_method": self.auth_method, "auth_header": self.auth_header, "card_url": self.card_url})
        return AdapterConfig(
            key=self.transport,
            name=self.name,
            endpoint=self.endpoint,
            credential_ref=self.credential_ref if self.needs_credential else "",
            timeout=float(timeout if timeout is not None else options.get("timeout", 30.0)),
            options=options,
        )

    def get_adapter(self, *, timeout: float | None = None):
        """Resolve through the registry. Nothing here branches on transport."""
        return registry.get_a2a(self.transport, self.adapter_config(timeout=timeout))

    # -- What it is ---------------------------------------------------------

    @property
    def is_internal(self) -> bool:
        return self.origin == CardOrigin.INTERNAL

    @property
    def is_external(self) -> bool:
        return self.origin == CardOrigin.EXTERNAL

    @property
    def uses_mailbox(self) -> bool:
        return self.transport == A2ATransport.MAILBOX

    @property
    def is_gateway_reachable(self) -> bool:
        return self.uses_mailbox or registry.has(AdapterKind.A2A, self.transport)

    @property
    def needs_credential(self) -> bool:
        return not self.uses_mailbox and self.auth_method != A2AAuthMethod.NONE

    @property
    def has_resolvable_credential(self) -> bool:
        return not self.needs_credential or self.adapter_config().has_credential()

    @property
    def is_serving(self) -> bool:
        """Reachable at all, before any contract, skill or content check."""
        return self.is_active and self.is_approved and self.is_gateway_reachable

    @property
    def address(self) -> str:
        """How the gateway, the log and the delegation graphs name this agent."""
        return self.agent.agent_id if self.agent_id else self.slug

    @property
    def has_incident(self) -> bool:
        return self.circuit_state == CircuitState.OPEN or self.health_status in {
            HealthStatus.DEGRADED,
            HealthStatus.UNHEALTHY,
        }

    def circuit_available(self, *, cool_off_seconds: int) -> bool:
        """Whether a delivery may be attempted now: an open breaker lets one probe through."""
        if self.circuit_state != CircuitState.OPEN:
            return True
        return bool(
            self.circuit_opened_at
            and timezone.now() - self.circuit_opened_at >= timedelta(seconds=cool_off_seconds)
        )


class SkillStatus(models.TextChoices):
    PENDING_REVIEW = "pending_review", "Awaiting review"
    ENABLED = "enabled", "Enabled"
    DISABLED = "disabled", "Disabled"
    REMOVED = "removed", "No longer advertised"


class A2ASkill(OrganizationOwnedModel):
    """A skill one agent's card advertises, as governance last saw it."""

    card = models.ForeignKey(A2AAgentCard, on_delete=models.CASCADE, related_name="skills")
    skill_id = models.CharField(max_length=128)
    name = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    tags = models.JSONField(default=list, blank=True)
    examples = models.JSONField(default=list, blank=True)
    input_modes = models.JSONField(default=list, blank=True)
    output_modes = models.JSONField(default=list, blank=True)
    definition_hash = models.CharField(max_length=64, blank=True)

    status = models.CharField(
        max_length=20, choices=SkillStatus.choices, default=SkillStatus.PENDING_REVIEW, db_index=True
    )
    risk_level = models.CharField(
        max_length=20, choices=RiskLevel.choices, default=RiskLevel.MODERATE, db_index=True
    )
    changed_since_review = models.BooleanField(
        default=False,
        help_text="The agent changed this skill's definition after it was last reviewed.",
    )
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reviewed_a2a_skills",
    )
    review_note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["card__name", "skill_id"]
        verbose_name = "A2A skill"
        constraints = [
            models.UniqueConstraint(fields=["card", "skill_id"], name="uniq_a2a_skill_per_card")
        ]
        indexes = [models.Index(fields=["organization", "status"])]

    def __str__(self) -> str:
        return self.qualified_name

    @property
    def qualified_name(self) -> str:
        return f"{self.card.slug}/{self.skill_id}"

    @property
    def label(self) -> str:
        return self.name or self.skill_id

    @property
    def is_callable(self) -> bool:
        return self.status == SkillStatus.ENABLED

    def to_wire(self) -> dict:
        """The skill as the published card shows it."""
        wire = {"id": self.skill_id, "name": self.label, "description": self.description, "tags": self.tags}
        if self.examples:
            wire["examples"] = self.examples
        if self.input_modes:
            wire["inputModes"] = self.input_modes
        if self.output_modes:
            wire["outputModes"] = self.output_modes
        return wire


class A2AContract(OrganizationOwnedModel):
    """Lets one agent send work to another: an edge of the communication graph.

    What an MCP access grant is to a tool server, a contract is to an agent. On
    a restricted card only senders holding one get through -- for the skills it
    names, with data up to its ceiling, at most at its rate. Contracts are part
    of the sender's authorized configuration, so a new or wider one for an
    agent authorized at Gate G3 is a change for Gate G4.
    """

    sender = models.ForeignKey(
        "agents.AIAgent", on_delete=models.CASCADE, related_name="a2a_contracts"
    )
    recipient = models.ForeignKey(A2AAgentCard, on_delete=models.CASCADE, related_name="contracts")
    allowed_skills = models.JSONField(
        default=list, blank=True,
        help_text="Skill ids as the recipient names them. Empty means every enabled skill.",
    )
    max_data_classification = models.CharField(
        max_length=20, choices=DataClassification.choices, blank=True,
        help_text="The most sensitive data the sender may send. Blank: the recipient's clearance.",
    )
    max_messages_per_hour = models.PositiveIntegerField(
        null=True, blank=True, help_text="Blank for no rate limit."
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    purpose = models.CharField(
        max_length=500, blank=True, help_text="Why the sender needs to reach this agent."
    )

    class Meta:
        ordering = ["recipient__name", "sender__name"]
        verbose_name = "A2A contract"
        constraints = [
            models.UniqueConstraint(fields=["sender", "recipient"], name="uniq_a2a_contract_per_pair")
        ]

    def __str__(self) -> str:
        return f"{self.sender} → {self.recipient}"

    def clean(self):
        errors = {}
        if self.recipient_id and self.sender_id:
            if self.sender.organization_id != self.recipient.organization_id:
                errors["sender"] = "The sender and the recipient must belong to the same organization."
            elif self.recipient.agent_id == self.sender_id:
                errors["sender"] = "An agent does not need a contract to reach itself."
        if self.recipient_id and self.max_data_classification and (
            sensitivity(self.max_data_classification) > sensitivity(self.recipient.data_classification)
        ):
            errors["max_data_classification"] = (
                f"{self.recipient.name} is cleared for {self.recipient.data_classification} data at most."
            )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        # A contract lives wherever its recipient lives; it is never chosen separately.
        self.organization_id = self.recipient.organization_id
        super().save(*args, **kwargs)

    @property
    def is_expired(self) -> bool:
        return bool(self.expires_at and self.expires_at <= timezone.now())

    def permits(self, skill_id: str) -> bool:
        return not self.allowed_skills or skill_id in self.allowed_skills

    @property
    def ceiling(self) -> str:
        """The most sensitive data this contract lets through: never above the recipient's clearance."""
        return least_sensitive(self.max_data_classification or self.recipient.data_classification,
                               self.recipient.data_classification)


class TaskState(models.TextChoices):
    """A2A's own vocabulary for where a task stands."""

    SUBMITTED = "submitted", "Submitted"
    WORKING = "working", "Working"
    INPUT_REQUIRED = "input-required", "Input required"
    AUTH_REQUIRED = "auth-required", "Authorization required"
    COMPLETED = "completed", "Completed"
    CANCELED = "canceled", "Canceled"
    FAILED = "failed", "Failed"
    REJECTED = "rejected", "Rejected"
    UNKNOWN = "unknown", "Unknown"


#: A task in one of these states never changes again.
TERMINAL_STATES = (TaskState.COMPLETED, TaskState.CANCELED, TaskState.FAILED, TaskState.REJECTED)
#: States in which the sender, not the recipient, has the next move.
AWAITING_SENDER = (TaskState.INPUT_REQUIRED, TaskState.AUTH_REQUIRED)


class DeliveryStatus(models.TextChoices):
    """What the gateway did with the message, apart from what became of the work."""

    DELIVERED = "delivered", "Delivered"
    BLOCKED = "blocked", "Blocked"
    UNAVAILABLE = "unavailable", "Agent unavailable"
    FAILED = "failed", "Delivery failed"


class A2AChannel(models.TextChoices):
    JSONRPC = "jsonrpc", "A2A endpoint"
    REST = "rest", "REST API"
    MAILBOX = "mailbox", "Agent messages (legacy)"


class A2ATask(OrganizationOwnedModel):
    """One agent-to-agent exchange through the gateway -- the refused ones included.

    A task in A2A's sense: the sender's message, the recipient's answers and the
    state the work is in. Its own record rather than an Execution, like an MCP
    tool call: an exchange is a step inside an agent's run, and when the sender
    names the run the exchange goes onto its timeline too.
    """

    sender = models.ForeignKey(
        "agents.AIAgent", on_delete=models.SET_NULL, null=True, blank=True, related_name="a2a_tasks_sent"
    )
    recipient = models.ForeignKey(
        A2AAgentCard, on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks"
    )
    recipient_agent = models.ForeignKey(
        "agents.AIAgent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="a2a_tasks_received", help_text="The registry agent behind an internal card.",
    )
    recipient_name = models.CharField(max_length=200, help_text="The address as the sender used it.")
    skill = models.ForeignKey(
        A2ASkill, on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks"
    )
    skill_name = models.CharField(max_length=128, blank=True)
    contract = models.ForeignKey(
        A2AContract, on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks"
    )
    service_account = models.ForeignKey(
        "api.ServiceAccount", on_delete=models.SET_NULL, null=True, blank=True, related_name="a2a_tasks"
    )
    execution = models.ForeignKey(
        "executions.Execution", on_delete=models.SET_NULL, null=True, blank=True, related_name="a2a_tasks"
    )
    channel = models.CharField(max_length=10, choices=A2AChannel.choices, default=A2AChannel.JSONRPC)

    context_id = models.CharField(
        max_length=200, blank=True, db_index=True,
        help_text="A2A contextId: groups the exchanges of one conversation.",
    )
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="handoffs",
        help_text="The task the sender was working on when it handed this on.",
    )
    hops = models.PositiveSmallIntegerField(default=1, help_text="How many agents the work has passed through.")
    trail = models.JSONField(default=list, blank=True, help_text="The agents the work passed through, first to last.")

    status = models.CharField(max_length=20, choices=DeliveryStatus.choices, db_index=True)
    state = models.CharField(max_length=20, choices=TaskState.choices, default=TaskState.SUBMITTED, db_index=True)
    reason = models.CharField(max_length=1000, blank=True)
    policies = models.JSONField(default=list, blank=True)
    limit_breached = models.CharField(
        max_length=40, blank=True, help_text="The limit that refused this message, if one did."
    )
    latency_ms = models.PositiveIntegerField(null=True, blank=True)

    data_classification = models.CharField(
        max_length=20, choices=DataClassification.choices, blank=True,
        help_text="What the message carried: as the sender declared it, raised if personal data was found.",
    )
    personal_data = models.JSONField(default=list, blank=True, help_text="Kinds of personal data found in it.")

    subject = models.CharField(max_length=255, blank=True)
    message = models.JSONField(default=dict, blank=True, help_text="The A2A message the sender sent.")
    text_preview = models.TextField(blank=True)
    status_message = models.JSONField(null=True, blank=True, help_text="The recipient's latest status message.")
    artifacts = models.JSONField(default=list, blank=True)
    history = models.JSONField(default=list, blank=True, help_text="Each state the task passed through.")
    turns = models.PositiveSmallIntegerField(default=1, help_text="Messages the sender sent in this task.")

    remote_task_id = models.CharField(max_length=200, blank=True)
    remote_context_id = models.CharField(max_length=200, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    collected_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "A2A task"
        indexes = [
            models.Index(fields=["organization", "-created_at"]),
            models.Index(fields=["organization", "status", "-created_at"]),
            models.Index(fields=["recipient_agent", "state"]),
            models.Index(fields=["organization", "context_id"]),
            models.Index(fields=["contract", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.sender_label} → {self.recipient_name} ({self.get_state_display()})"

    @property
    def sender_label(self) -> str:
        return self.sender.agent_id if self.sender_id else "no sender"

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def reached_recipient(self) -> bool:
        return self.status == DeliveryStatus.DELIVERED

    @property
    def awaits_sender(self) -> bool:
        return self.reached_recipient and self.state in AWAITING_SENDER

    @property
    def answer_text(self) -> str:
        """The recipient's answer, as text: its artifacts, else its latest status message."""
        pieces = [parts_text(artifact.get("parts")) for artifact in self.artifacts or [] if isinstance(artifact, dict)]
        if not any(pieces) and isinstance(self.status_message, dict):
            pieces = [parts_text(self.status_message.get("parts"))]
        return "\n".join(piece for piece in pieces if piece)
