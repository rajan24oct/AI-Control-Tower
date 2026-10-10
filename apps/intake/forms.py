"""Proposing a use case: S0 and S1, with provisional D1-D5 scores."""

from __future__ import annotations

from django import forms

from apps.classification import irgf as s2
from apps.core.forms import OrganizationModelForm
from apps.intake.models import UseCase

SCORE_FIELDS = {
    "d1_consequence": s2.D1, "d2_autonomy": s2.D2, "d3_reversibility": s2.D3, "d4_exposure": s2.D4,
    "d5_data_sensitivity": s2.D5_DATA, "d5_model_uncertainty": s2.D5_MODEL,
}


class UseCaseForm(OrganizationModelForm):
    class Meta:
        model = UseCase
        fields = [
            "title", "summary", "sponsor", "team", "department",
            "need", "current_measure", "target_measure", "non_ai_alternative", "expected_annual_value",
            "decision_supported", "affected_parties", "intended_autonomy", "data_path", "data_classification",
            "sector_constraints", "no_commitment", *SCORE_FIELDS,
        ]
        labels = {"expected_annual_value": "Expected annual value (USD)"}

    sections = (
        ("Use case", "", ["title", "summary", "sponsor", "team", "department"]),
        ("S0 · Opportunity", "Why this, and why AI.",
         ["need", "current_measure", "target_measure", "non_ai_alternative", "expected_annual_value"]),
        ("S1 · Qualification", "What it would decide, for whom, how autonomously, and from what data.",
         ["decision_supported", "affected_parties", "intended_autonomy", "data_path", "data_classification",
          "sector_constraints", "no_commitment"]),
        ("Provisional D1–D5", "Score each dimension 1–4 as best you can today. The scores stay provisional "
         "until the system is classified at S2; they decide who signs Gate G1.", list(SCORE_FIELDS)),
    )
    compact_fields = ("sponsor", "team", "department", "current_measure", "target_measure",
                      "expected_annual_value", "intended_autonomy", "data_classification", *SCORE_FIELDS)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, dimension in SCORE_FIELDS.items():
            field = self.fields[name]
            label = f"{dimension.code} · {dimension.name}"
            field.label = label
            field.help_text = dimension.question
            field.choices = [("", "—")] + [(score, f"{score} · {dimension.anchor(score)}") for score in s2.SCORES]
        self.apply_styles()
