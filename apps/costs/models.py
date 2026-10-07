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
    DEPARTMENT = "department", "Department"
    TEAM = "team", "Team"
    USER = "user", "Individual"
    APPLICATION = "application", "Application"
    AGENT = "agent", "Agent"
    PROVIDER = "provider", "Provider"
    MODEL = "model", "Model"


class CostSource(models.TextChoices):
    """Which spend a budget or alert rule counts.

    AI runtime is metered per execution; developer tools (GitHub Copilot and
    the like) are seat licences plus metered premium requests. They are
    different money with different owners, so a budget says which it means.
    """

    RUNTIME = "runtime", "AI runtime (agents and applications)"
    DEVELOPER_TOOLS = "developer_tools", "Developer tools (e.g. GitHub Copilot)"
    ALL = "all", "Both"

    @classmethod
    def counts_runtime(cls, value: str) -> bool:
        return value in (cls.RUNTIME, cls.ALL)

    @classmethod
    def counts_developer_tools(cls, value: str) -> bool:
        return value in (cls.DEVELOPER_TOOLS, cls.ALL)


class BudgetEnforcement(models.TextChoices):
    """What a budget does when it runs out.

    Advisory is the default and the pre-existing behaviour: alert, and let the
    spend happen. Hard stop is only meaningful where the platform is actually
    holding the request -- that is, gateway traffic -- and must be chosen
    deliberately, because it turns a finance control into an outage.
    """

    ADVISORY = "advisory", "Alert only"
    HARD_STOP = "hard_stop", "Refuse gateway requests"


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

    department = models.CharField(
        max_length=150, blank=True,
        help_text="As recorded on applications and teams, e.g. Claims.",
    )
    team = models.ForeignKey(
        "organizations.Team", null=True, blank=True, on_delete=models.CASCADE, related_name="budgets"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE,
        related_name="personal_budgets", verbose_name="individual",
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

    cost_source = models.CharField(
        max_length=20, choices=CostSource.choices, default=CostSource.RUNTIME,
        help_text="Which spend counts against the budget.",
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
    forecast_alerts = models.BooleanField(
        default=True,
        help_text="Also alert when spend so far projects past the budget by period end.",
    )
    notify_owner = models.BooleanField(
        default=True, help_text="Send this budget's alerts to its owner as well as to cost managers."
    )
    notify_team_lead = models.BooleanField(
        default=False, help_text="Team budgets: send alerts to the team's lead too."
    )
    extra_recipients = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="watched_budgets",
        help_text="Anyone else who should hear about this budget.",
    )
    enforcement = models.CharField(
        max_length=20,
        choices=BudgetEnforcement.choices,
        default=BudgetEnforcement.ADVISORY,
        help_text="Hard stop refuses gateway traffic once the budget is spent.",
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
            BudgetScope.DEPARTMENT: self.department or None,
            BudgetScope.TEAM: self.team,
            BudgetScope.USER: self.user,
            BudgetScope.APPLICATION: self.application,
            BudgetScope.AGENT: self.agent,
            BudgetScope.PROVIDER: self.provider,
            BudgetScope.MODEL: self.model,
        }.get(self.scope)

    @property
    def scope_label(self) -> str:
        target = self.scope_target
        if self.scope == BudgetScope.USER and target is not None:
            return target.get_full_name() or target.email
        return str(target) if target else self.organization.name

    @property
    def counts_runtime(self) -> bool:
        return CostSource.counts_runtime(self.cost_source)

    @property
    def counts_developer_tools(self) -> bool:
        return CostSource.counts_developer_tools(self.cost_source)

    def alert_recipients(self) -> list:
        """People this budget's alerts go to beyond the cost managers."""
        people = list(self.extra_recipients.filter(is_active=True))
        if self.notify_owner and self.owner_id:
            people.append(self.owner)
        if self.notify_team_lead and self.team_id and self.team.lead_id:
            people.append(self.team.lead)
        return people

    @property
    def is_enforcing(self) -> bool:
        return self.enforcement == BudgetEnforcement.HARD_STOP

    def applies_to_agent(self, agent) -> bool:
        """Whether a request from `agent` is covered by this budget.

        Organization-wide budgets cover everything. Narrower scopes only bite
        when the request can be attributed to the thing they are scoped to; an
        unattributable request is not held against someone else's budget.
        """
        if not self.counts_runtime:
            return False  # a developer-tools budget never holds gateway traffic
        if self.scope == BudgetScope.ORGANIZATION:
            return True
        if agent is None or self.scope == BudgetScope.USER:
            # The gateway attributes a request to an agent, not to a person.
            return False
        if self.scope == BudgetScope.DEPARTMENT:
            department = (self.department or "").strip().lower()
            if not department:
                return False
            if agent.team_id and (agent.team.department or "").strip().lower() == department:
                return True
            return agent.applications.filter(department__iexact=department).exists()
        if self.scope == BudgetScope.AGENT:
            return self.agent_id == agent.pk
        if self.scope == BudgetScope.TEAM:
            return bool(self.team_id) and self.team_id == agent.team_id
        if self.scope == BudgetScope.PROVIDER:
            return bool(self.provider_id) and self.provider_id == agent.provider_id
        if self.scope == BudgetScope.MODEL:
            return bool(self.model_id) and self.model_id == agent.primary_model_id
        if self.scope == BudgetScope.APPLICATION:
            return agent.applications.filter(pk=self.application_id).exists()
        return False

    def is_exhausted(self) -> bool:
        from apps.costs.services import BudgetService

        return bool(BudgetService.status_for(self)["is_over"])


class BudgetAlertKind(models.TextChoices):
    THRESHOLD = "threshold", "Threshold crossed"
    #: Spend so far, projected to period end, passes the budget. Raised once
    #: per period, before the money is actually spent.
    FORECAST = "forecast", "Forecast to exceed"


class BudgetAlert(OrganizationOwnedModel):
    """A threshold crossing. One row per budget, period and threshold, so a
    budget that stays over 90% does not alert on every check.
    """

    budget = models.ForeignKey(Budget, on_delete=models.CASCADE, related_name="alerts")
    kind = models.CharField(
        max_length=20, choices=BudgetAlertKind.choices, default=BudgetAlertKind.THRESHOLD
    )
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
                fields=["budget", "period_start", "kind", "threshold"],
                name="uniq_budget_alert_per_period_kind_threshold",
            )
        ]
        indexes = [models.Index(fields=["organization", "-created_at"])]

    def __str__(self) -> str:
        if self.is_forecast:
            return f"{self.budget.name} forecast past budget"
        return f"{self.budget.name} at {self.threshold}%"

    @property
    def is_acknowledged(self) -> bool:
        return self.acknowledged_at is not None

    @property
    def is_forecast(self) -> bool:
        return self.kind == BudgetAlertKind.FORECAST

    @property
    def severity(self) -> str:
        if self.is_forecast:
            return "high"
        if self.threshold >= 100:
            return "critical"
        if self.threshold >= 90:
            return "high"
        return "medium"


# -- Cost alert rules ------------------------------------------------------------


class AlertMetric(models.TextChoices):
    SPEND = "spend", "Spend in the window"
    SPEND_INCREASE = "spend_increase", "Spend increase vs. the previous windows (%)"
    COST_PER_RUN = "cost_per_run", "Average cost per run"
    FAILED_SPEND = "failed_spend", "Spend on failed runs"
    TOKENS = "tokens", "Tokens in the window"


class AlertWindow(models.IntegerChoices):
    HOUR = 1, "Last hour"
    DAY = 24, "Last 24 hours"
    WEEK = 168, "Last 7 days"


class AlertSeverity(models.TextChoices):
    """Matches the notification severities, so an event maps straight onto one."""

    INFO = "info", "Info"
    WARNING = "warning", "Warning"
    CRITICAL = "critical", "Critical"


class CostAlertRule(OrganizationScopedModel):
    """A guardrail on how spend behaves, independent of any budget.

    A budget answers "how much may we spend this month". A rule answers
    "tell me when something looks wrong now" -- a spike against the recent
    baseline, a run that suddenly costs ten times the norm, money burnt on
    failures. Rules count AI runtime spend only: developer-tool licences do
    not move by the hour.
    """

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    scope = models.CharField(
        max_length=20, choices=BudgetScope.choices, default=BudgetScope.ORGANIZATION
    )
    department = models.CharField(max_length=150, blank=True)
    team = models.ForeignKey(
        "organizations.Team", null=True, blank=True, on_delete=models.CASCADE,
        related_name="cost_alert_rules",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE,
        related_name="cost_alert_rules", verbose_name="individual",
    )
    application = models.ForeignKey(
        "applications_registry.AIApplication", null=True, blank=True,
        on_delete=models.CASCADE, related_name="cost_alert_rules",
    )
    agent = models.ForeignKey(
        "agents.AIAgent", null=True, blank=True, on_delete=models.CASCADE,
        related_name="cost_alert_rules",
    )
    provider = models.ForeignKey(
        "providers.AIProvider", null=True, blank=True, on_delete=models.CASCADE,
        related_name="cost_alert_rules",
    )
    model = models.ForeignKey(
        "models_registry.AIModel", null=True, blank=True, on_delete=models.CASCADE,
        related_name="cost_alert_rules",
    )

    metric = models.CharField(max_length=20, choices=AlertMetric.choices, default=AlertMetric.SPEND)
    window_hours = models.PositiveIntegerField(
        choices=AlertWindow.choices, default=AlertWindow.DAY, verbose_name="window"
    )
    threshold = models.DecimalField(
        max_digits=14, decimal_places=4,
        help_text="Alert when the metric reaches this value. A percentage for spend increase.",
    )
    minimum_spend = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0"),
        help_text="Ignore windows with less spend than this, so small numbers do not cause noise.",
    )
    currency = models.CharField(max_length=3, default="USD")
    severity = models.CharField(
        max_length=20, choices=AlertSeverity.choices, default=AlertSeverity.WARNING
    )
    cooldown_hours = models.PositiveIntegerField(
        default=24, help_text="After it fires, the rule stays quiet for this long."
    )

    notify_cost_managers = models.BooleanField(
        default=True, help_text="Everyone whose role grants cost.manage."
    )
    recipients = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="cost_alert_subscriptions",
        help_text="Anyone else who should hear about it.",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="owned_cost_alert_rules",
    )

    last_evaluated_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_value = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True, editable=False
    )
    last_triggered_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"], name="uniq_cost_alert_rule_name_per_org"
            )
        ]

    def __str__(self) -> str:
        return self.name

    # The same scope vocabulary as budgets, so the same counting applies.
    cost_source = CostSource.RUNTIME
    scope_target = Budget.scope_target
    scope_label = Budget.scope_label

    @property
    def is_percentage(self) -> bool:
        return self.metric == AlertMetric.SPEND_INCREASE

    @property
    def is_monetary(self) -> bool:
        return self.metric in (AlertMetric.SPEND, AlertMetric.COST_PER_RUN, AlertMetric.FAILED_SPEND)

    def in_cooldown(self, now=None) -> bool:
        if not self.last_triggered_at:
            return False
        now = now or timezone.now()
        return (now - self.last_triggered_at).total_seconds() < self.cooldown_hours * 3600


class CostAlertEvent(OrganizationOwnedModel):
    """One firing of a cost alert rule."""

    rule = models.ForeignKey(CostAlertRule, on_delete=models.CASCADE, related_name="events")
    severity = models.CharField(max_length=20, choices=AlertSeverity.choices)
    value = models.DecimalField(max_digits=18, decimal_places=4)
    threshold = models.DecimalField(max_digits=14, decimal_places=4)
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    summary = models.CharField(max_length=500)
    details = models.JSONField(default=dict, blank=True)

    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="acknowledged_cost_alert_events",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "-created_at"])]

    def __str__(self) -> str:
        return self.summary

    @property
    def is_acknowledged(self) -> bool:
        return self.acknowledged_at is not None


# -- Developer tools (GitHub Copilot) ----------------------------------------------


class DevToolPlatform(models.TextChoices):
    GITHUB_COPILOT = "github_copilot", "GitHub Copilot"


class SeatPlan(models.TextChoices):
    BUSINESS = "business", "Business"
    ENTERPRISE = "enterprise", "Enterprise"


class DevToolSubscription(OrganizationScopedModel):
    """An organization's licence for an AI developer tool.

    Copilot is billed differently from runtime AI: a monthly price per seat,
    plus metered "premium requests" beyond what each seat includes. The rates
    live here, editable, never as literals in the cost logic -- the defaults
    are GitHub's published list prices and an enterprise agreement will
    differ.
    """

    name = models.CharField(max_length=200)
    platform = models.CharField(
        max_length=40, choices=DevToolPlatform.choices, default=DevToolPlatform.GITHUB_COPILOT
    )
    adapter_key = models.CharField(
        max_length=64, default="github_copilot",
        help_text="The integration that reads seats and usage. 'mock_github_copilot' for demos.",
    )
    account = models.CharField(
        max_length=200, help_text="The GitHub organization login, e.g. northwind-financial."
    )
    endpoint = models.URLField(
        blank=True, default="https://api.github.com",
        help_text="GitHub API base URL; a GitHub Enterprise Server's /api/v3 address if self-hosted.",
    )
    credential_ref = models.CharField(
        max_length=200, blank=True,
        help_text="Environment variable holding a token that can read Copilot seats and billing.",
    )
    options = models.JSONField(
        default=dict, blank=True,
        help_text='Adapter options, e.g. {"login_map": {"octocat": "ava@example.com"}, "max_usage_calls": 2000}.',
    )

    currency = models.CharField(max_length=3, default="USD")
    business_seat_price = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("19.00"),
        help_text="Monthly price of a Business seat.",
    )
    enterprise_seat_price = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("39.00"),
        help_text="Monthly price of an Enterprise seat.",
    )
    business_included_requests = models.PositiveIntegerField(
        default=300, help_text="Premium requests a Business seat includes each month."
    )
    enterprise_included_requests = models.PositiveIntegerField(
        default=1000, help_text="Premium requests an Enterprise seat includes each month."
    )
    premium_request_price = models.DecimalField(
        max_digits=10, decimal_places=4, default=Decimal("0.04"),
        help_text="Price of each premium request beyond the included allowance.",
    )

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="owned_devtool_subscriptions",
    )
    last_synced_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_sync_message = models.CharField(max_length=500, blank=True, editable=False)

    class Meta:
        ordering = ["name"]
        verbose_name = "developer tool subscription"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"], name="uniq_devtool_subscription_name_per_org"
            )
        ]

    def __str__(self) -> str:
        return self.name

    def seat_price(self, plan: str) -> Decimal:
        return self.enterprise_seat_price if plan == SeatPlan.ENTERPRISE else self.business_seat_price

    def included_requests(self, plan: str) -> int:
        if plan == SeatPlan.ENTERPRISE:
            return self.enterprise_included_requests
        return self.business_included_requests


class DevToolSeat(OrganizationOwnedModel):
    """One person's seat. Attributed to a platform user and a team where known,
    so spend rolls up to the team and down to the individual.
    """

    subscription = models.ForeignKey(
        DevToolSubscription, on_delete=models.CASCADE, related_name="seats"
    )
    login = models.CharField(max_length=200, help_text="The GitHub login holding the seat.")
    display_name = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="devtool_seats", help_text="The platform user, where the login maps to one.",
    )
    team = models.ForeignKey(
        "organizations.Team", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="devtool_seats",
    )
    plan = models.CharField(max_length=20, choices=SeatPlan.choices, default=SeatPlan.BUSINESS)
    assigning_team = models.CharField(
        max_length=200, blank=True, help_text="The GitHub team that granted the seat, if any."
    )
    assigned_on = models.DateField()
    pending_cancellation_on = models.DateField(null=True, blank=True)
    last_activity_at = models.DateTimeField(null=True, blank=True)
    last_activity_editor = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(default=True, help_text="False once the seat is removed.")
    removed_on = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["login"]
        verbose_name = "developer tool seat"
        constraints = [
            models.UniqueConstraint(fields=["subscription", "login"], name="uniq_devtool_seat_login")
        ]
        indexes = [models.Index(fields=["organization", "team"])]

    def __str__(self) -> str:
        return self.login

    @property
    def person_label(self) -> str:
        if self.user_id:
            return self.user.get_full_name() or self.user.email
        return self.display_name or self.login

    @property
    def monthly_price(self) -> Decimal:
        return self.subscription.seat_price(self.plan)

    @property
    def included_requests(self) -> int:
        return self.subscription.included_requests(self.plan)

    def idle_days(self, now=None) -> int | None:
        """Days since the seat was last used; None if it has never been used."""
        if not self.last_activity_at:
            return None
        now = now or timezone.now()
        return max((now - self.last_activity_at).days, 0)


class DevToolUsage(OrganizationOwnedModel):
    """Premium requests one seat made on one day."""

    seat = models.ForeignKey(DevToolSeat, on_delete=models.CASCADE, related_name="usage")
    date = models.DateField(db_index=True)
    premium_requests = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0"),
        help_text="Fractional: some models count a request as less, or more, than one.",
    )
    by_model = models.JSONField(default=dict, blank=True, help_text="Premium requests per model.")

    class Meta:
        ordering = ["-date"]
        verbose_name = "developer tool usage"
        verbose_name_plural = "developer tool usage"
        constraints = [
            models.UniqueConstraint(fields=["seat", "date"], name="uniq_devtool_usage_seat_day")
        ]
        indexes = [models.Index(fields=["organization", "date"])]

    def __str__(self) -> str:
        return f"{self.seat.login} {self.date}"


# -- Optimization recommendations --------------------------------------------------


class RecommendationKind(models.TextChoices):
    MODEL_RIGHTSIZING = "model_rightsizing", "Right-size the model"
    PROMPT_CACHING = "prompt_caching", "Cache repeated prompt input"
    OUTPUT_LENGTH = "output_length", "Cap verbose output"
    FAILURE_WASTE = "failure_waste", "Stop paying for failed runs"
    IDLE_SEATS = "idle_seats", "Reclaim idle Copilot seats"
    SEAT_DOWNGRADE = "seat_downgrade", "Downgrade under-used Copilot seats"
    SEAT_UPGRADE = "seat_upgrade", "Upgrade heavy Copilot users"
    UNBUDGETED_SPEND = "unbudgeted_spend", "Put a budget on unbudgeted spend"


class RecommendationCategory(models.TextChoices):
    RUNTIME = "runtime", "AI runtime"
    DEVELOPER_TOOLS = "developer_tools", "Developer tools"
    GOVERNANCE = "governance", "Governance"


KIND_CATEGORIES = {
    RecommendationKind.MODEL_RIGHTSIZING: RecommendationCategory.RUNTIME,
    RecommendationKind.PROMPT_CACHING: RecommendationCategory.RUNTIME,
    RecommendationKind.OUTPUT_LENGTH: RecommendationCategory.RUNTIME,
    RecommendationKind.FAILURE_WASTE: RecommendationCategory.RUNTIME,
    RecommendationKind.IDLE_SEATS: RecommendationCategory.DEVELOPER_TOOLS,
    RecommendationKind.SEAT_DOWNGRADE: RecommendationCategory.DEVELOPER_TOOLS,
    RecommendationKind.SEAT_UPGRADE: RecommendationCategory.DEVELOPER_TOOLS,
    RecommendationKind.UNBUDGETED_SPEND: RecommendationCategory.GOVERNANCE,
}


class Confidence(models.TextChoices):
    HIGH = "high", "High"
    MEDIUM = "medium", "Medium"
    LOW = "low", "Low"


class Effort(models.TextChoices):
    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"


class RecommendationStatus(models.TextChoices):
    OPEN = "open", "Open"
    ACCEPTED = "accepted", "Accepted"
    IMPLEMENTED = "implemented", "Implemented"
    DISMISSED = "dismissed", "Dismissed"
    #: The analysis no longer finds the condition -- fixed, or traffic moved.
    RESOLVED = "resolved", "No longer applies"


class CostRecommendation(OrganizationOwnedModel):
    """A specific, costed suggestion to spend less.

    Identified by a fingerprint (kind plus target), so re-running the analysis
    refreshes the figures on an existing recommendation instead of creating a
    duplicate -- and a dismissal sticks.
    """

    kind = models.CharField(max_length=30, choices=RecommendationKind.choices, db_index=True)
    category = models.CharField(max_length=20, choices=RecommendationCategory.choices, db_index=True)
    fingerprint = models.CharField(max_length=200)
    title = models.CharField(max_length=300)
    rationale = models.TextField(help_text="What the analysis saw.")
    action = models.TextField(help_text="What to do about it.")

    estimated_monthly_savings = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0")
    )
    current_monthly_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    currency = models.CharField(max_length=3, default="USD")
    confidence = models.CharField(max_length=10, choices=Confidence.choices, default=Confidence.MEDIUM)
    effort = models.CharField(max_length=10, choices=Effort.choices, default=Effort.MEDIUM)
    evidence = models.JSONField(default=dict, blank=True)

    agent = models.ForeignKey(
        "agents.AIAgent", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="cost_recommendations",
    )
    model = models.ForeignKey(
        "models_registry.AIModel", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="cost_recommendations",
    )
    suggested_model = models.ForeignKey(
        "models_registry.AIModel", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="suggested_in_recommendations",
    )
    team = models.ForeignKey(
        "organizations.Team", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="cost_recommendations",
    )
    subscription = models.ForeignKey(
        DevToolSubscription, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="recommendations",
    )

    status = models.CharField(
        max_length=20, choices=RecommendationStatus.choices, default=RecommendationStatus.OPEN,
        db_index=True,
    )
    first_detected_at = models.DateTimeField(default=timezone.now)
    last_detected_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="decided_cost_recommendations",
    )
    decision_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-estimated_monthly_savings", "title"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "fingerprint"], name="uniq_cost_recommendation_fingerprint"
            )
        ]
        indexes = [models.Index(fields=["organization", "status"])]

    def __str__(self) -> str:
        return self.title

    @property
    def is_actionable(self) -> bool:
        return self.status in (RecommendationStatus.OPEN, RecommendationStatus.ACCEPTED)

    @property
    def annual_savings(self) -> Decimal:
        return self.estimated_monthly_savings * 12
