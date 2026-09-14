"""Agent registry (section 12)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.text import slugify

from apps.core.constants import DataClassification, Environment, RiskLevel
from apps.core.models import OrganizationScopedModel
from integrations.base.config import AdapterConfig
from integrations.registry import registry


class AgentFramework(models.TextChoices):
    LANGGRAPH = "langgraph", "LangGraph"
    CREWAI = "crewai", "CrewAI"
    OPENAI_AGENTS = "openai_agents", "OpenAI Agents SDK"
    AUTOGEN = "autogen", "AutoGen"
    SEMANTIC_KERNEL = "semantic_kernel", "Semantic Kernel"
    CUSTOM_PYTHON = "custom_python", "Custom Python Agent"
    REMOTE_PLATFORM = "remote_platform", "Remote Agent Platform"
    MOCK = "mock", "Mock / Demo"


class AutonomyLevel(models.IntegerChoices):
    """How much an agent may do without a human in the loop."""

    ADVISORY = 0, "0 - Advisory"
    RECOMMEND = 1, "1 - Recommend Actions"
    EXECUTE_WITH_APPROVAL = 2, "2 - Execute With Approval"
    EXECUTE_WITHIN_BOUNDARIES = 3, "3 - Execute Within Defined Boundaries"
    AUTONOMOUS = 4, "4 - Autonomous Within Policy"


#: Limits an Agent Card may declare (IRGF agent chain link 06). Anything else is
#: rejected, so a misspelt limit cannot pass for one that is set.
ACTION_LIMIT_KEYS = (
    "max_value_per_action",
    "max_value_per_day",
    "max_actions_per_hour",
    "currency",
    "allowed_counterparties",
)


def validate_action_limits(value) -> None:
    if value in (None, "", {}):
        return
    if not isinstance(value, dict):
        raise ValidationError("Action limits must be an object of limit names to values.")

    unknown = sorted(set(value) - set(ACTION_LIMIT_KEYS))
    if unknown:
        raise ValidationError(
            f"Unknown action limit(s): {', '.join(unknown)}. "
            f"Allowed: {', '.join(ACTION_LIMIT_KEYS)}."
        )
    for key in ("max_value_per_action", "max_value_per_day"):
        if key in value:
            try:
                amount = Decimal(str(value[key]))
            except (InvalidOperation, ValueError):
                raise ValidationError(f"{key} must be a number.") from None
            if amount < 0:
                raise ValidationError(f"{key} cannot be negative.")
    if "max_actions_per_hour" in value:
        rate = value["max_actions_per_hour"]
        if not isinstance(rate, int) or isinstance(rate, bool) or rate < 1:
            raise ValidationError("max_actions_per_hour must be a whole number of at least 1.")
    if "currency" in value:
        currency = value["currency"]
        if not isinstance(currency, str) or len(currency) != 3:
            raise ValidationError("currency must be a three-letter code, e.g. USD.")
    if "allowed_counterparties" in value:
        parties = value["allowed_counterparties"]
        if not isinstance(parties, list) or not all(isinstance(p, str) for p in parties):
            raise ValidationError("allowed_counterparties must be a list of names.")


#: Kill-switch conditions a machine can evaluate (IRGF link 06). IRGF asks for
#: Tier 3-4 agents' conditions to trigger without waiting for a person; an agent
#: lists here the ones pre-approved at Gate G3, and the Control Plane pulls the
#: kill-switch when one fires -- the only correction it automates.
KILL_SWITCH_TRIGGERS = {
    "action_limit_breach": "An attempt beyond a value, rate or counterparty limit (link 06)",
    "boundary_divergence": "Reaching a tool outside the authorized boundary (links 04 and 06)",
    "unbound_identity": "Acting under an identity not bound to the agent (link 02)",
}


def validate_kill_switch_triggers(value) -> None:
    if value in (None, "", []):
        return
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError("Kill-switch triggers must be a list of trigger names.")
    unknown = sorted(set(value) - set(KILL_SWITCH_TRIGGERS))
    if unknown:
        raise ValidationError(
            f"Unknown kill-switch trigger(s): {', '.join(unknown)}. "
            f"Allowed: {', '.join(KILL_SWITCH_TRIGGERS)}."
        )


class AIAgent(OrganizationScopedModel):
    name = models.CharField(max_length=200)
    agent_id = models.SlugField(
        max_length=200, help_text="Stable identifier external engines report against."
    )
    description = models.TextField(blank=True)
    business_purpose = models.TextField(blank=True)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="owned_agents",
    )
    team = models.ForeignKey(
        "organizations.Team",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="agents",
    )

    framework = models.CharField(
        max_length=40, choices=AgentFramework.choices, default=AgentFramework.MOCK, db_index=True
    )
    provider = models.ForeignKey(
        "providers.AIProvider",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="agents",
    )
    primary_model = models.ForeignKey(
        "models_registry.AIModel",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="primary_for_agents",
    )
    fallback_model = models.ForeignKey(
        "models_registry.AIModel",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fallback_for_agents",
    )

    environment = models.CharField(
        max_length=20, choices=Environment.choices, default=Environment.DEVELOPMENT, db_index=True
    )
    risk_level = models.CharField(
        max_length=20, choices=RiskLevel.choices, default=RiskLevel.LOW, db_index=True
    )
    autonomy_level = models.IntegerField(
        choices=AutonomyLevel.choices, default=AutonomyLevel.ADVISORY, db_index=True
    )
    requires_human_approval = models.BooleanField(default=True)

    allowed_tools = models.JSONField(default=list, blank=True)
    restricted_tools = models.JSONField(default=list, blank=True)
    knowledge_sources = models.JSONField(default=list, blank=True)
    data_classification = models.CharField(
        max_length=20, choices=DataClassification.choices, default=DataClassification.INTERNAL
    )

    system_prompt_reference = models.CharField(
        max_length=500,
        blank=True,
        help_text="Pointer to the prompt in source control or a prompt store, not the prompt text.",
    )
    adapter_key = models.CharField(max_length=64, default="mock")
    remote_endpoint = models.URLField(blank=True)
    options = models.JSONField(default=dict, blank=True)

    # IRGF risk tier (M5), cached from the current S2 classification so policies
    # and approval rules can read it on every gateway call without a query.
    # Written only by the classification service.
    risk_tier = models.PositiveSmallIntegerField(null=True, blank=True, editable=False, db_index=True)

    # The Agent Card: the record IRGF produces at S3 and requires at Gate G3 for
    # any agent that can act.
    out_of_scope = models.TextField(
        blank=True, help_text="What this agent is not for (IRGF agent chain link 01)."
    )
    action_limits = models.JSONField(
        default=dict,
        blank=True,
        validators=[validate_action_limits],
        help_text=(
            "Value, rate and counterparty limits (link 06): max_value_per_action, "
            "max_value_per_day, max_actions_per_hour, currency, allowed_counterparties."
        ),
    )
    escalation_thresholds = models.TextField(
        blank=True, help_text="When the agent must stop and hand over to a person."
    )
    kill_switch_conditions = models.TextField(
        blank=True,
        help_text="Pre-approved conditions under which the agent is suspended (link 06).",
    )
    kill_switch_triggers = models.JSONField(
        default=list,
        blank=True,
        validators=[validate_kill_switch_triggers],
        help_text=(
            "Kill-switch conditions the Control Plane evaluates itself (link 06): "
            "action_limit_breach, boundary_divergence, unbound_identity."
        ),
    )

    # Kill-switch state. Set only through the suspend and resume actions, which
    # audit the change and tell the agent's managers.
    suspended_at = models.DateTimeField(null=True, blank=True, editable=False)
    suspended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="suspended_agents",
        editable=False,
    )
    suspended_by_label = models.CharField(max_length=255, blank=True, editable=False)
    suspension_reason = models.TextField(blank=True, editable=False)

    class Meta:
        ordering = ["name"]
        verbose_name = "AI agent"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "agent_id"], name="uniq_agent_id_per_org"
            )
        ]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "risk_level"]),
            models.Index(fields=["organization", "autonomy_level"]),
            models.Index(fields=["framework"]),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.agent_id:
            self.agent_id = slugify(self.name)[:200]
        super().save(*args, **kwargs)

    # -- Adapter binding ----------------------------------------------------

    def adapter_config(self) -> AdapterConfig:
        return AdapterConfig(
            key=self.adapter_key,
            name=self.name,
            endpoint=self.remote_endpoint,
            options=self.options or {},
        )

    def get_adapter(self):
        return registry.get_agent(self.adapter_key, self.adapter_config())

    # -- Governance ---------------------------------------------------------

    @property
    def autonomy_threshold(self) -> int:
        return settings.CONTROL_TOWER["AGENT_AUTONOMY_APPROVAL_THRESHOLD"]

    @property
    def approval_required(self) -> bool:
        """Approval is required if configured, or forced by the autonomy policy."""
        return self.requires_human_approval or self.autonomy_level >= self.autonomy_threshold

    @property
    def has_excessive_autonomy(self) -> bool:
        """High autonomy on a high-risk agent, with no approval gate."""
        return (
            self.autonomy_level >= self.autonomy_threshold
            and not self.requires_human_approval
            and self.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}
        )

    @property
    def is_suspended(self) -> bool:
        return self.suspended_at is not None

    @property
    def acts(self) -> bool:
        """Whether the agent can change things, which is when IRGF wants an Agent Card.

        Advisory agents that only inform are exempt: IRGF's minimum viable
        framework asks for the card only where there is genuine tool access or
        autonomy.
        """
        return self.autonomy_level >= AutonomyLevel.EXECUTE_WITH_APPROVAL or bool(self.allowed_tools)

    def agent_card_gaps(self) -> list[str]:
        """What the Agent Card still lacks. Required at Gate G3 when the agent acts."""
        gaps = []
        if not (self.business_purpose or "").strip():
            gaps.append("Scope: what the agent is for (link 01)")
        if not (self.out_of_scope or "").strip():
            gaps.append("Scope: what the agent is not for (link 01)")
        if not self.owner_id:
            gaps.append("A named human owner")
        if not self.allowed_tools:
            gaps.append("An explicit tool allow-list (link 04)")
        if not self.action_limits:
            gaps.append("Action limits on value, rate or counterparties (link 06)")
        if not (self.escalation_thresholds or "").strip():
            gaps.append("Escalation thresholds")
        if not (self.kill_switch_conditions or "").strip():
            gaps.append("Pre-approved kill-switch conditions (link 06)")
        return gaps

    @property
    def action_limit_items(self) -> list[tuple[str, str]]:
        """The declared action limits as readable label and value pairs."""
        labels = {
            "max_value_per_action": "Max value per action",
            "max_value_per_day": "Max value per day",
            "max_actions_per_hour": "Max actions per hour",
            "currency": "Currency",
            "allowed_counterparties": "Allowed counterparties",
        }
        return [
            (labels.get(key, key), ", ".join(map(str, value)) if isinstance(value, list) else str(value))
            for key, value in (self.action_limits or {}).items()
        ]

    @property
    def kill_switch_trigger_labels(self) -> list[str]:
        """The automatic kill-switch triggers, described (link 06)."""
        return [KILL_SWITCH_TRIGGERS[key] for key in self.kill_switch_triggers or [] if key in KILL_SWITCH_TRIGGERS]
