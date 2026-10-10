"""Use-case intake: IRGF stages S0 (opportunity) and S1 (qualification), and Gate G1.

A use case is an idea before it is a system: the need, why AI rather than
something simpler, the one decision the output will support, who is affected,
how autonomous it is meant to be, and where its data comes from. It carries
provisional D1-D5 scores -- the same dimensions, anchors and tier matrix as
the S2 classification it will later get -- so the tier is known before anyone
builds anything, and Gate G1 is decided by the people that tier calls for.

An approved use case is registered as a draft application, which then goes
through S2, G3 and the rest of the lifecycle.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.classification import irgf as s2
from apps.core.constants import DataClassification
from apps.core.models import OrganizationOwnedModel

SCORE_CHOICES = [(score, str(score)) for score in s2.SCORES]
AUTONOMY_CHOICES = [(index + 1, f"{index + 1} · {anchor}") for index, anchor in enumerate(s2.D2.anchors)]


class UseCaseStage(models.TextChoices):
    S0 = "S0", "S0 · Opportunity"
    S1 = "S1", "S1 · Use-case qualification"


class UseCaseStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SUBMITTED = "submitted", "Awaiting Gate G1"
    APPROVED = "approved", "Approved at G1"
    CONDITIONAL = "conditional", "Approved with conditions"
    REMEDIATE = "remediate", "Returned to remediate"
    ESCALATED = "escalated", "Escalated"
    REJECTED = "rejected", "Rejected at G1"
    REGISTERED = "registered", "Registered"
    WITHDRAWN = "withdrawn", "Withdrawn"


#: Statuses in which the person proposing it can still change it.
EDITABLE_STATUSES = (UseCaseStatus.DRAFT, UseCaseStatus.REMEDIATE)
#: Statuses awaiting a G1 decision.
OPEN_STATUSES = (UseCaseStatus.SUBMITTED, UseCaseStatus.ESCALATED)
#: Approved and ready to become a registered system.
APPROVED_STATUSES = (UseCaseStatus.APPROVED, UseCaseStatus.CONDITIONAL)


class G1Outcome(models.TextChoices):
    """IRGF's five gate outcomes."""

    APPROVE = "approve", "Approve"
    APPROVE_WITH_CONDITIONS = "approve_with_conditions", "Approve with conditions"
    REMEDIATE = "remediate", "Remediate"
    ESCALATE = "escalate", "Escalate"
    REJECT = "reject", "Reject"


OUTCOME_STATUS = {
    G1Outcome.APPROVE: UseCaseStatus.APPROVED,
    G1Outcome.APPROVE_WITH_CONDITIONS: UseCaseStatus.CONDITIONAL,
    G1Outcome.REMEDIATE: UseCaseStatus.REMEDIATE,
    G1Outcome.ESCALATE: UseCaseStatus.ESCALATED,
    G1Outcome.REJECT: UseCaseStatus.REJECTED,
}


class UseCase(OrganizationOwnedModel):
    reference = models.CharField(max_length=20, editable=False, db_index=True)
    title = models.CharField(max_length=200)
    summary = models.TextField(blank=True, help_text="One or two sentences for the register.")

    sponsor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="sponsored_use_cases", help_text="The business sponsor, who can stop the work.",
    )
    team = models.ForeignKey(
        "organizations.Team", null=True, blank=True, on_delete=models.SET_NULL, related_name="use_cases"
    )
    department = models.CharField(max_length=150, blank=True)

    # S0 · Opportunity
    need = models.TextField(help_text="The business need, in the business's words.")
    current_measure = models.CharField(max_length=300, blank=True, help_text="How it is measured today.")
    target_measure = models.CharField(max_length=300, blank=True, help_text="The target the use case should reach.")
    non_ai_alternative = models.TextField(
        blank=True, help_text="The simpler, non-AI way to meet the need, and why it is not enough.",
    )
    expected_annual_value = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True, help_text="Expected annual benefit, if known.",
    )

    # S1 · Qualification
    decision_supported = models.CharField(
        max_length=300, blank=True, help_text="The one decision the output will support, in a sentence.",
    )
    affected_parties = models.TextField(blank=True, help_text="Who is affected by the decision, inside and outside.")
    intended_autonomy = models.PositiveSmallIntegerField(
        choices=AUTONOMY_CHOICES, null=True, blank=True, help_text="Launch autonomy, in D2 terms.",
    )
    data_path = models.TextField(blank=True, help_text="Where the data comes from and how it would reach the system.")
    data_classification = models.CharField(
        max_length=20, choices=DataClassification.choices, blank=True, help_text="The most sensitive data involved.",
    )
    sector_constraints = models.TextField(
        blank=True, help_text="Regulation or sector rules that apply (for example, consumer credit, health data).",
    )
    no_commitment = models.BooleanField(
        default=False, verbose_name="No architecture or vendor commitment has been made",
        help_text="G1 decides whether to proceed; committing to a build or a vendor first pre-empts it.",
    )

    # Provisional D1-D5: the evidence G1 asks for, confirmed later at S2.
    d1_consequence = models.PositiveSmallIntegerField(choices=SCORE_CHOICES, null=True, blank=True, verbose_name="D1")
    d2_autonomy = models.PositiveSmallIntegerField(choices=SCORE_CHOICES, null=True, blank=True, verbose_name="D2")
    d3_reversibility = models.PositiveSmallIntegerField(choices=SCORE_CHOICES, null=True, blank=True, verbose_name="D3")
    d4_exposure = models.PositiveSmallIntegerField(choices=SCORE_CHOICES, null=True, blank=True, verbose_name="D4")
    d5_data_sensitivity = models.PositiveSmallIntegerField(
        choices=SCORE_CHOICES, null=True, blank=True, verbose_name="D5 data sensitivity"
    )
    d5_model_uncertainty = models.PositiveSmallIntegerField(
        choices=SCORE_CHOICES, null=True, blank=True, verbose_name="D5 model uncertainty"
    )
    provisional_tier = models.PositiveSmallIntegerField(null=True, blank=True, editable=False, db_index=True)
    floor_applied = models.BooleanField(default=False, editable=False)

    stage = models.CharField(max_length=4, choices=UseCaseStage.choices, default=UseCaseStage.S0)
    status = models.CharField(
        max_length=20, choices=UseCaseStatus.choices, default=UseCaseStatus.DRAFT, db_index=True
    )
    submitted_at = models.DateTimeField(null=True, blank=True, editable=False)

    # The G1 decision.
    outcome = models.CharField(max_length=30, choices=G1Outcome.choices, blank=True, editable=False)
    decided_at = models.DateTimeField(null=True, blank=True, editable=False)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="g1_decisions", editable=False,
    )
    decided_by_rule = models.CharField(max_length=200, blank=True, editable=False)
    decision_note = models.TextField(blank=True, editable=False)
    #: Approve-with-conditions: [{"text", "owner_id", "owner", "due", "done"}].
    conditions = models.JSONField(default=list, blank=True, editable=False)

    application = models.ForeignKey(
        "applications_registry.AIApplication", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="use_cases", editable=False,
    )
    registered_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "reference"], name="uniq_use_case_reference_per_org")
        ]
        indexes = [models.Index(fields=["organization", "status"])]

    def __str__(self) -> str:
        return f"{self.reference} {self.title}"

    @property
    def scores(self) -> dict:
        return {field: getattr(self, field) for field in s2.ALL_FIELDS}

    @property
    def scored(self) -> bool:
        return all(value is not None for value in self.scores.values())

    @property
    def tier_result(self):
        return s2.compute(**{
            "d1": self.d1_consequence, "d2": self.d2_autonomy, "d3": self.d3_reversibility,
            "d4": self.d4_exposure, "d5_data": self.d5_data_sensitivity, "d5_model": self.d5_model_uncertainty,
        }) if self.scored else None

    @property
    def is_editable(self) -> bool:
        return self.status in EDITABLE_STATUSES

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_approved(self) -> bool:
        return self.status in APPROVED_STATUSES

    @property
    def open_conditions(self) -> list[dict]:
        return [condition for condition in self.conditions or [] if not condition.get("done")]

    def save(self, *args, **kwargs):
        if not self.reference:
            last = (
                UseCase.objects.filter(organization_id=self.organization_id)
                .order_by("-created_at").values_list("reference", flat=True).first()
            )
            number = int(last.split("-")[1]) + 1 if last and "-" in last else 1
            while UseCase.objects.filter(organization_id=self.organization_id, reference=f"UC-{number:04d}").exists():
                number += 1
            self.reference = f"UC-{number:04d}"
        result = self.tier_result
        self.provisional_tier = result.tier if result else None
        self.floor_applied = bool(result and result.floor_applied)
        if self.decided_at is None and self.outcome:
            self.decided_at = timezone.now()
        super().save(*args, **kwargs)
