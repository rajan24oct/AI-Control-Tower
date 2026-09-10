"""Human approval centre and rule engine (sections 19 and 20)."""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.db.models import Max
from django.utils import timezone

from apps.core.constants import DataClassification, Environment, RiskLevel
from apps.core.models import OrganizationOwnedModel, OrganizationScopedModel


class ApprovalStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    EXPIRED = "expired", "Expired"
    CANCELLED = "cancelled", "Cancelled"


DECIDED_STATUSES = [ApprovalStatus.APPROVED, ApprovalStatus.REJECTED]


class TriggerAction(models.TextChoices):
    """Sensitive actions an agent may attempt (section 20)."""

    EXTERNAL_COMMUNICATION = "external_communication", "External communication"
    FINANCIAL_TRANSACTION = "financial_transaction", "Financial transaction"
    PRODUCTION_DB_ACCESS = "production_database_access", "Production database access"
    DATA_EXPORT = "data_export", "Data export"
    ACCOUNT_MODIFICATION = "account_modification", "Account modification"
    CODE_EXECUTION = "code_execution", "Code execution"


class ApprovalRule(OrganizationScopedModel):
    """A configurable condition that forces human sign-off.

    Deliberately a fixed set of typed conditions rather than an expression
    language: section 20 calls for a simple rule engine in MVP, and a DSL is
    listed under "do not build yet".

    An empty condition means "do not check this". A rule matches when every
    condition it *does* specify is satisfied.
    """

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    priority = models.PositiveSmallIntegerField(
        default=100, help_text="Lower numbers are evaluated first."
    )

    min_autonomy_level = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Match agents at or above this autonomy level."
    )
    risk_levels = models.JSONField(default=list, blank=True)
    environments = models.JSONField(default=list, blank=True)
    data_classifications = models.JSONField(default=list, blank=True)
    tools = models.JSONField(
        default=list, blank=True, help_text="Requesting any of these tools triggers the rule."
    )
    actions = models.JSONField(default=list, blank=True)
    cost_threshold = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True,
        help_text="Match when estimated cost reaches this amount.",
    )
    match_restricted_tool = models.BooleanField(
        default=False, help_text="Match when the tool is on the agent's restricted list."
    )
    match_unapproved_provider = models.BooleanField(default=False)
    match_unapproved_model = models.BooleanField(default=False)

    severity = models.CharField(
        max_length=20, choices=RiskLevel.choices, default=RiskLevel.MODERATE
    )

    class Meta:
        ordering = ["priority", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"], name="uniq_approval_rule_name_per_org"
            )
        ]
        indexes = [models.Index(fields=["organization", "status", "priority"])]

    def __str__(self) -> str:
        return self.name

    @property
    def conditions(self) -> list[str]:
        """Human-readable summary, for the rule list and audit trail."""
        parts = []
        if self.min_autonomy_level is not None:
            parts.append(f"autonomy >= {self.min_autonomy_level}")
        if self.risk_levels:
            parts.append(f"risk in {', '.join(self.risk_levels)}")
        if self.environments:
            parts.append(f"environment in {', '.join(self.environments)}")
        if self.data_classifications:
            parts.append(f"data in {', '.join(self.data_classifications)}")
        if self.tools:
            parts.append(f"tool in {', '.join(self.tools)}")
        if self.actions:
            parts.append(f"action in {', '.join(self.actions)}")
        if self.cost_threshold is not None:
            parts.append(f"cost >= {self.cost_threshold}")
        if self.match_restricted_tool:
            parts.append("restricted tool requested")
        if self.match_unapproved_provider:
            parts.append("provider not approved")
        if self.match_unapproved_model:
            parts.append("model not approved")
        return parts


class ApprovalRequest(OrganizationOwnedModel):
    """A pause point where a human decides whether an agent may proceed."""

    request_id = models.CharField(max_length=20, db_index=True)

    execution = models.ForeignKey(
        "executions.Execution", null=True, blank=True, on_delete=models.CASCADE,
        related_name="approval_requests",
    )
    application = models.ForeignKey(
        "applications_registry.AIApplication", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="approval_requests",
    )
    agent = models.ForeignKey(
        "agents.AIAgent", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="approval_requests",
    )

    requested_action = models.CharField(max_length=255)
    reason = models.TextField(blank=True)
    risk_level = models.CharField(
        max_length=20, choices=RiskLevel.choices, default=RiskLevel.MODERATE, db_index=True
    )

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="raised_approval_requests",
    )
    requested_by_system = models.CharField(
        max_length=200, blank=True, help_text="External engine that raised the request."
    )
    requested_at = models.DateTimeField(default=timezone.now, db_index=True)

    assigned_approver = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="assigned_approval_requests",
    )
    status = models.CharField(
        max_length=20, choices=ApprovalStatus.choices, default=ApprovalStatus.PENDING, db_index=True
    )
    expires_at = models.DateTimeField(null=True, blank=True)

    decision_at = models.DateTimeField(null=True, blank=True)
    decision_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="approval_decisions",
    )
    decision_notes = models.TextField(blank=True)

    matched_rules = models.JSONField(
        default=list, blank=True, help_text="Rules that required this approval."
    )
    context = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-requested_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "request_id"], name="uniq_approval_request_id_per_org"
            )
        ]
        indexes = [
            models.Index(fields=["organization", "status", "-requested_at"]),
            models.Index(fields=["assigned_approver", "status"]),
            models.Index(fields=["organization", "risk_level"]),
        ]

    def __str__(self) -> str:
        return f"{self.request_id} {self.requested_action}"

    def save(self, *args, **kwargs):
        if not self.request_id:
            self.request_id = self._next_request_id()
        super().save(*args, **kwargs)

    @transaction.atomic
    def _next_request_id(self) -> str:
        existing = (
            ApprovalRequest.objects.filter(organization_id=self.organization_id)
            .exclude(request_id="")
            .aggregate(top=Max("request_id"))["top"]
        )
        next_number = 1
        if existing and existing.startswith("APR-"):
            try:
                next_number = int(existing.split("-")[1]) + 1
            except (IndexError, ValueError):
                next_number = (
                    ApprovalRequest.objects.filter(organization_id=self.organization_id).count() + 1
                )
        return f"APR-{next_number:04d}"

    # -- Derived state ------------------------------------------------------

    @property
    def is_pending(self) -> bool:
        return self.status == ApprovalStatus.PENDING

    @property
    def is_decided(self) -> bool:
        return self.status in DECIDED_STATUSES

    @property
    def is_expired(self) -> bool:
        return bool(
            self.status == ApprovalStatus.PENDING
            and self.expires_at
            and self.expires_at <= timezone.now()
        )

    @property
    def waiting_for(self) -> timezone.timedelta | None:
        if not self.is_pending:
            return None
        return timezone.now() - self.requested_at

    @property
    def decision_latency(self) -> timezone.timedelta | None:
        if not self.decision_at:
            return None
        return self.decision_at - self.requested_at


#: Seed rules created for a new organization, mirroring section 20's examples.
DEFAULT_RULES = [
    {
        "name": "High autonomy agents",
        "description": "Agents that can act on their own need a human in the loop.",
        "priority": 10,
        "min_autonomy_level": 3,
        "severity": RiskLevel.HIGH,
    },
    {
        "name": "Critical risk assets",
        "description": "Anything classified critical requires sign-off.",
        "priority": 20,
        "risk_levels": [RiskLevel.CRITICAL],
        "severity": RiskLevel.CRITICAL,
    },
    {
        "name": "Restricted tool requested",
        "description": "The agent asked for a tool on its own restricted list.",
        "priority": 30,
        "match_restricted_tool": True,
        "severity": RiskLevel.HIGH,
    },
    {
        "name": "Sensitive actions",
        "description": "External communication, payments, or production data access.",
        "priority": 40,
        "actions": [
            TriggerAction.EXTERNAL_COMMUNICATION,
            TriggerAction.FINANCIAL_TRANSACTION,
            TriggerAction.PRODUCTION_DB_ACCESS,
        ],
        "severity": RiskLevel.HIGH,
    },
    {
        "name": "Restricted data in production",
        "description": "Restricted data handled in a production environment.",
        "priority": 50,
        "data_classifications": [DataClassification.RESTRICTED],
        "environments": [Environment.PRODUCTION],
        "severity": RiskLevel.HIGH,
    },
    {
        "name": "Unapproved provider or model",
        "description": "Governance has not approved the provider or model in use.",
        "priority": 60,
        "match_unapproved_provider": True,
        "severity": RiskLevel.MODERATE,
    },
    {
        "name": "High cost operation",
        "description": "Estimated spend for a single run exceeds the threshold.",
        "priority": 70,
        "cost_threshold": Decimal("5.0000"),
        "severity": RiskLevel.MODERATE,
    },
]
