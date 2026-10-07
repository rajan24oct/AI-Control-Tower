"""Onboarding forms for the four registries: providers, models, agents and applications."""

from __future__ import annotations

import re
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.utils.text import slugify

from apps.agents.models import KILL_SWITCH_TRIGGERS, AIAgent, validate_action_limits
from apps.applications_registry.models import DELEGATION_AUTHORITY, AIApplication, validate_delegations
from apps.core.forms import ChoiceListField, JSONObjectField, ListField, OrganizationModelForm
from apps.models_registry.models import AIModel
from apps.providers.models import AIProvider
from integrations.base.capabilities import Capability
from integrations.registry import AdapterKind, registry

#: An environment variable's name. The platform resolves credentials from its
#: environment, so anything else -- a pasted key above all -- is refused.
CREDENTIAL_REF = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

CAPABILITY_CHOICES = [
    (str(capability), str(capability).removeprefix("supports_").replace("_", " ").capitalize())
    for capability in Capability
]


def adapter_choices(kind: str, current: str = "") -> list[tuple[str, str]]:
    choices = list(registry.choices(kind))
    if current and current not in {key for key, _ in choices}:
        choices.append((current, f"{current} (not loaded in this installation)"))
    return choices


def clean_credential_ref(value: str) -> str:
    value = (value or "").strip()
    if value and not CREDENTIAL_REF.match(value):
        raise ValidationError(
            "Enter the name of an environment variable, such as OPENAI_API_KEY -- never the credential itself."
        )
    return value


def _number(value: Decimal):
    """JSON keeps numbers, not Decimals: whole amounts as ints, the rest as floats."""
    return int(value) if value == value.to_integral_value() else float(value)


# -- Providers ------------------------------------------------------------------


class ProviderForm(OrganizationModelForm):
    adapter_key = forms.ChoiceField(
        label="Adapter",
        help_text="The code the platform calls this provider through. See /api/v1/adapters/ for what is loaded.",
    )
    options = JSONObjectField(
        label="Adapter options",
        help_text='Non-secret settings, e.g. {"timeout": 20}, or a generic_rest completion_path. Never a credential.',
    )

    class Meta:
        model = AIProvider
        fields = [
            "name", "description", "provider_type", "owner", "status",
            "adapter_key", "endpoint", "region", "deployment",
            "authentication_method", "credential_ref", "options",
        ]
        labels = {"provider_type": "Provider type", "credential_ref": "Credential reference"}
        help_texts = {
            "provider_type": "A label for the registry. What the platform calls is set by the adapter.",
            "credential_ref": "The environment variable holding the secret, e.g. AZURE_OPENAI_API_KEY.",
        }

    unique_in_organization = ("name",)
    slug_source = "name"
    sections = (
        ("Provider", "", ["name", "description", "provider_type", "owner", "status"]),
        ("Connection", "", ["adapter_key", "endpoint", "region", "deployment"]),
        (
            "Credential",
            "The database never stores a secret. Name the environment variable that holds it; the "
            "platform reads it from its own environment at the moment of each call.",
            ["authentication_method", "credential_ref"],
        ),
        ("Advanced", "", ["options"]),
    )
    compact_fields = (
        "provider_type", "owner", "adapter_key", "region", "deployment", "authentication_method",
        "credential_ref", "status",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["adapter_key"].choices = adapter_choices(AdapterKind.PROVIDER, self.instance.adapter_key)

    def clean_credential_ref(self):
        return clean_credential_ref(self.cleaned_data.get("credential_ref"))


# -- Models ---------------------------------------------------------------------


class AIModelForm(OrganizationModelForm):
    capabilities = ChoiceListField(
        choices=CAPABILITY_CHOICES,
        help_text="What the model can do. Routing policies can require a capability.",
    )

    class Meta:
        model = AIModel
        fields = [
            "provider", "model_name", "model_id", "model_family", "model_type", "version",
            "capabilities", "context_window", "max_output_tokens",
            "input_cost_per_1k", "output_cost_per_1k", "currency",
            "region", "deployment", "risk_classification", "notes", "status",
        ]
        labels = {
            "model_name": "Display name", "model_id": "Model ID", "input_cost_per_1k": "Input price per 1K tokens",
            "output_cost_per_1k": "Output price per 1K tokens", "risk_classification": "Risk",
        }
        help_texts = {"model_id": "Exactly as the provider's API expects it, e.g. gpt-4o-mini."}

    sections = (
        ("Model", "", ["provider", "model_type", "model_name", "model_id", "model_family", "version"]),
        ("Capabilities and limits", "", ["capabilities", "context_window", "max_output_tokens"]),
        (
            "Pricing",
            "Rates drive cost reporting, budgets and cost-aware routing.",
            ["input_cost_per_1k", "output_cost_per_1k", "currency"],
        ),
        (
            "Deployment and governance",
            "Approval for use is a separate decision, taken on the model's page by someone who holds model.approve.",
            ["region", "deployment", "risk_classification", "status", "notes"],
        ),
    )
    compact_fields = (
        "provider", "model_type", "model_name", "model_id", "model_family", "version",
        "context_window", "max_output_tokens", "region", "deployment", "risk_classification", "status",
    )
    narrow_fields = ("input_cost_per_1k", "output_cost_per_1k", "currency")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["capabilities"].include(self.instance.capabilities)

    def clean_currency(self):
        currency = (self.cleaned_data.get("currency") or "").strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValidationError("A three-letter currency code, e.g. USD.")
        return currency

    def clean(self):
        cleaned = super().clean()
        provider, model_id = cleaned.get("provider"), cleaned.get("model_id")
        if provider and model_id:
            clash = AIModel.objects.filter(
                organization=self.organization, provider=provider, model_id=model_id,
                version=cleaned.get("version") or "",
            )
            if not self.creating:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                self.add_error("model_id", f"{provider} already has this model ID at this version.")
        return cleaned


# -- Agents ---------------------------------------------------------------------


class AgentForm(OrganizationModelForm):
    adapter_key = forms.ChoiceField(
        label="Engine adapter",
        help_text="Used when the platform drives the agent itself. Reporting engines can leave it on mock.",
    )
    allowed_tools = ListField(
        label="Allowed tools",
        help_text="Comma-separated. An explicit allow-list is IRGF link 04; without one, deny-lists fail open.",
    )
    restricted_tools = ListField(
        label="Restricted tools", help_text="Never available to this agent. The MCP Gateway enforces this list."
    )
    knowledge_sources = ListField(label="Knowledge sources", help_text="What the agent is grounded in.")
    limit_value_per_action = forms.DecimalField(
        label="Max value per action", required=False, min_value=0, max_digits=14, decimal_places=2
    )
    limit_value_per_day = forms.DecimalField(
        label="Max value per day", required=False, min_value=0, max_digits=14, decimal_places=2
    )
    limit_actions_per_hour = forms.IntegerField(label="Max actions per hour", required=False, min_value=1)
    limit_currency = forms.CharField(label="Currency", required=False, max_length=3)
    limit_counterparties = ListField(
        label="Allowed counterparties", help_text="Who the agent may act on or pay. Empty means anyone."
    )
    kill_switch_triggers = ChoiceListField(
        label="Automatic kill-switch triggers",
        choices=list(KILL_SWITCH_TRIGGERS.items()),
        help_text="The Control Plane suspends the agent when one fires. Tier 3-4 needs at least one at Gate G3.",
    )
    options = JSONObjectField(label="Adapter options")

    LIMIT_FIELDS = {
        "limit_value_per_action": "max_value_per_action",
        "limit_value_per_day": "max_value_per_day",
        "limit_actions_per_hour": "max_actions_per_hour",
        "limit_currency": "currency",
        "limit_counterparties": "allowed_counterparties",
    }

    class Meta:
        model = AIAgent
        fields = [
            "name", "agent_id", "description", "business_purpose", "owner", "team",
            "framework", "adapter_key", "provider", "primary_model", "fallback_model", "environment",
            "remote_endpoint", "system_prompt_reference",
            "risk_level", "autonomy_level", "requires_human_approval", "data_classification",
            "allowed_tools", "restricted_tools", "knowledge_sources",
            "out_of_scope", "escalation_thresholds", "kill_switch_conditions", "kill_switch_triggers",
            "options", "status",
        ]
        labels = {
            "agent_id": "Agent ID", "business_purpose": "Business purpose", "primary_model": "Primary model",
            "fallback_model": "Fallback model", "remote_endpoint": "Remote endpoint",
            "system_prompt_reference": "System prompt reference", "risk_level": "Risk",
            "requires_human_approval": "Requires human approval", "out_of_scope": "Not for",
        }
        help_texts = {
            "agent_id": "The identifier your code sends. Leave blank to derive it from the name; it cannot change later.",
            "autonomy_level": "Level 3 or above in production needs human approval under the default policies.",
            "out_of_scope": "What this agent is not for (IRGF link 01).",
        }

    sections = (
        ("Agent", "", ["name", "agent_id", "description", "business_purpose", "owner", "team"]),
        (
            "Runtime",
            "",
            ["framework", "adapter_key", "provider", "primary_model", "fallback_model", "environment",
             "remote_endpoint", "system_prompt_reference"],
        ),
        (
            "Risk and autonomy",
            "",
            ["risk_level", "autonomy_level", "data_classification", "requires_human_approval"],
        ),
        ("Boundaries", "", ["allowed_tools", "restricted_tools", "knowledge_sources"], {"tag": "IRGF links 03-04"}),
        (
            "Agent Card",
            "Required at Gate G3 for any agent that acts: autonomy level 2 or above, or an allow-list of tools.",
            ["out_of_scope", "limit_value_per_action", "limit_value_per_day", "limit_actions_per_hour",
             "limit_currency", "limit_counterparties", "escalation_thresholds", "kill_switch_conditions",
             "kill_switch_triggers"],
            {"tag": "IRGF S3 · link 06"},
        ),
        ("Advanced", "", ["options", "status"]),
    )
    compact_fields = (
        "agent_id", "owner", "team", "framework", "adapter_key", "provider", "environment",
        "primary_model", "fallback_model", "remote_endpoint", "system_prompt_reference",
        "risk_level", "autonomy_level", "data_classification", "limit_counterparties", "status",
    )
    narrow_fields = ("limit_value_per_action", "limit_value_per_day", "limit_actions_per_hour")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["adapter_key"].choices = adapter_choices(AdapterKind.AGENT, self.instance.adapter_key)
        self.fields["kill_switch_triggers"].include(self.instance.kill_switch_triggers)
        self.fields["agent_id"].required = False
        if not self.creating:
            # Engines report against it; renaming would orphan their telemetry.
            self.fields["agent_id"].disabled = True
            self.fields["agent_id"].help_text = "The identifier engines report against. It cannot change."
        limits = self.instance.action_limits or {}
        for field, key in self.LIMIT_FIELDS.items():
            if key in limits:
                self.initial[field] = limits[key]
        self.fields["limit_currency"].widget.attrs["placeholder"] = "USD"

    def clean_agent_id(self):
        if not self.creating:
            return self.instance.agent_id
        return slugify(self.cleaned_data.get("agent_id") or "")[:200]

    def clean(self):
        cleaned = super().clean()
        if self.creating:
            agent_id = cleaned.get("agent_id") or slugify(cleaned.get("name") or "")[:200]
            cleaned["agent_id"] = agent_id
            self.instance.agent_id = agent_id
            if agent_id and AIAgent.objects.filter(organization=self.organization, agent_id=agent_id).exists():
                self.add_error("agent_id" if self.data.get("agent_id") else "name",
                               f"Another agent in this organization already reports as “{agent_id}”.")

        provider, primary = cleaned.get("provider"), cleaned.get("primary_model")
        if primary and not provider:
            cleaned["provider"] = self.instance.provider = primary.provider
        elif primary and provider and primary.provider_id != provider.pk:
            self.add_error("primary_model", f"{primary.model_name} is served by {primary.provider}, not {provider}.")

        overlap = set(cleaned.get("allowed_tools") or []) & set(cleaned.get("restricted_tools") or [])
        if overlap:
            self.add_error("restricted_tools", f"Both allowed and restricted: {', '.join(sorted(overlap))}.")

        limits = {}
        for field, key in self.LIMIT_FIELDS.items():
            value = cleaned.get(field)
            if value in (None, "", []):
                continue
            if isinstance(value, Decimal):
                value = _number(value)
            elif field == "limit_currency":
                value = value.strip().upper()
            limits[key] = value
        try:
            validate_action_limits(limits)
        except ValidationError as exc:
            self.add_error(None, exc)
        else:
            self.instance.action_limits = limits
        return cleaned


# -- Applications ----------------------------------------------------------------


class ApplicationForm(OrganizationModelForm):
    agents = forms.ModelMultipleChoiceField(
        queryset=AIAgent.objects.all(), required=False, widget=forms.CheckboxSelectMultiple,
        help_text="The agents this application is made of.",
    )
    ai_models = forms.ModelMultipleChoiceField(
        queryset=AIModel.objects.all(), required=False, widget=forms.CheckboxSelectMultiple, label="Models",
    )
    providers = forms.ModelMultipleChoiceField(
        queryset=AIProvider.objects.all(), required=False, widget=forms.CheckboxSelectMultiple,
    )
    link_dependencies = forms.BooleanField(
        label="Also link the models and providers its agents use", required=False, initial=True,
    )
    tools = ListField(help_text="Tools the application calls directly, outside its agents.")

    class Meta:
        model = AIApplication
        fields = [
            "name", "description", "business_purpose", "business_owner", "technical_owner", "team",
            "department", "environment", "criticality", "data_classification", "risk_level",
            "production_date", "review_date", "agents", "ai_models", "providers", "tools", "status",
        ]
        labels = {"business_purpose": "Business purpose", "risk_level": "Risk"}
        help_texts = {
            "business_owner": "The accountable owner. At Gate G3 this person holds the system owner's seat.",
            "review_date": "When the registry entry is next reviewed.",
        }

    unique_in_organization = ("name",)
    slug_source = "name"
    DELEGATION_ROWS_MIN = 3

    compact_fields = (
        "business_owner", "technical_owner", "team", "department", "environment", "criticality",
        "data_classification", "risk_level", "production_date", "review_date", "status",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["agents"].label_from_instance = lambda agent: f"{agent.name} · {agent.agent_id}"
        existing = [] if self.creating else list(self.instance.delegations or [])
        self.delegation_rows = max(self.DELEGATION_ROWS_MIN, len(existing) + 2)
        agent_ids = [("", "—")] + [
            (agent.agent_id, agent.name) for agent in self.fields["agents"].queryset.order_by("name")
        ]
        from apps.a2a.models import A2AAgentCard, CardOrigin

        # Work may also be handed outside the organization, to an agent published for A2A.
        self.external_agents = dict(
            A2AAgentCard.objects.filter(organization=self.organization, origin=CardOrigin.EXTERNAL)
            .order_by("name").values_list("slug", "name")
        )
        targets = agent_ids + [(slug, f"{name} · external") for slug, name in self.external_agents.items()]
        for index in range(self.delegation_rows):
            edge = existing[index] if index < len(existing) else {}
            self.fields[f"delegation_{index}_from"] = forms.ChoiceField(
                label="Agent" if index == 0 else "", choices=agent_ids, required=False, initial=edge.get("from", ""),
            )
            self.fields[f"delegation_{index}_to"] = forms.ChoiceField(
                label="May invoke" if index == 0 else "", choices=targets, required=False,
                initial=edge.get("to", ""),
            )
            self.fields[f"delegation_{index}_authority"] = forms.ChoiceField(
                label="With" if index == 0 else "", choices=list(DELEGATION_AUTHORITY.items()),
                required=False, initial=edge.get("authority", "own"),
            )
        if not self.creating:
            self.fields["link_dependencies"].initial = False
        self.apply_styles()

    @property
    def sections(self):
        rows = [
            name for index in range(self.delegation_rows)
            for name in (f"delegation_{index}_from", f"delegation_{index}_to", f"delegation_{index}_authority")
        ]
        return (
            ("Application", "", ["name", "description", "business_purpose", "business_owner", "technical_owner",
                                 "team", "department"]),
            ("Risk and lifecycle", "", ["environment", "criticality", "data_classification", "risk_level",
                                        "production_date", "review_date", "status"]),
            ("Composition", "What the application is made of. Its composite authority is what its agents can reach together.",
             ["agents", "link_dependencies", "ai_models", "providers", "tools"], {"tag": "IRGF chapter 16"}),
            ("Delegation graph",
             "Which of its agents may invoke which -- or which external agent published for A2A -- and with whose "
             "authority. Declared, never inferred from traffic; the Control Plane flags any delegation not declared "
             "here. Tier 3-4 applications with more than one acting agent need one at Gate G3.",
             rows, {"tag": "IRGF chapter 16"}),
        )

    @property
    def narrow_fields(self):
        return tuple(
            name for index in range(self.delegation_rows)
            for name in (f"delegation_{index}_from", f"delegation_{index}_to", f"delegation_{index}_authority")
        )

    def clean(self):
        cleaned = super().clean()
        members = {agent.agent_id for agent in cleaned.get("agents") or []}
        edges = []
        for index in range(self.delegation_rows):
            source = cleaned.get(f"delegation_{index}_from") or ""
            target = cleaned.get(f"delegation_{index}_to") or ""
            if not source and not target:
                continue
            if not source or not target:
                self.add_error(f"delegation_{index}_to" if source else f"delegation_{index}_from",
                               "A delegation names both agents.")
                continue
            outside = sorted(({source} - members) | ({target} - members - set(self.external_agents)))
            if outside:
                self.add_error(f"delegation_{index}_from", f"Not an agent of this application: {', '.join(outside)}.")
                continue
            edge = {"from": source, "to": target, "authority": cleaned.get(f"delegation_{index}_authority") or "own"}
            if edge not in edges:
                edges.append(edge)
        try:
            validate_delegations(edges)
        except ValidationError as exc:
            self.add_error(None, exc)
        else:
            self.instance.delegations = edges
        return cleaned

    def after_save(self, application) -> None:
        if not self.cleaned_data.get("link_dependencies"):
            return
        agents = list(application.agents.select_related("primary_model", "fallback_model", "provider"))
        models = {model for agent in agents for model in (agent.primary_model, agent.fallback_model) if model}
        providers = {agent.provider for agent in agents if agent.provider}
        providers |= {model.provider for model in models}
        application.ai_models.add(*models)
        application.providers.add(*providers)
