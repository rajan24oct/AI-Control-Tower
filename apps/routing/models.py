"""Model routing: smart selection and automatic fallback across providers.

The Control Tower decides the route; it does not drive it. Nothing here calls a
model. A routing policy produces an *ordered plan* -- a primary and a ranked
fallback chain, with unhealthy and non-compliant candidates already removed --
and the engine walks that plan itself, then reports which entry actually
served. That keeps the Tower out of the request path, where a governance
platform being slow or down must never be able to take an agent down with it.

"Automatic fallback" is therefore a property of the plan, not of a proxy: by
the time an engine receives the chain, every entry in it is one the platform is
willing to stand behind right now.

Like policies, the selection rules are typed fields rather than an expression
language -- section 40 puts a DSL out of scope, and the reasoning applies just
as well to routing as to policy.
"""

from __future__ import annotations

from django.db import models

from apps.core.constants import Environment
from apps.core.models import OrganizationOwnedModel, OrganizationScopedModel
from apps.models_registry.models import ModelType


class RoutingStrategy(models.TextChoices):
    """What the policy optimises for when candidates are otherwise equal."""

    COST = "cost", "Lowest cost"
    LATENCY = "latency", "Lowest latency"
    RELIABILITY = "reliability", "Highest reliability"
    BALANCED = "balanced", "Balanced"


#: How each strategy weighs the three observed signals. Weights sum to 1 so the
#: resulting score is comparable across strategies when reading decision logs.
STRATEGY_WEIGHTS: dict[str, dict[str, float]] = {
    RoutingStrategy.COST: {"cost": 0.60, "reliability": 0.25, "latency": 0.15},
    RoutingStrategy.LATENCY: {"latency": 0.60, "reliability": 0.25, "cost": 0.15},
    # Reliability leans hard on its own signal: if you asked for reliability,
    # a cheaper model that fails more often is not a trade you wanted.
    RoutingStrategy.RELIABILITY: {"reliability": 0.70, "latency": 0.20, "cost": 0.10},
    RoutingStrategy.BALANCED: {"cost": 0.34, "reliability": 0.33, "latency": 0.33},
}


class RoutingPolicy(OrganizationScopedModel):
    """Which models may serve a class of work, and in what order."""

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    strategy = models.CharField(
        max_length=20, choices=RoutingStrategy.choices, default=RoutingStrategy.BALANCED
    )
    priority = models.PositiveSmallIntegerField(
        default=100, help_text="Lower numbers are considered first."
    )

    # -- What kind of work this policy answers for -------------------------

    model_type = models.CharField(
        max_length=40, choices=ModelType.choices, default=ModelType.CHAT, db_index=True
    )
    environments = models.JSONField(
        default=list,
        blank=True,
        help_text="Environments this policy applies to. Empty means all.",
    )
    agents = models.ManyToManyField(
        "agents.AIAgent",
        blank=True,
        related_name="routing_policies",
        help_text="Agents this policy applies to. Empty means all.",
    )

    # -- Which candidates are admissible -----------------------------------

    required_capabilities = models.JSONField(
        default=list, blank=True, help_text="Capabilities a model must advertise."
    )
    min_context_window = models.PositiveIntegerField(
        null=True, blank=True, help_text="Smallest usable context window, in tokens."
    )
    max_input_cost_per_1k = models.DecimalField(
        max_digits=12,
        decimal_places=6,
        null=True,
        blank=True,
        help_text="Ceiling on input rate. Leave empty for no ceiling.",
    )
    allowed_providers = models.ManyToManyField(
        "providers.AIProvider",
        blank=True,
        related_name="routing_allowed_by",
        help_text="If set, only these providers are considered.",
    )
    blocked_providers = models.ManyToManyField(
        "providers.AIProvider",
        blank=True,
        related_name="routing_blocked_by",
        help_text="Never routed to, even if otherwise eligible.",
    )
    require_approved_models = models.BooleanField(
        default=True,
        help_text="Only route to models approved for use in the registry.",
    )
    allow_degraded_providers = models.BooleanField(
        default=True,
        help_text="Keep degraded providers as fallbacks. Unhealthy ones are always excluded.",
    )

    fallback_depth = models.PositiveSmallIntegerField(
        default=2, help_text="Fallbacks returned after the primary."
    )

    class Meta:
        ordering = ["priority", "name"]
        verbose_name_plural = "routing policies"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"], name="uniq_routing_policy_name_per_org"
            )
        ]
        indexes = [models.Index(fields=["organization", "model_type", "priority"])]

    def __str__(self) -> str:
        return self.name

    @property
    def weights(self) -> dict[str, float]:
        return STRATEGY_WEIGHTS.get(self.strategy, STRATEGY_WEIGHTS[RoutingStrategy.BALANCED])

    @property
    def chain_length(self) -> int:
        return self.fallback_depth + 1

    def applies_to_environment(self, environment: str) -> bool:
        return not self.environments or environment in self.environments


class RouteOutcome(models.TextChoices):
    PENDING = "pending", "Awaiting outcome"
    SERVED = "served", "Served"
    EXHAUSTED = "exhausted", "Chain exhausted"
    ABANDONED = "abandoned", "Abandoned"


class RoutingDecision(OrganizationOwnedModel):
    """One answer to "which model should I use?", and what happened next.

    Recorded for every decision, because a routing plan nobody can audit is
    just a hidden dependency. The outcome half is reported by the engine and is
    what makes fallback measurable rather than merely offered.
    """

    policy = models.ForeignKey(
        RoutingPolicy,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="decisions",
    )
    agent = models.ForeignKey(
        "agents.AIAgent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="routing_decisions",
    )
    execution = models.ForeignKey(
        "executions.Execution", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="routing_decisions",
    )
    service_account = models.ForeignKey(
        "api.ServiceAccount", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="routing_decisions",
    )

    model_type = models.CharField(max_length=40, default=ModelType.CHAT)
    environment = models.CharField(
        max_length=20, choices=Environment.choices, default=Environment.PRODUCTION
    )
    strategy = models.CharField(max_length=20, choices=RoutingStrategy.choices)

    selected_model = models.ForeignKey(
        "models_registry.AIModel", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="routed_as_primary",
    )
    #: The full ordered plan as sent, including scores and the reason each
    #: candidate placed where it did. Frozen at decision time -- re-deriving it
    #: later would answer a different question, because health moves.
    chain = models.JSONField(default=list, blank=True)
    #: Candidates ruled out, with why. The more useful half when a plan is empty.
    excluded = models.JSONField(default=list, blank=True)
    reason = models.CharField(max_length=500, blank=True)

    outcome = models.CharField(
        max_length=20, choices=RouteOutcome.choices, default=RouteOutcome.PENDING, db_index=True
    )
    served_model = models.ForeignKey(
        "models_registry.AIModel", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="routed_as_served",
    )
    #: 1 means the primary served. Anything higher means fallback happened.
    attempt = models.PositiveSmallIntegerField(null=True, blank=True)
    outcome_reported_at = models.DateTimeField(null=True, blank=True)
    outcome_detail = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "-created_at"]),
            models.Index(fields=["organization", "outcome"]),
        ]

    def __str__(self) -> str:
        target = self.selected_model.model_name if self.selected_model else "no route"
        return f"{self.model_type} -> {target}"

    @property
    def used_fallback(self) -> bool:
        return bool(self.attempt and self.attempt > 1)

    @property
    def has_route(self) -> bool:
        return bool(self.chain)

    @property
    def fallback_names(self) -> list[str]:
        return [entry.get("model_name", "") for entry in self.chain[1:]]
