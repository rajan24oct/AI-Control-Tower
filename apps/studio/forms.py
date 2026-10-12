"""Describing a use case for the advisor, and setting up a sandbox session."""

from __future__ import annotations

from django import forms

from apps.core.forms import OrganizationModelForm
from apps.studio.models import SandboxSession, SolutionRequest
from apps.studio.services import MAX_COMPARE, MAX_OUTPUT_TOKENS


class SolutionRequestForm(OrganizationModelForm):
    class Meta:
        model = SolutionRequest
        fields = ["title", "problem", "users", "data_sources", "integrations", "constraints",
                  "data_classification", "volume", "hosting", "human_in_loop"]
        widgets = {
            "problem": forms.Textarea(attrs={"rows": 5, "placeholder": "e.g. Claims handlers spend 20 minutes per claim "
                                             "searching policy wordings to check cover. We want them to ask in plain "
                                             "English and get the answer with the clause cited."}),
            "users": forms.Textarea(attrs={"rows": 2, "placeholder": "e.g. 120 claims handlers; customers are affected "
                                           "by the cover decision."}),
            "data_sources": forms.Textarea(attrs={"rows": 2, "placeholder": "e.g. Policy wordings (PDF) in SharePoint, "
                                                  "claims system notes."}),
            "integrations": forms.Textarea(attrs={"rows": 2, "placeholder": "e.g. Guidewire ClaimCenter, Microsoft Teams"}),
            "constraints": forms.Textarea(attrs={"rows": 2, "placeholder": "e.g. EU data residency, answers in under "
                                                 "5 seconds, English and German."}),
        }

    sections = (
        ("The use case", "Describe it as you would to a colleague; the advisor works out the technical side.",
         ["title", "problem", "users"]),
        ("Data and systems", "", ["data_sources", "integrations", "data_classification"]),
        ("Constraints", "These shape the stack, the hosting and the IRGF controls.",
         ["constraints", "volume", "hosting", "human_in_loop"]),
    )
    compact_fields = ("data_classification", "volume", "hosting")


class SandboxSessionForm(OrganizationModelForm):
    compare_models = forms.MultipleChoiceField(
        label="Models to compare", required=True, widget=forms.CheckboxSelectMultiple,
        help_text=f"Up to {MAX_COMPARE}. Each input is sent to every model you pick, through the AI Gateway.",
    )
    samples = forms.CharField(label="Sample inputs", required=False, widget=forms.Textarea(attrs={"rows": 4}),
                              help_text="One per line; click one in the sandbox to use it.")

    class Meta:
        model = SandboxSession
        fields = ["name", "agent", "system_prompt", "temperature", "max_tokens"]
        widgets = {"system_prompt": forms.Textarea(attrs={"rows": 6})}

    sections = (
        ("Session", "", ["name", "agent"]),
        ("Prompt", "", ["system_prompt", "samples"]),
        ("Models", "", ["compare_models", "temperature", "max_tokens"]),
    )
    compact_fields = ("temperature", "max_tokens")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.models_registry.models import AIModel

        choices = []
        for model in (AIModel.objects.for_organization(self.organization).filter(status="active", model_type="chat")
                      .select_related("provider").order_by("provider__name", "model_name")):
            flag = "" if model.approved_for_use and model.provider.is_approved else " · not approved"
            choices.append((str(model.pk), f"{model.model_name} — {model.provider.name}{flag}"))
        self.fields["compare_models"].choices = choices
        self.fields["compare_models"].initial = list(self.instance.model_ids or [])
        self.fields["samples"].initial = "\n".join(self.instance.sample_inputs or [])
        self.fields["max_tokens"].help_text = f"At most {MAX_OUTPUT_TOKENS:,} in the sandbox."
        self.fields["agent"].required = False
        self.apply_styles()

    def clean_compare_models(self):
        picked = self.cleaned_data["compare_models"]
        if len(picked) > MAX_COMPARE:
            raise forms.ValidationError(f"Pick at most {MAX_COMPARE} models.")
        return picked

    def clean_max_tokens(self):
        value = self.cleaned_data["max_tokens"]
        if not 16 <= value <= MAX_OUTPUT_TOKENS:
            raise forms.ValidationError(f"Between 16 and {MAX_OUTPUT_TOKENS:,}.")
        return value

    def clean_temperature(self):
        value = self.cleaned_data["temperature"]
        if not 0 <= value <= 2:
            raise forms.ValidationError("Between 0 and 2.")
        return value

    def save(self, commit=True):
        session = super().save(commit=False)
        session.model_ids = list(self.cleaned_data["compare_models"])
        session.sample_inputs = [line.strip() for line in self.cleaned_data.get("samples", "").splitlines()
                                 if line.strip()][:12]
        if commit:
            session.save()
        return session
