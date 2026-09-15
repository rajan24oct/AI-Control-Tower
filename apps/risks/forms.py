"""Risks in the AI risk register, recorded in the web UI."""

from __future__ import annotations

from apps.core.forms import OrganizationModelForm
from apps.risks.models import Risk


class RiskForm(OrganizationModelForm):
    class Meta:
        model = Risk
        fields = [
            "title", "description", "risk_category", "status", "owner", "review_date",
            "application", "agent", "model", "provider",
            "likelihood", "impact", "mitigation", "residual_likelihood", "residual_impact",
        ]
        labels = {
            "risk_category": "Category", "review_date": "Next review",
            "residual_likelihood": "Residual likelihood", "residual_impact": "Residual impact",
        }
        help_texts = {
            "mitigation": "The controls in place. A risk with a recorded control counts as mapped at Gate G3.",
        }

    sections = (
        ("Risk", "", ["title", "description", "risk_category", "status", "owner", "review_date"]),
        (
            "What it concerns",
            "Attach it to the asset it threatens, so it shows on that asset's page.",
            ["application", "agent", "model", "provider"],
        ),
        ("Inherent risk", "Before mitigation. The score is likelihood × impact, from 1 to 25.", ["likelihood", "impact"]),
        ("Mitigation and residual risk", "After the controls in place.", ["mitigation", "residual_likelihood", "residual_impact"]),
    )
    compact_fields = (
        "risk_category", "status", "owner", "review_date", "application", "agent", "model", "provider",
        "likelihood", "impact", "residual_likelihood", "residual_impact",
    )

    def clean(self):
        cleaned = super().clean()
        likelihood, impact = cleaned.get("residual_likelihood"), cleaned.get("residual_impact")
        if bool(likelihood) != bool(impact):
            self.add_error(
                "residual_impact" if likelihood else "residual_likelihood",
                "Score residual risk on both likelihood and impact, or on neither.",
            )
        return cleaned
