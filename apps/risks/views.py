from __future__ import annotations

import json

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import DetailView, ListView, TemplateView

from apps.agents.models import AIAgent
from apps.applications_registry.models import AIApplication
from apps.core.views import OrganizationScopedMixin, ScopedDetailMixin
from apps.risks.models import Risk, RiskCategory, RiskStatus
from apps.risks.services import RiskService


class RiskListView(OrganizationScopedMixin, ListView):
    model = Risk
    template_name = "risks/risk_list.html"
    context_object_name = "risks"
    paginate_by = 50
    ordering = ["-inherent_risk_score", "risk_id"]
    extra_context = {
        "page_title": "Risk Register",
        "page_subtitle": "Identified AI risks, scored before and after mitigation",
    }
    search_fields = ["risk_id", "title", "description", "mitigation"]
    filter_fields = {
        "category": "risk_category",
        "status": "status",
        "agent": "agent_id",
        "application": "application_id",
    }

    def get_base_queryset(self):
        return (
            super()
            .get_base_queryset()
            .select_related("owner", "agent", "application", "model", "provider")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization
        context.update(
            {
                "categories": RiskCategory.choices,
                "statuses": RiskStatus.choices,
                "agents": AIAgent.objects.for_organization(organization).order_by("name"),
                "applications": AIApplication.objects.for_organization(organization).order_by("name"),
                "summary": RiskService(organization).summary(),
            }
        )
        return context


class RiskDetailView(ScopedDetailMixin, DetailView):
    model = Risk
    template_name = "risks/risk_detail.html"
    context_object_name = "risk"
    extra_context = {"page_title": "Risk"}

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("owner", "agent", "application", "model", "provider")
        )


class RiskMatrixView(LoginRequiredMixin, TemplateView):
    template_name = "risks/matrix.html"
    extra_context = {
        "page_title": "Risk Matrix",
        "page_subtitle": "5 x 5 likelihood and impact heat map",
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization
        if organization is None:
            return context

        service = RiskService(organization)
        show_residual = self.request.GET.get("view") == "residual"

        context.update(
            {
                "grid": service.heat_map(residual=show_residual),
                "show_residual": show_residual,
                "summary": service.summary(),
                "by_category": service.by_category(),
                "distribution_json": json.dumps(service.level_distribution()),
            }
        )
        return context
