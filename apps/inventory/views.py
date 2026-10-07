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
    sort_fields = {
        "application": "name",
        "team": "team__name",
        "environment": "environment",
        "criticality": ("criticality", Criticality.values),
        "risk": ("risk_level", RiskLevel.values),
        "agents": "agent_total",
        "owner": ["business_owner__first_name", "business_owner__last_name"],
        "status": "status",
    }
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
            # Meta.ordering does not survive an aggregate, so pages would shuffle.
            .order_by("name")
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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.classification.services import ClassificationService
        from apps.classification.views import can_score

        context["classification"] = ClassificationService.current(self.object)
        context["can_score"] = can_score(self.request, self.object)

        from apps.controlplane.models import UNRESOLVED_STATUSES
        from apps.lifecycle.models import OPEN_CHANGE_STATUSES
        from apps.lifecycle.services import AuthorizationService, composite_authority, display
        from apps.lifecycle.views import can_request

        application = self.object
        context.update(
            {
                "authorization": AuthorizationService.current(application),
                "open_changes": application.change_records.filter(status__in=OPEN_CHANGE_STATUSES)[:5],
                "can_request_authorization": can_request(self.request, application),
                "composite_text": display("composite", composite_authority(application)),
                "drift": application.drift_records.filter(status__in=UNRESOLVED_STATUSES)[:5],
            }
        )
        return context


# -- Agents -----------------------------------------------------------------


class AgentListView(OrganizationScopedMixin, ListView):
    model = AIAgent
    template_name = "inventory/agent_list.html"
    extra_context = {"page_title": "AI Agents"}
    context_object_name = "agents"
    paginate_by = 25
    ordering = ["name"]
    search_fields = ["name", "agent_id", "description", "business_purpose"]
    sort_fields = {
        "agent": "name",
        "framework": "framework",
        "model": "primary_model__model_name",
        "provider": "provider__name",
        "environment": "environment",
        "risk": ("risk_level", RiskLevel.values),
        "autonomy": "autonomy_level",
        # The column shows "required" when the flag is set *or* autonomy forces
        # it; flag then autonomy keeps every "required" row on the same side.
        "approval": ["requires_human_approval", "autonomy_level"],
        "status": "status",
    }
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
            .prefetch_related("applications", "service_accounts")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.accounts.roles import Perm
        from apps.agents.services import governance_chain
        from apps.classification.services import ClassificationService
        from apps.classification.views import can_score

        agent = self.object
        context.update(
            {
                "classification": ClassificationService.current(agent),
                "can_score": can_score(self.request, agent),
                "governance_chain": governance_chain(agent),
                "card_gaps": agent.agent_card_gaps() if agent.acts else [],
                "can_suspend": self.request.user.has_org_permission(
                    self.request.organization, Perm.AGENT_SUSPEND
                ),
            }
        )

        from apps.lifecycle.models import OPEN_CHANGE_STATUSES, ExceptionStatus
        from apps.lifecycle.services import AuthorizationService
        from apps.lifecycle.views import can_request

        context.update(
            {
                "authorization": AuthorizationService.current(agent),
                "open_changes": agent.change_records.filter(status__in=OPEN_CHANGE_STATUSES)[:5],
                "exceptions": agent.governance_exceptions.select_related("policy").filter(
                    status__in=[ExceptionStatus.PENDING, ExceptionStatus.ACTIVE]
                ),
                "can_request_authorization": can_request(self.request, agent),
            }
        )

        from apps.a2a.services import A2AGateway
        from apps.controlplane.services import ControlPlane

        context["control_plane"] = ControlPlane.agent_summary(agent)
        context["a2a"] = A2AGateway.agent_summary(agent)
        return context


# -- Models -----------------------------------------------------------------


class ModelListView(OrganizationScopedMixin, ListView):
    model = AIModel
    template_name = "inventory/model_list.html"
    extra_context = {"page_title": "AI Models"}
    context_object_name = "ai_models"
    paginate_by = 25
    ordering = ["provider__name", "model_name"]
    search_fields = ["model_name", "model_id", "model_family"]
    sort_fields = {
        "model": "model_name",
        "provider": "provider__name",
        "type": "model_type",
        "context": "context_window",
        "input": "input_cost_per_1k",
        "output": "output_cost_per_1k",
        "approved": "approved_for_use",
        "risk": ("risk_classification", RiskLevel.values),
        "status": "status",
    }
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
    # Credential status is resolved from the environment at request time, so
    # it is shown but not offered as a sort key.
    sort_fields = {
        "provider": "name",
        "type": "provider_type",
        "adapter": "adapter_key",
        "region": "region",
        "models": "model_total",
        "health": ("health_status", HealthStatus.values),
        "approved": "is_approved",
        "status": "status",
    }
    filter_fields = {
        "type": "provider_type",
        "health": "health_status",
        "status": "status",
    }

    def get_base_queryset(self):
        return (
            super()
            .get_base_queryset()
            .annotate(model_total=Count("models", distinct=True))
            # Meta.ordering does not survive an aggregate, so pages would shuffle.
            .order_by("name")
        )

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
