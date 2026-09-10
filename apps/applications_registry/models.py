"""Application registry (section 9)."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.text import slugify

from apps.core.constants import Criticality, DataClassification, Environment, RiskLevel
from apps.core.models import OrganizationScopedModel


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
