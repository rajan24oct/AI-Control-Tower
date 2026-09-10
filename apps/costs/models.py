"""FinOps: versioned pricing and budgets (sections 15 and 16).

Pricing is versioned and dated rather than stored as a single current rate.
Providers change prices, and a cost figure for last month must still be
explainable using the rates that applied then -- so cost calculation always
resolves the pricing version covering the execution's own timestamp.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.core.constants import LifecycleStatus
from apps.core.models import BaseModel, OrganizationOwnedModel, OrganizationScopedModel


class PricingVersion(OrganizationScopedModel):
    """A dated set of rates. Overlapping windows for one organization are
    allowed only if the newer one supersedes -- resolution takes the latest
    `effective_from` that covers the moment in question.
    """

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    currency = models.CharField(max_length=3, default="USD")
    effective_from = models.DateTimeField(db_index=True)
    effective_to = models.DateTimeField(
        null=True, blank=True, help_text="Leave empty for the version currently in force."
    )
    source = models.CharField(
        max_length=200, blank=True, help_text="Where these rates came from, e.g. a vendor price list."
    )

    class Meta:
        ordering = ["-effective_from"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"], name="uniq_pricing_version_name_per_org"
            )
        ]
        indexes = [models.Index(fields=["organization", "-effective_from"])]

    def __str__(self) -> str:
        return self.name

    def clean(self):
        if self.effective_to and self.effective_to <= self.effective_from:
            raise ValidationError({"effective_to": "Must be later than effective_from."})

    def covers(self, moment) -> bool:
        if moment < self.effective_from:
            return False
        return self.effective_to is None or moment < self.effective_to


class ModelPricing(BaseModel):
    """Per-1,000-token rates for one model under one pricing version."""

    version = models.ForeignKey(
        PricingVersion, on_delete=models.CASCADE, related_name="model_prices"
    )
    model = models.ForeignKey(
        "models_registry.AIModel", on_delete=models.CASCADE, related_name="pricing_entries"
    )

    input_cost_per_1k = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"))
    output_cost_per_1k = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"))
    cached_input_cost_per_1k = models.DecimalField(
        max_digits=12, decimal_places=6, null=True, blank=True
    )
    currency = models.CharField(max_length=3, default="USD")

    class Meta:
        ordering = ["model__model_name"]
        verbose_name = "model pricing"
        verbose_name_plural = "model pricing"
        constraints = [
            models.UniqueConstraint(fields=["version", "model"], name="uniq_model_price_per_version")
        ]

    def __str__(self) -> str:
        return f"{self.model.model_name} @ {self.version.name}"

    def cost_for(self, input_tokens: int, output_tokens: int) -> Decimal:
        return (
            Decimal(input_tokens) / Decimal(1000) * self.input_cost_per_1k
            + Decimal(output_tokens) / Decimal(1000) * self.output_cost_per_1k
        ).quantize(Decimal("0.000001"))


class ProviderPricing(BaseModel):
    """Provider-level charges that are not per-token.

    Kept separate from model rates because they apply per request or per tool
    call regardless of which model was used.
    """

    version = models.ForeignKey(
        PricingVersion, on_delete=models.CASCADE, related_name="provider_prices"
    )
    provider = models.ForeignKey(
        "providers.AIProvider", on_delete=models.CASCADE, related_name="pricing_entries"
    )

    request_fee = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"))
    tool_execution_fee = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"))
    currency = models.CharField(max_length=3, default="USD")

    class Meta:
        ordering = ["provider__name"]
        verbose_name = "provider pricing"
        verbose_name_plural = "provider pricing"
        constraints = [
            models.UniqueConstraint(
                fields=["version", "provider"], name="uniq_provider_price_per_version"
            )
        ]

    def __str__(self) -> str:
        return f"{self.provider.name} @ {self.version.name}"


class DailyUsageRollup(OrganizationOwnedModel):
    """Pre-aggregated daily spend (sections 14 and 24).

    Trend queries read this instead of scanning raw executions. Rebuilt by a
    scheduled task, and always reproducible from the executions it summarizes.
    """

    date = models.DateField(db_index=True)
    application = models.ForeignKey(
        "applications_registry.AIApplication", null=True, blank=True,
        on_delete=models.CASCADE, related_name="daily_rollups",
    )
    agent = models.ForeignKey(
        "agents.AIAgent", null=True, blank=True, on_delete=models.CASCADE,
        related_name="daily_rollups",
    )
    provider = models.ForeignKey(
        "providers.AIProvider", null=True, blank=True, on_delete=models.CASCADE,
        related_name="daily_rollups",
    )
    model = models.ForeignKey(
        "models_registry.AIModel", null=True, blank=True, on_delete=models.CASCADE,
        related_name="daily_rollups",
    )

    executions = models.PositiveIntegerField(default=0)
    successful_executions = models.PositiveIntegerField(default=0)
    failed_executions = models.PositiveIntegerField(default=0)
    input_tokens = models.BigIntegerField(default=0)
    output_tokens = models.BigIntegerField(default=0)
    total_tokens = models.BigIntegerField(default=0)
    cost = models.DecimalField(max_digits=16, decimal_places=6, default=Decimal("0"))
    currency = models.CharField(max_length=3, default="USD")

    class Meta:
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "date", "application", "agent", "provider", "model"],
                name="uniq_daily_rollup_slice",
            )
        ]
        indexes = [models.Index(fields=["organization", "-date"])]

    def __str__(self) -> str:
        return f"{self.date} rollup"


class BudgetScope(models.TextChoices):
    ORGANIZATION = "organization", "Organization"
    TEAM = "team", "Team"
    APPLICATION = "application", "Application"
    AGENT = "agent", "Agent"
    PROVIDER = "provider", "Provider"
    MODEL = "model", "Model"


class BudgetPeriod(models.TextChoices):
    DAILY = "daily", "Daily"
    WEEKLY = "weekly", "Weekly"
    MONTHLY = "monthly", "Monthly"
    QUARTERLY = "quarterly", "Quarterly"
    ANNUAL = "annual", "Annual"


DEFAULT_THRESHOLDS = [50, 75, 90, 100]


class Budget(OrganizationScopedModel):
    """A spend limit for one slice of the estate.

    The scope is expressed as real foreign keys rather than a generic
    (type, id) pair, so "spend against this budget" stays a single indexed
    query instead of a lookup through a polymorphic column.
    """

    name = models.CharField(max_length=200)
    scope = models.CharField(
        max_length=20, choices=BudgetScope.choices, default=BudgetScope.ORGANIZATION
    )

    team = models.ForeignKey(
        "organizations.Team", null=True, blank=True, on_delete=models.CASCADE, related_name="budgets"
    )
    application = models.ForeignKey(
        "applications_registry.AIApplication", null=True, blank=True,
        on_delete=models.CASCADE, related_name="budgets",
    )
    agent = models.ForeignKey(
        "agents.AIAgent", null=True, blank=True, on_delete=models.CASCADE, related_name="budgets"
    )
    provider = models.ForeignKey(
        "providers.AIProvider", null=True, blank=True, on_delete=models.CASCADE,
        related_name="budgets",
    )
    model = models.ForeignKey(
        "models_registry.AIModel", null=True, blank=True, on_delete=models.CASCADE,
        related_name="budgets",
    )

    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    period = models.CharField(
        max_length=20, choices=BudgetPeriod.choices, default=BudgetPeriod.MONTHLY
    )
    starts_on = models.DateField(default=timezone.now)

    alert_thresholds = models.JSONField(
        default=list, blank=True, help_text="Percentages that raise an alert, e.g. [50, 75, 90, 100]."
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="owned_budgets",
    )

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="uniq_budget_name_per_org")
        ]
        indexes = [models.Index(fields=["organization", "status"])]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.alert_thresholds:
            self.alert_thresholds = list(DEFAULT_THRESHOLDS)
        super().save(*args, **kwargs)

    @property
    def thresholds(self) -> list[int]:
        return sorted(int(t) for t in (self.alert_thresholds or DEFAULT_THRESHOLDS))

    @property
    def scope_target(self):
        """The record this budget applies to, or None for organization-wide."""
        return {
            BudgetScope.TEAM: self.team,
            BudgetScope.APPLICATION: self.application,
            BudgetScope.AGENT: self.agent,
            BudgetScope.PROVIDER: self.provider,
            BudgetScope.MODEL: self.model,
        }.get(self.scope)

    @property
    def scope_label(self) -> str:
        target = self.scope_target
        return str(target) if target else self.organization.name


class BudgetAlert(OrganizationOwnedModel):
    """A threshold crossing. One row per budget, period and threshold, so a
    budget that stays over 90% does not alert on every check.
    """

    budget = models.ForeignKey(Budget, on_delete=models.CASCADE, related_name="alerts")
    threshold = models.PositiveIntegerField()
    period_start = models.DateField()
    period_end = models.DateField()

    spend = models.DecimalField(max_digits=14, decimal_places=6, default=Decimal("0"))
    budget_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    percentage = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0"))
    currency = models.CharField(max_length=3, default="USD")

    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="acknowledged_budget_alerts",
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["budget", "period_start", "threshold"],
                name="uniq_budget_alert_per_period_threshold",
            )
        ]
        indexes = [models.Index(fields=["organization", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.budget.name} at {self.threshold}%"

    @property
    def is_acknowledged(self) -> bool:
        return self.acknowledged_at is not None

    @property
    def severity(self) -> str:
        if self.threshold >= 100:
            return "critical"
        if self.threshold >= 90:
            return "high"
        return "medium"
