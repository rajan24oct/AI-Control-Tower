"""Forms for IRGF Gate G3 requests and M17 exceptions."""

from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import AIAgent
from apps.lifecycle import irgf
from apps.organizations.models import OrganizationMembership
from apps.policies.models import Policy

CONDITION_ROWS = (1, 2)


class _PersonChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.get_full_name() or obj.email


def _textarea(rows: int, placeholder: str = "") -> forms.Textarea:
    return forms.Textarea(attrs={"rows": rows, "class": "form-control", "placeholder": placeholder})


def _members(organization):
    return get_user_model().objects.filter(
        pk__in=OrganizationMembership.objects.filter(organization=organization).values("user_id")
    ).order_by("first_name", "last_name", "email")


class AuthorizationRequestForm(forms.Form):
    assurance_summary = forms.CharField(
        required=False,
        widget=_textarea(3, "What was tested, against which criteria, and what was not tested"),
        help_text="IRGF S6. At Tier 2 this can stand in for an evaluation run.",
    )
    oversight_point = forms.CharField(
        required=False,
        widget=_textarea(3, "Who reviews, with what information, how fast, with what authority"),
        help_text="Required from Tier 2.",
    )
    monitoring_commitments = forms.CharField(
        required=False,
        widget=_textarea(2, "Which signals are watched, at what thresholds, by whom"),
        help_text="Required from Tier 3.",
    )

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        members = _members(organization)
        for row in CONDITION_ROWS:
            self.fields[f"condition_{row}"] = forms.CharField(
                required=False, label=f"Condition {row}",
                widget=forms.TextInput(attrs={"class": "form-control form-control-sm",
                                              "placeholder": "What must be true, by when"}),
            )
            self.fields[f"condition_{row}_owner"] = _PersonChoice(
                queryset=members, required=False, label="Owner",
                widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
            )
            self.fields[f"condition_{row}_due"] = forms.DateField(
                required=False, label="Due",
                widget=forms.DateInput(attrs={"type": "date", "class": "form-control form-control-sm"}),
            )

    def clean(self):
        cleaned = super().clean()
        for row in CONDITION_ROWS:
            if not (cleaned.get(f"condition_{row}") or "").strip():
                continue
            if not cleaned.get(f"condition_{row}_owner"):
                self.add_error(f"condition_{row}_owner", "A condition needs an owner.")
            due = cleaned.get(f"condition_{row}_due")
            if not due:
                self.add_error(f"condition_{row}_due", "A condition needs a deadline.")
            elif due <= timezone.now().date():
                self.add_error(f"condition_{row}_due", "The deadline must be in the future.")
        return cleaned

    def condition_rows(self):
        return [
            (self[f"condition_{row}"], self[f"condition_{row}_owner"], self[f"condition_{row}_due"])
            for row in CONDITION_ROWS
        ]

    def conditions(self) -> list[dict]:
        return [
            {
                "description": self.cleaned_data[f"condition_{row}"].strip(),
                "owner": self.cleaned_data[f"condition_{row}_owner"],
                "due_date": self.cleaned_data[f"condition_{row}_due"],
            }
            for row in CONDITION_ROWS
            if (self.cleaned_data.get(f"condition_{row}") or "").strip()
        ]


class ExceptionRequestForm(forms.Form):
    agent = forms.ModelChoiceField(
        queryset=AIAgent.objects.none(), widget=forms.Select(attrs={"class": "form-select"})
    )
    policy = forms.ModelChoiceField(
        queryset=Policy.objects.none(), widget=forms.Select(attrs={"class": "form-select"}),
        help_text="The requirement being excepted.",
    )
    justification = forms.CharField(
        widget=_textarea(3, "Why the requirement cannot be met, or is met another way"),
    )
    compensating_control = forms.CharField(
        required=False,
        widget=_textarea(2, "What covers the risk meanwhile"),
        help_text="Required at Tier 3-4.",
    )
    duration_days = forms.IntegerField(
        min_value=1, max_value=irgf.MAX_EXCEPTION_DAYS, initial=30,
        widget=forms.NumberInput(attrs={"class": "form-control"}),
        help_text=f"At most {irgf.MAX_EXCEPTION_DAYS} days, at every tier.",
    )

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["agent"].queryset = AIAgent.objects.for_organization(organization).order_by("name")
        self.fields["policy"].queryset = (
            Policy.objects.for_organization(organization).filter(status="active").order_by("priority", "name")
        )
