"""AI risk register (section 17)."""

from __future__ import annotations

from django.conf import settings
from django.db import models, transaction
from django.db.models import Max

from apps.core.constants import RiskLevel
from apps.core.models import OrganizationOwnedModel


class RiskCategory(models.TextChoices):
    BIAS = "bias", "Bias"
    HALLUCINATION = "hallucination", "Hallucination"
    PRIVACY = "privacy", "Privacy"
    SECURITY = "security", "Security"
    DATA_LEAKAGE = "data_leakage", "Data Leakage"
    COMPLIANCE = "compliance", "Compliance"
    ETHICS = "ethics", "Ethics"
    AVAILABILITY = "availability", "Availability"
    MODEL_FAILURE = "model_failure", "Model Failure"
    PROVIDER_DEPENDENCY = "provider_dependency", "Provider Dependency"
    COST = "cost", "Cost"
    OPERATIONAL = "operational", "Operational"
    REPUTATIONAL = "reputational", "Reputational"
    AUTONOMOUS_AGENT = "autonomous_agent", "Autonomous Agent Risk"


class RiskStatus(models.TextChoices):
    IDENTIFIED = "identified", "Identified"
    ASSESSED = "assessed", "Assessed"
    MITIGATING = "mitigating", "Mitigating"
    MONITORING = "monitoring", "Monitoring"
    ACCEPTED = "accepted", "Accepted"
    CLOSED = "closed", "Closed"


OPEN_STATUSES = [
    RiskStatus.IDENTIFIED,
    RiskStatus.ASSESSED,
    RiskStatus.MITIGATING,
    RiskStatus.MONITORING,
]

SCALE = [1, 2, 3, 4, 5]
SCALE_CHOICES = [
    (1, "1 - Very Low"),
    (2, "2 - Low"),
    (3, "3 - Medium"),
    (4, "4 - High"),
    (5, "5 - Very High"),
]


def level_for_score(score: int | None) -> str:
    """Map a 1-25 score onto a level using the configured thresholds.

    Thresholds live in settings so an organization can retune its appetite
    without a migration (section 17).
    """
    if not score:
        return RiskLevel.LOW

    thresholds = settings.CONTROL_TOWER["RISK_THRESHOLDS"]
    if score <= thresholds["LOW"]:
        return RiskLevel.LOW
    if score <= thresholds["MODERATE"]:
        return RiskLevel.MODERATE
    if score <= thresholds["HIGH"]:
        return RiskLevel.HIGH
    return RiskLevel.CRITICAL


class Risk(OrganizationOwnedModel):
    """One identified AI risk, scored before and after mitigation."""

    risk_id = models.CharField(max_length=20, db_index=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    risk_category = models.CharField(
        max_length=40, choices=RiskCategory.choices, default=RiskCategory.OPERATIONAL, db_index=True
    )

    application = models.ForeignKey(
        "applications_registry.AIApplication", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="risks",
    )
    agent = models.ForeignKey(
        "agents.AIAgent", null=True, blank=True, on_delete=models.SET_NULL, related_name="risks"
    )
    model = models.ForeignKey(
        "models_registry.AIModel", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="risks",
    )
    provider = models.ForeignKey(
        "providers.AIProvider", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="risks",
    )

    likelihood = models.PositiveSmallIntegerField(choices=SCALE_CHOICES, default=3)
    impact = models.PositiveSmallIntegerField(choices=SCALE_CHOICES, default=3)
    inherent_risk_score = models.PositiveSmallIntegerField(default=9, db_index=True)

    mitigation = models.TextField(blank=True)
    residual_likelihood = models.PositiveSmallIntegerField(
        choices=SCALE_CHOICES, null=True, blank=True
    )
    residual_impact = models.PositiveSmallIntegerField(choices=SCALE_CHOICES, null=True, blank=True)
    residual_risk_score = models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="owned_risks",
    )
    review_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=RiskStatus.choices, default=RiskStatus.IDENTIFIED, db_index=True
    )

    class Meta:
        ordering = ["-inherent_risk_score", "risk_id"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "risk_id"], name="uniq_risk_id_per_org")
        ]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "risk_category"]),
            models.Index(fields=["organization", "-inherent_risk_score"]),
        ]

    def __str__(self) -> str:
        return f"{self.risk_id} {self.title}"

    def save(self, *args, **kwargs):
        # Scores are always derived, never entered by hand, so the register
        # cannot drift from likelihood x impact.
        self.inherent_risk_score = self.likelihood * self.impact
        if self.residual_likelihood and self.residual_impact:
            self.residual_risk_score = self.residual_likelihood * self.residual_impact
        else:
            self.residual_risk_score = None

        if not self.risk_id:
            self.risk_id = self._next_risk_id()
        super().save(*args, **kwargs)

    @transaction.atomic
    def _next_risk_id(self) -> str:
        """Sequential, human-quotable id, unique within the organization."""
        existing = (
            Risk.objects.filter(organization_id=self.organization_id)
            .exclude(risk_id="")
            .aggregate(top=Max("risk_id"))["top"]
        )
        next_number = 1
        if existing and existing.startswith("RISK-"):
            try:
                next_number = int(existing.split("-")[1]) + 1
            except (IndexError, ValueError):
                next_number = Risk.objects.filter(organization_id=self.organization_id).count() + 1
        return f"RISK-{next_number:04d}"

    # -- Derived state ------------------------------------------------------

    @property
    def inherent_level(self) -> str:
        return level_for_score(self.inherent_risk_score)

    @property
    def residual_level(self) -> str | None:
        return level_for_score(self.residual_risk_score) if self.residual_risk_score else None

    @property
    def effective_level(self) -> str:
        """Residual where it has been assessed, otherwise inherent."""
        return self.residual_level or self.inherent_level

    @property
    def effective_score(self) -> int:
        return self.residual_risk_score or self.inherent_risk_score

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def reduction(self) -> int:
        """How much mitigation lowered the score."""
        if not self.residual_risk_score:
            return 0
        return self.inherent_risk_score - self.residual_risk_score

    @property
    def subject(self):
        """The asset this risk attaches to, if any."""
        return self.agent or self.application or self.model or self.provider
