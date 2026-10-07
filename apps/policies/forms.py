"""Control policies -- the decisions both gateways enforce -- onboarded in the web UI."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from django import forms

from apps.core.constants import DataClassification, Environment, RiskLevel
from apps.core.forms import ChoiceListField, JSONObjectField, OrganizationModelForm
from apps.policies.models import Policy, PolicyType

TIER_CHOICES = [(tier, f"Tier {tier}") for tier in range(1, 5)]

#: What each setting holds. The model rejects unknown keys; this catches a
#: wrongly typed value before the policy engine meets it on live traffic.
SETTING_KINDS: dict[str, str] = {
    "blocked_risk_classifications": "list", "required_suites": "list", "allowed_providers": "list",
    "blocked_providers": "list", "allowed_regions": "list", "classifications": "list",
    "environments": "list", "risk_levels": "list", "blocked_tools": "list",
    "require_evaluation": "bool", "require_approved": "bool", "require_approved_provider": "bool",
    "require_countersignature": "bool", "block_when_rescore_due": "bool", "block_pending_changes": "bool",
    "evaluation_max_age_days": "int", "max_autonomy_level": "int",
    "max_cost_per_execution": "number",
    "blocked_recipients": "list", "block_external": "bool", "external_max_data_classification": "classification",
    "max_hops": "int",
}

#: Settings whose values come from a fixed vocabulary.
SETTING_VALUES: dict[str, list[str]] = {
    "environments": Environment.values,
    "risk_levels": RiskLevel.values,
    "blocked_risk_classifications": RiskLevel.values,
    "classifications": DataClassification.values,
}

#: A starting configuration for each type, filled in when the type is chosen.
STARTERS: dict[str, dict] = {
    PolicyType.MODEL_APPROVAL: {"blocked_risk_classifications": ["critical"], "require_evaluation": False},
    PolicyType.PROVIDER: {"require_approved": True, "allowed_regions": []},
    PolicyType.DATA_CLASSIFICATION: {
        "classifications": ["restricted"], "environments": ["production"], "require_approved_provider": True,
    },
    PolicyType.AGENT_AUTONOMY: {"max_autonomy_level": 3, "environments": ["production"]},
    PolicyType.COST_LIMIT: {"max_cost_per_execution": "5.00"},
    PolicyType.TOOL_ACCESS: {"blocked_tools": [], "environments": ["production"]},
    PolicyType.HUMAN_APPROVAL: {},
    PolicyType.RISK_CLASSIFICATION: {
        "environments": ["production"], "require_countersignature": True, "block_when_rescore_due": False,
    },
    PolicyType.DEPLOYMENT_AUTHORIZATION: {"environments": ["production"], "block_pending_changes": False},
    PolicyType.AGENT_COMMUNICATION: {
        "blocked_recipients": [], "block_external": False, "external_max_data_classification": "internal",
        "max_hops": 3,
    },
}


def _kind_error(key: str, kind: str, value) -> str | None:
    if kind == "list" and not (isinstance(value, list) and all(isinstance(item, (str, int)) for item in value)):
        return f"{key} is a list, e.g. [\"a\", \"b\"]."
    if kind == "bool" and not isinstance(value, bool):
        return f"{key} is true or false."
    if kind == "int" and (not isinstance(value, int) or isinstance(value, bool)):
        return f"{key} is a whole number."
    if kind == "number":
        try:
            Decimal(str(value))
        except (InvalidOperation, ValueError):
            return f"{key} is an amount, e.g. \"5.00\"."
        if isinstance(value, bool):
            return f"{key} is an amount, e.g. \"5.00\"."
    if kind == "classification" and value not in DataClassification.values:
        return f"{key} is one of {', '.join(DataClassification.values)}."
    return None


class PolicyForm(OrganizationModelForm):
    configuration = JSONObjectField(
        label="Settings",
        help_text="The settings this type understands, as JSON. Empty applies the type's built-in check.",
        widget=forms.Textarea(attrs={"rows": 8, "class": "font-monospace"}),
    )
    tiers = ChoiceListField(
        choices=TIER_CHOICES, coerce=int, label="Only at IRGF tiers",
        help_text="Leave empty to apply at every tier. A tier-scoped policy never matches an unclassified system.",
    )

    class Meta:
        model = Policy
        fields = [
            "name", "description", "reference", "policy_type", "effect", "severity", "priority", "status",
            "configuration", "tiers",
        ]
        labels = {"policy_type": "Type", "reference": "Compliance reference"}
        help_texts = {
            "reference": "The control or regulation it implements, e.g. EU AI Act Art. 14, ISO 42001 A.6.",
            "effect": "Deny refuses outright; require approval holds for a person; warn records and lets it through.",
            "priority": "Lower numbers are evaluated first.",
        }

    unique_in_organization = ("name",)
    sections = (
        ("Policy", "", ["name", "description", "reference"]),
        ("Decision", "What both gateways do when it matches.", ["policy_type", "effect", "severity", "priority", "status"]),
        ("Settings", "", ["configuration", "tiers"]),
    )
    compact_fields = ("policy_type", "effect", "severity", "priority", "status")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        configuration = dict(self.instance.configuration or {})
        self.initial["tiers"] = configuration.pop("tiers", [])
        self.initial["configuration"] = configuration
        self.fields["policy_type"].widget.attrs.update({
            "data-config-templates": json.dumps(
                {key: json.dumps(value, indent=2) for key, value in STARTERS.items()}
            ),
            "data-config-target": "#id_configuration",
        })

    def clean(self):
        cleaned = super().clean()
        configuration = dict(cleaned.get("configuration") or {})
        configuration.pop("tiers", None)  # the tier boxes are the one place tiers are set
        for key, value in configuration.items():
            kind = SETTING_KINDS.get(key)
            problem = _kind_error(key, kind, value) if kind else None
            if problem is None and key in SETTING_VALUES:
                unknown = sorted({str(item) for item in value} - set(SETTING_VALUES[key]))
                if unknown:
                    problem = f"{key}: unknown value(s) {', '.join(unknown)}. Allowed: {', '.join(SETTING_VALUES[key])}."
            if problem is None and key == "max_autonomy_level" and not 0 <= value <= 4:
                problem = "max_autonomy_level is between 0 and 4."
            if problem is None and key == "max_hops" and value < 1:
                problem = "max_hops is at least 1: one hand-off from the first agent to the next."
            if problem:
                self.add_error("configuration", problem)
        if cleaned.get("tiers"):
            configuration["tiers"] = sorted(cleaned["tiers"])
        cleaned["configuration"] = configuration
        return cleaned
