"""Approval rules -- which actions wait for a person -- onboarded in the web UI."""

from __future__ import annotations

from django import forms

from apps.agents.models import AutonomyLevel
from apps.approvals.models import ApprovalRule, TriggerAction
from apps.core.constants import DataClassification, Environment, RiskLevel
from apps.core.forms import ChoiceListField, ListField, OrganizationModelForm
from apps.policies.forms import TIER_CHOICES

CONDITION_FIELDS = (
    "min_autonomy_level", "risk_levels", "environments", "data_classifications", "tiers", "actions", "tools",
    "cost_threshold", "match_restricted_tool", "match_unapproved_provider", "match_unapproved_model",
)


class ApprovalRuleForm(OrganizationModelForm):
    min_autonomy_level = forms.TypedChoiceField(
        choices=[("", "Any autonomy")] + AutonomyLevel.choices, coerce=int, empty_value=None, required=False,
        label="Agents at or above autonomy",
    )
    risk_levels = ChoiceListField(choices=RiskLevel.choices, label="Risk levels")
    environments = ChoiceListField(choices=Environment.choices)
    data_classifications = ChoiceListField(choices=DataClassification.choices, label="Data classifications")
    tiers = ChoiceListField(
        choices=TIER_CHOICES, coerce=int, label="IRGF tiers",
        help_text="An unclassified system never matches a tier condition.",
    )
    actions = ChoiceListField(choices=TriggerAction.choices, label="Sensitive actions")
    tools = ListField(help_text="Requesting any of these tools triggers the rule.")

    class Meta:
        model = ApprovalRule
        fields = ["name", "description", "severity", "priority", "status", *CONDITION_FIELDS]
        labels = {
            "cost_threshold": "Estimated cost reaches",
            "match_restricted_tool": "A tool on the agent's restricted list is requested",
            "match_unapproved_provider": "The provider is not approved",
            "match_unapproved_model": "The model is not approved",
        }

    unique_in_organization = ("name",)
    sections = (
        ("Rule", "", ["name", "description", "severity", "priority", "status"]),
        (
            "Conditions",
            "The rule matches when every condition it sets is met; a condition left empty is not checked. "
            "A human_approval control policy sends matching actions to the Approval Centre.",
            list(CONDITION_FIELDS),
        ),
    )
    compact_fields = ("severity", "priority", "status", "min_autonomy_level", "cost_threshold")

    def clean(self):
        cleaned = super().clean()
        if not any(cleaned.get(name) not in (None, "", [], False) for name in CONDITION_FIELDS):
            self.add_error(None, "Set at least one condition: a rule without conditions never matches.")
        return cleaned
