"""Unified AI inventory (section 8).

Every list view is organization-scoped in `get_queryset`, so tenant isolation
holds regardless of what the request asks for.
"""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count
from django.views.generic import DetailView, ListView, TemplateView

from apps.agents.models import AgentFramework, AIAgent, AutonomyLevel
from apps.applications_registry.models import AIApplication
from apps.core.constants import Criticality, Environment, HealthStatus, LifecycleStatus, RiskLevel
from apps.core.views import OrganizationScopedMixin, ScopedDetailMixin
from apps.models_registry.models import AIModel, ModelType
from apps.providers.models import AIProvider, ProviderType


class InventoryIndexView(LoginRequiredMixin, TemplateView):
    template_name = "inventory/index.html"
    extra_context = {"page_title": "AI Inventory", "page_subtitle": "Every AI asset the organization owns"}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization
        context["counts"] = {
            "applications": AIApplication.objects.for_organization(organization).count(),
            "agents": AIAgent.objects.for_organization(organization).count(),
            "models": AIModel.objects.for_organization(organization).count(),
            "providers": AIProvider.objects.for_organization(organization).count(),
        }
        return context


# -- Applications -----------------------------------------------------------


class ApplicationListView(OrganizationScopedMixin, ListView):
    model = AIApplication
    template_name = "inventory/application_list.html"
    extra_context = {"page_title": "AI Applications"}
    context_object_name = "applications"
    paginate_by = 25
    ordering = ["name"]
    search_fields = ["name", "description", "department", "business_purpose"]
    filter_fields = {
        "environment": "environment",
        "criticality": "criticality",
        "risk": "risk_level",
        "status": "status",
    }

    def get_base_queryset(self):
        return (
            super()
            .get_base_queryset()
            .select_related("team", "business_owner", "technical_owner")
            .annotate(agent_total=Count("agents", distinct=True))
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["environments"] = Environment.choices
        context["criticalities"] = Criticality.choices
        context["risk_levels"] = RiskLevel.choices
        context["statuses"] = LifecycleStatus.choices
        return context


class ApplicationDetailView(ScopedDetailMixin, DetailView):
    model = AIApplication
    template_name = "inventory/application_detail.html"
    extra_context = {"page_title": "Application"}
    context_object_name = "application"

    def get_queryset(self):
        return super().get_queryset().prefetch_related("agents", "ai_models", "providers")


# -- Agents -----------------------------------------------------------------


class AgentListView(OrganizationScopedMixin, ListView):
    model = AIAgent
    template_name = "inventory/agent_list.html"
    extra_context = {"page_title": "AI Agents"}
    context_object_name = "agents"
    paginate_by = 25
    ordering = ["name"]
    search_fields = ["name", "agent_id", "description", "business_purpose"]
    filter_fields = {
        "framework": "framework",
        "environment": "environment",
        "risk": "risk_level",
        "autonomy": "autonomy_level",
        "status": "status",
    }

    def get_base_queryset(self):
        return super().get_base_queryset().select_related("provider", "primary_model", "team", "owner")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["frameworks"] = AgentFramework.choices
        context["environments"] = Environment.choices
        context["risk_levels"] = RiskLevel.choices
        context["autonomy_levels"] = AutonomyLevel.choices
        context["statuses"] = LifecycleStatus.choices
        return context


class AgentDetailView(ScopedDetailMixin, DetailView):
    model = AIAgent
    template_name = "inventory/agent_detail.html"
    extra_context = {"page_title": "Agent"}
    context_object_name = "agent"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("provider", "primary_model", "fallback_model", "team", "owner")
            .prefetch_related("applications")
        )


# -- Models -----------------------------------------------------------------


class ModelListView(OrganizationScopedMixin, ListView):
    model = AIModel
    template_name = "inventory/model_list.html"
    extra_context = {"page_title": "AI Models"}
    context_object_name = "ai_models"
    paginate_by = 25
    ordering = ["provider__name", "model_name"]
    search_fields = ["model_name", "model_id", "model_family"]
    filter_fields = {
        "provider": "provider_id",
        "type": "model_type",
        "approved": "approved_for_use",
        "status": "status",
    }

    def get_base_queryset(self):
        return super().get_base_queryset().select_related("provider")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["providers"] = AIProvider.objects.for_organization(self.request.organization)
        context["model_types"] = ModelType.choices
        context["statuses"] = LifecycleStatus.choices
        return context


class ModelDetailView(ScopedDetailMixin, DetailView):
    model = AIModel
    template_name = "inventory/model_detail.html"
    extra_context = {"page_title": "Model"}
    context_object_name = "ai_model"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("provider")
            .prefetch_related("applications", "primary_for_agents")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.evaluations.services import EvaluationService

        # The evidence behind the approval badge, shown next to it. An approval
        # with nothing behind it should look like one.
        context["latest_evaluation"] = EvaluationService.latest_for_model(self.object)
        context["evaluation_gates"] = self._gates(self.request.organization)
        return context

    @staticmethod
    def _gates(organization):
        """Active policies that require evaluation evidence, if any."""
        from apps.core.constants import LifecycleStatus
        from apps.policies.models import Policy, PolicyType

        return [
            policy
            for policy in Policy.objects.for_organization(organization).filter(
                policy_type=PolicyType.MODEL_APPROVAL, status=LifecycleStatus.ACTIVE
            )
            if policy.option("require_evaluation", False)
        ]


# -- Providers --------------------------------------------------------------


class ProviderListView(OrganizationScopedMixin, ListView):
    model = AIProvider
    template_name = "inventory/provider_list.html"
    extra_context = {"page_title": "AI Providers"}
    context_object_name = "providers"
    paginate_by = 25
    ordering = ["name"]
    search_fields = ["name", "description", "endpoint", "adapter_key"]
    filter_fields = {
        "type": "provider_type",
        "health": "health_status",
        "status": "status",
    }

    def get_base_queryset(self):
        return super().get_base_queryset().annotate(model_total=Count("models", distinct=True))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["provider_types"] = ProviderType.choices
        context["health_statuses"] = HealthStatus.choices
        context["statuses"] = LifecycleStatus.choices
        return context


class ProviderDetailView(ScopedDetailMixin, DetailView):
    model = AIProvider
    template_name = "inventory/provider_detail.html"
    extra_context = {"page_title": "Provider"}
    context_object_name = "provider"

    def get_queryset(self):
        return super().get_queryset().prefetch_related("models", "agents", "applications")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        provider = self.object

        # Surface what the bound adapter can actually do, alongside the cached
        # capability list on the record.
        try:
            adapter = provider.get_adapter()
            context["adapter_capabilities"] = adapter.capability_list()
            context["adapter_available"] = True
        except Exception as exc:
            context["adapter_capabilities"] = []
            context["adapter_available"] = False
            context["adapter_error"] = str(exc)

        return context
