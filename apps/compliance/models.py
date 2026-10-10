"""Compliance: which regimes apply to a system, and what people attest to.

The evidence itself is not stored here: it is read live from the platform's own
records every time (see `apps.compliance.evidence`), so a pack is never out of
date with what the platform knows. What is stored is what only a person can
decide or vouch for.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import OrganizationOwnedModel


class EUCategory(models.TextChoices):
    TO_ASSESS = "to_assess", "Not yet assessed"
    PROHIBITED = "prohibited", "Prohibited practice"
    HIGH_RISK = "high_risk", "High-risk"
    LIMITED = "limited", "Limited risk (transparency duties)"
    MINIMAL = "minimal", "Minimal risk"
    NOT_IN_SCOPE = "not_in_scope", "Not in scope"


class Applicability(models.TextChoices):
    APPLIES = "applies", "Applies"
    DOES_NOT_APPLY = "does_not_apply", "Does not apply"


def _subject_constraint(name: str) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=Q(agent__isnull=False, application__isnull=True) | Q(agent__isnull=True, application__isnull=False),
        name=name,
    )


class RegulatoryScope(OrganizationOwnedModel):
    """Whether a regime applies to one system, decided by a person with a rationale."""

    agent = models.ForeignKey("agents.AIAgent", null=True, blank=True, on_delete=models.CASCADE,
                              related_name="regulatory_scopes")
    application = models.ForeignKey("applications_registry.AIApplication", null=True, blank=True,
                                    on_delete=models.CASCADE, related_name="regulatory_scopes")
    regime = models.CharField(max_length=30)
    applicability = models.CharField(max_length=20, choices=Applicability.choices, default=Applicability.APPLIES)
    eu_category = models.CharField(max_length=20, choices=EUCategory.choices, blank=True,
                                   help_text="EU AI Act only: the risk category this system falls in.")
    rationale = models.TextField(blank=True)
    assessed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="regulatory_scopes")
    assessed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["regime"]
        constraints = [
            _subject_constraint("regulatory_scope_has_one_subject"),
            models.UniqueConstraint(fields=["agent", "regime"], condition=Q(agent__isnull=False),
                                    name="uniq_scope_per_agent_regime"),
            models.UniqueConstraint(fields=["application", "regime"], condition=Q(application__isnull=False),
                                    name="uniq_scope_per_application_regime"),
        ]

    def __str__(self) -> str:
        return f"{self.agent or self.application} · {self.regime}"


class AttestationStatus(models.TextChoices):
    MET = "met", "Met"
    NOT_APPLICABLE = "not_applicable", "Not applicable"


class Attestation(OrganizationOwnedModel):
    """A person vouching for a requirement the platform cannot see, with the evidence and an expiry."""

    agent = models.ForeignKey("agents.AIAgent", null=True, blank=True, on_delete=models.CASCADE,
                              related_name="attestations")
    application = models.ForeignKey("applications_registry.AIApplication", null=True, blank=True,
                                    on_delete=models.CASCADE, related_name="attestations")
    requirement = models.CharField(max_length=40, db_index=True)
    status = models.CharField(max_length=20, choices=AttestationStatus.choices, default=AttestationStatus.MET)
    statement = models.TextField(help_text="What was done, or why the requirement does not apply.")
    evidence_reference = models.CharField(max_length=500, blank=True,
                                          help_text="Where the evidence is: a document ID, link or file name.")
    attested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="attestations")
    attested_at = models.DateTimeField(default=timezone.now)
    valid_until = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-attested_at"]
        constraints = [_subject_constraint("attestation_has_one_subject")]

    def __str__(self) -> str:
        return f"{self.requirement} · {self.agent or self.application}"

    @property
    def is_valid(self) -> bool:
        return self.valid_until is None or self.valid_until >= timezone.now().date()
