"""Application registry (section 9)."""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.text import slugify

from apps.core.constants import Criticality, DataClassification, Environment, RiskLevel
from apps.core.models import OrganizationScopedModel

#: IRGF chapter 16: does an invoked agent act within its own boundary, or with
#: its caller's authority -- the case where authority compounds?
DELEGATION_AUTHORITY = {
    "own": "Within its own boundary",
    "caller": "With the caller's authority",
}


def validate_delegations(value) -> None:
    """A delegation graph: [{"from": agent_id, "to": agent_id, "authority": "own" | "caller"}]."""
    if value in (None, "", []):
        return
    if not isinstance(value, list):
        raise ValidationError("Delegations must be a list of {from, to, authority}.")
    for edge in value:
        if not isinstance(edge, dict) or not edge.get("from") or not edge.get("to"):
            raise ValidationError("Each delegation names the agent id it goes from and the one it goes to.")
        unknown = sorted(set(edge) - {"from", "to", "authority"})
        if unknown:
            raise ValidationError(f"Unknown delegation key(s): {', '.join(unknown)}.")
        if edge["from"] == edge["to"]:
            raise ValidationError("An agent does not delegate to itself.")
        if edge.get("authority", "own") not in DELEGATION_AUTHORITY:
            raise ValidationError("authority is 'own' or 'caller'.")


class AIApplication(OrganizationScopedModel):
    """A business-facing application that uses AI.

    This is the top of the inventory hierarchy: business users -> applications
    -> agents -> models -> providers.
    """

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    business_purpose = models.TextField(blank=True)

    business_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="business_owned_applications",
    )
    technical_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="technically_owned_applications",
    )
    team = models.ForeignKey(
        "organizations.Team",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="applications",
    )
    department = models.CharField(max_length=150, blank=True)

    environment = models.CharField(
        max_length=20, choices=Environment.choices, default=Environment.DEVELOPMENT, db_index=True
    )
    criticality = models.CharField(
        max_length=20, choices=Criticality.choices, default=Criticality.MEDIUM, db_index=True
    )
    data_classification = models.CharField(
        max_length=20, choices=DataClassification.choices, default=DataClassification.INTERNAL
    )
    risk_level = models.CharField(
        max_length=20, choices=RiskLevel.choices, default=RiskLevel.LOW, db_index=True
    )

    production_date = models.DateField(null=True, blank=True)
    review_date = models.DateField(null=True, blank=True)

    # IRGF risk tier (M5), cached from the current S2 classification. Written
    # only by the classification service.
    risk_tier = models.PositiveSmallIntegerField(null=True, blank=True, editable=False, db_index=True)

    # Relationships that make the inventory navigable in both directions.
    ai_models = models.ManyToManyField(
        "models_registry.AIModel", blank=True, related_name="applications"
    )
    agents = models.ManyToManyField("agents.AIAgent", blank=True, related_name="applications")
    providers = models.ManyToManyField(
        "providers.AIProvider", blank=True, related_name="applications"
    )
    tools = models.JSONField(
        default=list, blank=True, help_text="Tool identifiers until the tool registry lands."
    )
    #: IRGF chapter 16: which of its agents may invoke which others, and with
    #: whose authority. Declared, not inferred from traffic.
    delegations = models.JSONField(
        default=list, blank=True, validators=[validate_delegations],
        help_text='Which agents may invoke which: [{"from": agent_id, "to": agent_id, "authority": "own" or "caller"}].',
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "AI application"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "slug"], name="uniq_application_slug_per_org"
            )
        ]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "criticality"]),
            models.Index(fields=["organization", "environment"]),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:200]
        super().save(*args, **kwargs)

    @property
    def is_production(self) -> bool:
        return self.environment == Environment.PRODUCTION

    @property
    def needs_review(self) -> bool:
        from django.utils import timezone

        return bool(self.review_date and self.review_date <= timezone.now().date())
