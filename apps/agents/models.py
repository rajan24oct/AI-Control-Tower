"""Agent registry (section 12)."""

from __future__ import annotations

from django.conf import settings
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
