"""Model routing views."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import DetailView, ListView, TemplateView

from apps.core.constants import Environment
from apps.core.views import OrganizationScopedMixin, ScopedDetailMixin
from apps.models_registry.models import ModelType
from apps.routing.models import RoutingDecision, RoutingPolicy, RoutingStrategy
from apps.routing.services import RoutingRequest, RoutingService, RoutingStats


class RoutingPolicyListView(OrganizationScopedMixin, ListView):
    model = RoutingPolicy
    template_name = "routing/policy_list.html"
    context_object_name = "policies"
    paginate_by = 25
    extra_context = {
        "page_title": "Model Routing",
        "page_subtitle": "Smart model selection and automatic fallback across providers",
    }
    search_fields = ["name", "description"]
    filter_fields = {"strategy": "strategy", "model_type": "model_type", "status": "status"}

    def get_queryset(self):
        return super().get_queryset().prefetch_related("agents", "allowed_providers")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["strategies"] = RoutingStrategy.choices
        context["stats"] = RoutingStats(self.request.organization).build()
        context["recent"] = (
            RoutingDecision.objects.for_organization(self.request.organization)
            .select_related("selected_model", "served_model", "policy", "agent")
            .order_by("-created_at")[:15]
        )
        return context


class RoutingPolicyDetailView(ScopedDetailMixin, DetailView):
    model = RoutingPolicy
    template_name = "routing/policy_detail.html"
    context_object_name = "policy"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        policy = self.object

        # Show what this policy would do right now. A routing rule you cannot
        # see the effect of is a rule nobody will trust enough to change.
        plan = RoutingService.plan(
            self.request.organization,
            RoutingRequest(
                model_type=policy.model_type,
                environment=(policy.environments or [Environment.PRODUCTION])[0],
            ),
        )
        context["plan"] = plan
        context["page_title"] = policy.name
        context["page_subtitle"] = policy.get_strategy_display()
        context["decisions"] = (
            policy.decisions.select_related("selected_model", "served_model")
            .order_by("-created_at")[:10]
        )
        return context


class RoutingSimulatorView(LoginRequiredMixin, TemplateView):
    """Answer "what would route now, and why?" without an engine.

    The exclusions matter as much as the plan: most routing questions turn out
    to be "why is my model *not* being chosen?".
    """

    template_name = "routing/simulator.html"
    extra_context = {
        "page_title": "Routing Simulator",
        "page_subtitle": "What the platform would choose right now, and why",
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        params = self.request.GET

        model_type = params.get("model_type") or ModelType.CHAT
        environment = params.get("environment") or Environment.PRODUCTION
        capabilities = [c for c in params.getlist("capability") if c]

        context["model_type"] = model_type
        context["environment"] = environment
        context["selected_capabilities"] = capabilities
        context["model_types"] = ModelType.choices
        context["environments"] = Environment.choices

        if self.request.organization is not None:
            context["plan"] = RoutingService.plan(
                self.request.organization,
                RoutingRequest(
                    model_type=model_type,
                    environment=environment,
                    required_capabilities=tuple(capabilities),
                ),
            )
        return context


class RoutingDecisionListView(OrganizationScopedMixin, ListView):
    model = RoutingDecision
    template_name = "routing/decision_list.html"
    context_object_name = "decisions"
    paginate_by = 50
    ordering = ["-created_at"]
    extra_context = {
        "page_title": "Routing Decisions",
        "page_subtitle": "Every plan issued, and what actually served",
    }
    search_fields = ["reason", "selected_model__model_name"]
    filter_fields = {"outcome": "outcome", "strategy": "strategy", "policy": "policy_id"}

    def get_queryset(self):
        return super().get_queryset().select_related(
            "selected_model", "served_model", "policy", "agent", "selected_model__provider"
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["stats"] = RoutingStats(self.request.organization).build()
        return context
