"""Model registry (section 10)."""

from __future__ import annotations

from decimal import Decimal

from django.db import models

from apps.core.constants import RiskLevel
from apps.core.models import OrganizationScopedModel


class ModelType(models.TextChoices):
    CHAT = "chat", "Chat / Completion"
    EMBEDDING = "embedding", "Embedding"
    RERANK = "rerank", "Rerank"
    VISION = "vision", "Vision"
    AUDIO = "audio", "Audio"
    IMAGE = "image", "Image Generation"
    MULTIMODAL = "multimodal", "Multimodal"
    CLASSIFICATION = "classification", "Classification"


class AIModel(OrganizationScopedModel):
    """A model version available to the organization through a provider."""

    provider = models.ForeignKey(
        "providers.AIProvider", on_delete=models.CASCADE, related_name="models"
    )

    model_name = models.CharField(max_length=200, help_text="Human-readable name.")
    model_id = models.CharField(
        max_length=200, help_text="Identifier passed to the provider API.", db_index=True
    )
    model_family = models.CharField(max_length=100, blank=True)
    model_type = models.CharField(
        max_length=40, choices=ModelType.choices, default=ModelType.CHAT, db_index=True
    )
    version = models.CharField(max_length=100, blank=True)

    context_window = models.PositiveIntegerField(null=True, blank=True)
    max_output_tokens = models.PositiveIntegerField(null=True, blank=True)

    # Rates per 1,000 tokens. Section 15 adds versioned pricing tables on top of
    # these; application logic reads pricing from the registry, never literals.
    input_cost_per_1k = models.DecimalField(
        max_digits=12, decimal_places=6, default=Decimal("0")
    )
    output_cost_per_1k = models.DecimalField(
        max_digits=12, decimal_places=6, default=Decimal("0")
    )
    currency = models.CharField(max_length=3, default="USD")

    capabilities = models.JSONField(default=list, blank=True)
    region = models.CharField(max_length=100, blank=True)
    deployment = models.CharField(max_length=200, blank=True)

    approved_for_use = models.BooleanField(default=False, db_index=True)
    risk_classification = models.CharField(
        max_length=20, choices=RiskLevel.choices, default=RiskLevel.LOW
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["provider__name", "model_name"]
        verbose_name = "AI model"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "provider", "model_id", "version"],
                name="uniq_model_per_provider_version",
            )
        ]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "approved_for_use"]),
            models.Index(fields=["provider", "model_type"]),
        ]

    def __str__(self) -> str:
        return f"{self.model_name} ({self.provider.name})"

    def supports(self, capability: str) -> bool:
        return str(capability) in set(self.capabilities or [])

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        """Cost for a given token split, using this model's registered rates."""
        return (
            Decimal(input_tokens) / Decimal(1000) * self.input_cost_per_1k
            + Decimal(output_tokens) / Decimal(1000) * self.output_cost_per_1k
        ).quantize(Decimal("0.000001"))

    @property
    def is_usable(self) -> bool:
        """Approved by governance and on an active provider."""
        return self.approved_for_use and self.is_active and self.provider.is_active
