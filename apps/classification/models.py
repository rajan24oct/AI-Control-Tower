"""IRGF S2 risk classification records (modules M4, M5 and M6).

One record per scoring. A re-score creates a new record and supersedes the old
one rather than overwriting it, because IRGF asks for score history to be kept:
"what did we think this system was, and when did that change" is exactly what a
reviewer asks after an incident.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.classification import irgf
from apps.core.models import OrganizationOwnedModel

SCORE_CHOICES = [(score, str(score)) for score in irgf.SCORES]
TIER_CHOICES = [(tier, f"Tier {tier}") for tier in irgf.TIERS]


class ClassificationStatus(models.TextChoices):
    PROVISIONAL = "provisional", "Provisional"
    CONFIRMED = "confirmed", "Confirmed"
    SUPERSEDED = "superseded", "Superseded"


#: A subject's current classification is its newest record in one of these.
CURRENT_STATUSES = (ClassificationStatus.PROVISIONAL, ClassificationStatus.CONFIRMED)


class RiskClassification(OrganizationOwnedModel):
    """The Risk Classification Record: D1–D5, the tier, and who vouched for it."""

    agent = models.ForeignKey(
        "agents.AIAgent", null=True, blank=True, on_delete=models.CASCADE,
        related_name="risk_classifications",
    )
    application = models.ForeignKey(
        "applications_registry.AIApplication", null=True, blank=True, on_delete=models.CASCADE,
        related_name="risk_classifications",
    )

    d1_consequence = models.PositiveSmallIntegerField("D1 consequence severity", choices=SCORE_CHOICES)
    d2_autonomy = models.PositiveSmallIntegerField("D2 autonomy", choices=SCORE_CHOICES)
    d3_reversibility = models.PositiveSmallIntegerField("D3 reversibility deficit", choices=SCORE_CHOICES)
    d4_exposure = models.PositiveSmallIntegerField("D4 exposure and scale", choices=SCORE_CHOICES)
    d5_data_sensitivity = models.PositiveSmallIntegerField("D5 data sensitivity", choices=SCORE_CHOICES)
    d5_model_uncertainty = models.PositiveSmallIntegerField("D5 model uncertainty", choices=SCORE_CHOICES)

    # Derived by the M5 rule on every save, and stored so registers can sort and
    # filter on them. Never edited directly.
    impact = models.PositiveSmallIntegerField(default=1, editable=False)
    control_deficit = models.PositiveSmallIntegerField(default=1, editable=False)
    matrix_tier = models.PositiveSmallIntegerField(choices=TIER_CHOICES, default=1, editable=False)
    floor_applied = models.BooleanField(default=False, editable=False)
    tier = models.PositiveSmallIntegerField(
        choices=TIER_CHOICES, default=1, editable=False, db_index=True
    )

    tier_override = models.PositiveSmallIntegerField(
        choices=TIER_CHOICES, null=True, blank=True,
        help_text="Replace the computed tier. Never below the safety floor; needs a rationale.",
    )
    override_rationale = models.TextField(blank=True)
    rationale = models.TextField(help_text="The evidence and assumptions behind the scores.")

    status = models.CharField(
        max_length=20, choices=ClassificationStatus.choices,
        default=ClassificationStatus.PROVISIONAL, db_index=True,
    )
    scored_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="risk_classifications_scored",
    )
    scored_at = models.DateTimeField(default=timezone.now, db_index=True)

    # M6, independent verification. The countersigner may adjust scores; when
    # they do, the scorer's originals are kept alongside, not replaced.
    countersigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="risk_classifications_countersigned",
    )
    countersigned_at = models.DateTimeField(null=True, blank=True)
    countersign_note = models.TextField(blank=True)
    scorer_scores = models.JSONField(default=dict, blank=True)

    # Re-score triggers. The record stays in force, flagged, until someone
    # re-scores: dropping a system to "unclassified" because its autonomy rose
    # would read as less risky, the opposite of the truth.
    rescore_required = models.BooleanField(default=False, db_index=True)
    rescore_reasons = models.JSONField(default=list, blank=True)
    review_due = models.DateField(null=True, blank=True)

    superseded_at = models.DateTimeField(null=True, blank=True)
    superseded_by = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="supersedes"
    )

    class Meta:
        ordering = ["-scored_at"]
        verbose_name = "risk classification"
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(agent__isnull=False, application__isnull=True)
                    | Q(agent__isnull=True, application__isnull=False)
                ),
                name="risk_classification_has_one_subject",
            )
        ]
        indexes = [
            models.Index(fields=["organization", "status", "tier"]),
            models.Index(fields=["agent", "status"]),
            models.Index(fields=["application", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.subject} · Tier {self.tier} ({self.get_status_display().lower()})"

    # -- Subject ------------------------------------------------------------

    @property
    def subject(self):
        return self.agent or self.application

    @property
    def subject_kind(self) -> str:
        return "agent" if self.agent_id else "application"

    # -- Scores -------------------------------------------------------------

    def scores(self) -> dict[str, int]:
        return {field: getattr(self, field) for field in irgf.ALL_FIELDS}

    @property
    def d5(self) -> int:
        return max(self.d5_data_sensitivity or 0, self.d5_model_uncertainty or 0)

    def dimension_rows(self) -> list[dict]:
        """Each dimension with its score and what that score means."""
        rows = [
            {"code": d.code, "name": d.name, "score": getattr(self, d.field),
             "anchor": d.anchor(getattr(self, d.field))}
            for d in irgf.DIMENSIONS
        ]
        rows.append({
            "code": "D5", "name": "Sensitivity and uncertainty", "score": self.d5,
            "anchor": "Higher of the two sub-scores below",
            "parts": [
                {"name": d.name, "score": getattr(self, d.field),
                 "anchor": d.anchor(getattr(self, d.field))}
                for d in irgf.D5_PARTS
            ],
        })
        return rows

    def result(self) -> irgf.TierResult:
        return irgf.compute(
            self.d1_consequence, self.d2_autonomy, self.d3_reversibility,
            self.d4_exposure, self.d5_data_sensitivity, self.d5_model_uncertainty,
        )

    def recompute(self) -> None:
        result = self.result()
        self.impact = result.impact
        self.control_deficit = result.control_deficit
        self.matrix_tier = result.matrix_tier
        self.floor_applied = result.floor_applied
        self.tier = self.tier_override or result.tier

    # -- Validation ---------------------------------------------------------

    def clean(self):
        errors = {}
        if bool(self.agent_id) == bool(self.application_id):
            errors["agent"] = "Classify exactly one agent or one application."
        try:
            result = self.result()
        except (TypeError, ValueError) as exc:
            errors["d1_consequence"] = str(exc)
            result = None

        if self.tier_override:
            if not (self.override_rationale or "").strip():
                errors["override_rationale"] = "Explain why the computed tier is being replaced."
            if result and irgf.floor_applies(
                self.d1_consequence, self.d2_autonomy, self.d3_reversibility
            ) and self.tier_override < irgf.FLOOR_TIER:
                errors["tier_override"] = (
                    f"The safety floor holds this system at Tier {irgf.FLOOR_TIER} or above "
                    "(D1 = 4, or D2 = 4 with D3 = 4). No override can go below it."
                )
        if not (self.rationale or "").strip():
            errors["rationale"] = "Record the evidence and assumptions behind the scores."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.recompute()
        super().save(*args, **kwargs)

    # -- State --------------------------------------------------------------

    @property
    def is_current(self) -> bool:
        return self.status in CURRENT_STATUSES

    @property
    def is_confirmed(self) -> bool:
        return self.status == ClassificationStatus.CONFIRMED

    @property
    def is_provisional(self) -> bool:
        return self.status == ClassificationStatus.PROVISIONAL

    @property
    def review_overdue(self) -> bool:
        return bool(self.review_due and self.review_due <= timezone.now().date())

    @property
    def rescore_due(self) -> bool:
        """A trigger fired, or the annual review for Tier 3–4 has come round."""
        return self.is_current and (self.rescore_required or self.review_overdue)

    @property
    def gate_evidence(self) -> str:
        """The gate this record can stand as evidence for."""
        return irgf.CONFIRMED_GATE if self.is_confirmed else irgf.PROVISIONAL_GATE

    @property
    def tone(self) -> str:
        return irgf.TIER_TONE.get(self.tier, "low")

    @property
    def tier_summary(self) -> str:
        return irgf.TIER_SUMMARY.get(self.tier, "")

    @property
    def was_adjusted(self) -> bool:
        return bool(self.scorer_scores)
