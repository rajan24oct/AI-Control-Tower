"""Forms for IRGF S2 scoring and M6 countersigning."""

from __future__ import annotations

from django import forms

from apps.classification import irgf
from apps.classification.models import TIER_CHOICES


def _score_field(dimension: irgf.Dimension) -> forms.TypedChoiceField:
    return forms.TypedChoiceField(
        label=f"{dimension.code} · {dimension.name}",
        choices=[(score, f"{score} — {dimension.anchor(score)}") for score in irgf.SCORES],
        coerce=int,
        widget=forms.RadioSelect(attrs={"class": "form-check-input"}),
        help_text=dimension.question,
    )


def _textarea(rows: int) -> forms.Textarea:
    return forms.Textarea(attrs={"rows": rows, "class": "form-control"})


class _DimensionFields(forms.Form):
    d1_consequence = _score_field(irgf.D1)
    d2_autonomy = _score_field(irgf.D2)
    d3_reversibility = _score_field(irgf.D3)
    d4_exposure = _score_field(irgf.D4)
    d5_data_sensitivity = _score_field(irgf.D5_DATA)
    d5_model_uncertainty = _score_field(irgf.D5_MODEL)

    def dimension_groups(self) -> list[dict]:
        """Each dimension with its bound field, in scoring order."""
        return [
            {"dimension": dimension, "field": self[dimension.field]}
            for dimension in (*irgf.DIMENSIONS, *irgf.D5_PARTS)
        ]


class ScoreForm(_DimensionFields):
    rationale = forms.CharField(
        widget=_textarea(4),
        help_text="The evidence and assumptions behind the scores. It is read at Gates G1 and G2.",
    )
    tier_override = forms.TypedChoiceField(
        choices=[("", "Use the computed tier")] + TIER_CHOICES,
        coerce=int,
        empty_value=None,
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        help_text="Rarely needed, and never below the safety floor.",
    )
    override_rationale = forms.CharField(
        widget=_textarea(2), required=False, help_text="Required with an override.",
    )


class CountersignForm(_DimensionFields):
    note = forms.CharField(
        widget=_textarea(3),
        required=False,
        help_text="Required if you change any score: the reason is part of the evidence.",
    )
