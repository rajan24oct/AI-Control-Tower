"""Routing policies, onboarded in the web UI."""

from __future__ import annotations

from django import forms

from apps.agents.models import AIAgent
from apps.core.constants import Environment
from apps.core.forms import ChoiceListField, OrganizationModelForm
from apps.inventory.forms import CAPABILITY_CHOICES
from apps.providers.models import AIProvider
from apps.routing.models import RoutingPolicy

MAX_FALLBACK_DEPTH = 5


class RoutingPolicyForm(OrganizationModelForm):
    environments = ChoiceListField(
        choices=Environment.choices, help_text="Leave every box empty to apply in all environments."
    )
    agents = forms.ModelMultipleChoiceField(
        queryset=AIAgent.objects.all(), required=False, widget=forms.CheckboxSelectMultiple,
        help_text="Leave every box empty to apply to all agents.",
    )
    required_capabilities = ChoiceListField(choices=CAPABILITY_CHOICES, label="Required capabilities")
    allowed_providers = forms.ModelMultipleChoiceField(
        queryset=AIProvider.objects.all(), required=False, widget=forms.CheckboxSelectMultiple,
        label="Only these providers", help_text="If any are ticked, no other provider is considered.",
    )
    blocked_providers = forms.ModelMultipleChoiceField(
        queryset=AIProvider.objects.all(), required=False, widget=forms.CheckboxSelectMultiple,
        label="Never these providers",
    )

    class Meta:
        model = RoutingPolicy
        fields = [
            "name", "description", "strategy", "priority", "status",
            "model_type", "environments", "agents",
            "required_capabilities", "min_context_window", "max_input_cost_per_1k",
            "allowed_providers", "blocked_providers", "require_approved_models", "allow_degraded_providers",
            "fallback_depth",
        ]
        labels = {
            "model_type": "Model type", "min_context_window": "Minimum context window",
            "max_input_cost_per_1k": "Maximum input price per 1K tokens",
            "require_approved_models": "Only models approved for use",
            "allow_degraded_providers": "Keep degraded providers as fallbacks",
            "fallback_depth": "Fallbacks after the primary",
        }

    unique_in_organization = ("name",)
    sections = (
        ("Policy", "", ["name", "description", "strategy", "priority", "status"]),
        ("Applies to", "The class of work this policy answers for.", ["model_type", "environments", "agents"]),
        (
            "Admissible models",
            "Candidates outside these limits are excluded, and every routing decision records why.",
            ["required_capabilities", "min_context_window", "max_input_cost_per_1k", "allowed_providers",
             "blocked_providers", "require_approved_models", "allow_degraded_providers"],
        ),
        ("Fallback", "Unhealthy providers and open circuit breakers are always skipped.", ["fallback_depth"]),
    )
    compact_fields = (
        "strategy", "priority", "model_type", "status", "min_context_window", "max_input_cost_per_1k",
        "fallback_depth",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["required_capabilities"].include(self.instance.required_capabilities)
        self.fields["environments"].include(self.instance.environments)

    def clean_fallback_depth(self):
        depth = self.cleaned_data.get("fallback_depth")
        if depth is not None and depth > MAX_FALLBACK_DEPTH:
            raise forms.ValidationError(f"At most {MAX_FALLBACK_DEPTH} fallbacks; longer chains outlast the request deadline.")
        return depth

    def clean(self):
        cleaned = super().clean()
        both = set(cleaned.get("allowed_providers") or []) & set(cleaned.get("blocked_providers") or [])
        if both:
            self.add_error(
                "blocked_providers", f"Both allowed and blocked: {', '.join(sorted(str(p) for p in both))}."
            )
        return cleaned
