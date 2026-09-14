"""AI control policies (section 18).

A deliberately fixed set of typed, parameterised policy types rather than an
expression language -- section 40 puts a policy DSL explicitly out of scope.
Each type has a documented configuration schema and its own evaluator; adding a
new kind of control means adding a type and an evaluator, not teaching the
platform to parse expressions.

The important distinction from an approval rule: a policy can *deny* outright,
not merely route to a human.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.core.constants import DataClassification, Environment, RiskLevel
from apps.core.models import OrganizationOwnedModel, OrganizationScopedModel


class PolicyType(models.TextChoices):
    MODEL_APPROVAL = "model_approval", "Model Approval Policy"
    PROVIDER = "provider", "Provider Policy"
    DATA_CLASSIFICATION = "data_classification", "Data Classification Policy"
    AGENT_AUTONOMY = "agent_autonomy", "Agent Autonomy Policy"
    COST_LIMIT = "cost_limit", "Cost Limit Policy"
    TOOL_ACCESS = "tool_access", "Tool Access Policy"
    HUMAN_APPROVAL = "human_approval", "Human Approval Policy"
    RISK_CLASSIFICATION = "risk_classification", "Risk Classification Policy"
    DEPLOYMENT_AUTHORIZATION = "deployment_authorization", "Deployment Authorization Policy"


class PolicyEffect(models.TextChoices):
    """What happens when a policy matches.

    There is no explicit ALLOW effect: permitting is expressed inside a
    policy's configuration (an allow-list), which avoids the precedence
    subtleties that come with competing allow and deny rules.
    """

    DENY = "deny", "Deny"
    REQUIRE_APPROVAL = "require_approval", "Require Approval"
    WARN = "warn", "Warn"


#: Configuration keys each policy type understands. Anything else is rejected
#: at save time, so a typo cannot silently disable a control.
POLICY_SCHEMA: dict[str, set[str]] = {
    PolicyType.MODEL_APPROVAL: {
        "blocked_risk_classifications",
        # Evaluation gating. Off unless configured, so an existing model
        # approval policy does not start blocking traffic on upgrade.
        "require_evaluation",
        "evaluation_max_age_days",
        "required_suites",
    },
    PolicyType.PROVIDER: {
        "allowed_providers",
        "blocked_providers",
        "allowed_regions",
        "require_approved",
    },
    PolicyType.DATA_CLASSIFICATION: {
        "classifications",
        "blocked_providers",
        "environments",
        "require_approved_provider",
    },
    PolicyType.AGENT_AUTONOMY: {"max_autonomy_level", "environments", "risk_levels"},
    PolicyType.COST_LIMIT: {"max_cost_per_execution"},
    PolicyType.TOOL_ACCESS: {"blocked_tools", "environments"},
    PolicyType.HUMAN_APPROVAL: set(),
    # IRGF S2: the system acting must carry a current risk classification.
    PolicyType.RISK_CLASSIFICATION: {
        "environments",
        "require_countersignature",
        "block_when_rescore_due",
    },
    # IRGF S7 · Gate G3: the agent must hold an authorization in force.
    PolicyType.DEPLOYMENT_AUTHORIZATION: {"environments", "block_pending_changes"},
}

#: Keys every policy type accepts. `tiers` scopes a policy to IRGF risk tiers
#: (1-4), taken from the agent's and application's S2 classification. An
#: unclassified system matches no tier-scoped policy.
COMMON_POLICY_KEYS = {"tiers"}


#: What each type checks with no settings at all. Several types are meaningful
#: unconfigured -- a model approval policy already knows what "approved" means.
INHERENT_CHECKS = {
    PolicyType.MODEL_APPROVAL: "model must be approved for use",
    PolicyType.PROVIDER: "no restrictions configured",
    PolicyType.DATA_CLASSIFICATION: "no classifications configured",
    PolicyType.AGENT_AUTONOMY: "no autonomy ceiling configured",
    PolicyType.COST_LIMIT: "no cost ceiling configured",
    PolicyType.TOOL_ACCESS: "no tools blocked",
    PolicyType.HUMAN_APPROVAL: "delegates to the configured approval rules",
    PolicyType.RISK_CLASSIFICATION: "a countersigned S2 risk classification is required",
    PolicyType.DEPLOYMENT_AUTHORIZATION: "the agent needs a deployment authorization in force (S7 · G3)",
}


class Policy(OrganizationScopedModel):
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    policy_type = models.CharField(max_length=40, choices=PolicyType.choices, db_index=True)
    effect = models.CharField(
        max_length=20, choices=PolicyEffect.choices, default=PolicyEffect.REQUIRE_APPROVAL
    )
    severity = models.CharField(
        max_length=20, choices=RiskLevel.choices, default=RiskLevel.MODERATE
    )
    priority = models.PositiveSmallIntegerField(
        default=100, help_text="Lower numbers are evaluated first."
    )
    configuration = models.JSONField(
        default=dict, blank=True, help_text="Typed settings for this policy type."
    )
    #: Free-text pointer to the control framework this implements, e.g.
    #: "EU AI Act Art. 14" -- mapping frameworks properly is a future module.
    reference = models.CharField(max_length=255, blank=True)
    #: When what the policy does last changed, as opposed to how it is named or
    #: described. The Control Plane compares it with authorizations (IRGF M13).
    content_changed_at = models.DateTimeField(null=True, blank=True, editable=False)

    #: The fields that decide what a policy does.
    SUBSTANCE_FIELDS = ("policy_type", "effect", "severity", "priority", "status", "configuration")

    class Meta:
        ordering = ["priority", "name"]
        verbose_name_plural = "policies"
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="uniq_policy_name_per_org")
        ]
        indexes = [models.Index(fields=["organization", "status", "priority"])]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        # A save that changes nothing a policy does -- a re-import, a reworded
        # description -- must not look like a new policy to everything it governs.
        if self._state.adding:
            self.content_changed_at = self.content_changed_at or timezone.now()
        else:
            previous = type(self).objects.filter(pk=self.pk).values(*self.SUBSTANCE_FIELDS).first()
            if previous is None or any(previous[field] != getattr(self, field) for field in self.SUBSTANCE_FIELDS):
                self.content_changed_at = timezone.now()
                if kwargs.get("update_fields") is not None:
                    kwargs["update_fields"] = {*kwargs["update_fields"], "content_changed_at"}
        super().save(*args, **kwargs)

    def clean(self):
        allowed = POLICY_SCHEMA.get(self.policy_type, set()) | COMMON_POLICY_KEYS
        unknown = set(self.configuration or {}) - allowed
        if unknown:
            raise ValidationError(
                {
                    "configuration": (
                        f"Unknown setting(s) for a {self.get_policy_type_display()}: "
                        f"{', '.join(sorted(unknown))}. Allowed: "
                        f"{', '.join(sorted(allowed)) or 'none'}."
                    )
                }
            )

        tiers = (self.configuration or {}).get("tiers")
        if tiers is not None and (
            not isinstance(tiers, list)
            or not all(
                isinstance(tier, int) and not isinstance(tier, bool) and 1 <= tier <= 4
                for tier in tiers
            )
        ):
            raise ValidationError(
                {"configuration": "tiers must be a list of IRGF tiers: whole numbers from 1 to 4."}
            )

    def option(self, key: str, default=None):
        return (self.configuration or {}).get(key, default)

    @property
    def is_blocking(self) -> bool:
        return self.effect == PolicyEffect.DENY

    @property
    def summary(self) -> str:
        """Readable description of what this policy checks."""
        config = self.configuration or {}
        if not config:
            return INHERENT_CHECKS.get(self.policy_type, self.get_policy_type_display())

        parts = []
        for key, value in sorted(config.items()):
            if isinstance(value, list):
                parts.append(f"{key.replace('_', ' ')}: {', '.join(str(v) for v in value)}")
            else:
                parts.append(f"{key.replace('_', ' ')}: {value}")
        return "; ".join(parts)


class PolicyViolation(OrganizationOwnedModel):
    """A record that a policy matched and blocked or flagged an action.

    Kept as its own record rather than only an execution event, because a
    policy check often happens *before* any execution exists -- an engine asks
    permission first.
    """

    policy = models.ForeignKey(Policy, on_delete=models.CASCADE, related_name="violations")
    execution = models.ForeignKey(
        "executions.Execution", null=True, blank=True, on_delete=models.CASCADE,
        related_name="policy_violations",
    )
    agent = models.ForeignKey(
        "agents.AIAgent", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="policy_violations",
    )
    application = models.ForeignKey(
        "applications_registry.AIApplication", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="policy_violations",
    )
    model = models.ForeignKey(
        "models_registry.AIModel", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="policy_violations",
    )
    provider = models.ForeignKey(
        "providers.AIProvider", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="policy_violations",
    )

    effect = models.CharField(max_length=20, choices=PolicyEffect.choices)
    severity = models.CharField(
        max_length=20, choices=RiskLevel.choices, default=RiskLevel.MODERATE, db_index=True
    )
    reason = models.TextField(blank=True)
    requested_action = models.CharField(max_length=255, blank=True)
    context = models.JSONField(default=dict, blank=True)

    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="acknowledged_policy_violations",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "-created_at"]),
            models.Index(fields=["organization", "effect", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.policy.name} ({self.get_effect_display()})"

    @property
    def is_acknowledged(self) -> bool:
        return self.acknowledged_at is not None


#: Seed policies for a new organization, covering every type named in
#: section 18 so the architecture is visible from day one.
DEFAULT_POLICIES = [
    {
        "name": "Unapproved models may not run in production",
        "description": "Governance must approve a model before production traffic reaches it.",
        "policy_type": PolicyType.MODEL_APPROVAL,
        "effect": PolicyEffect.DENY,
        "severity": RiskLevel.HIGH,
        "priority": 10,
        "configuration": {},
        "reference": "Internal AI Governance Standard 4.2",
    },
    {
        "name": "Restricted data stays with approved providers",
        "description": "Restricted or confidential data may only reach an approved provider.",
        "policy_type": PolicyType.DATA_CLASSIFICATION,
        "effect": PolicyEffect.DENY,
        "severity": RiskLevel.CRITICAL,
        "priority": 20,
        "configuration": {
            "classifications": [DataClassification.RESTRICTED, DataClassification.CONFIDENTIAL],
            "require_approved_provider": True,
        },
        "reference": "Data Protection Policy 7.1",
    },
    {
        "name": "Providers must be approved and in-region",
        "description": "Only approved providers in permitted regions may serve production traffic.",
        "policy_type": PolicyType.PROVIDER,
        "effect": PolicyEffect.REQUIRE_APPROVAL,
        "severity": RiskLevel.HIGH,
        "priority": 30,
        "configuration": {"require_approved": True},
    },
    {
        "name": "Autonomy is capped in production",
        "description": "Agents above level 3 need sign-off before acting in production.",
        "policy_type": PolicyType.AGENT_AUTONOMY,
        "effect": PolicyEffect.REQUIRE_APPROVAL,
        "severity": RiskLevel.HIGH,
        "priority": 40,
        "configuration": {
            "max_autonomy_level": 3,
            "environments": [Environment.PRODUCTION],
        },
        "reference": "EU AI Act Art. 14 (human oversight)",
    },
    {
        "name": "Single-run cost ceiling",
        "description": "A single execution may not exceed the per-run spend ceiling.",
        "policy_type": PolicyType.COST_LIMIT,
        "effect": PolicyEffect.REQUIRE_APPROVAL,
        "severity": RiskLevel.MODERATE,
        "priority": 50,
        "configuration": {"max_cost_per_execution": "5.00"},
    },
    {
        "name": "Prohibited tools",
        "description": "These tools are never available to an AI agent.",
        "policy_type": PolicyType.TOOL_ACCESS,
        "effect": PolicyEffect.DENY,
        "severity": RiskLevel.CRITICAL,
        "priority": 60,
        "configuration": {
            "blocked_tools": ["delete_account", "transfer_funds", "modify_permissions"]
        },
    },
    {
        "name": "Human approval rules",
        "description": "Delegates to the configured approval rules in the Approval Centre.",
        "policy_type": PolicyType.HUMAN_APPROVAL,
        "effect": PolicyEffect.REQUIRE_APPROVAL,
        "severity": RiskLevel.MODERATE,
        "priority": 70,
        "configuration": {},
    },
]
