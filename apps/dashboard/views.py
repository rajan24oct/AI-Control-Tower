from __future__ import annotations

import json

from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse
from django.views.generic import TemplateView, View

from apps.core.responses import action_response
from apps.dashboard.landscape import ControlTowerService
from apps.dashboard.ranges import PRESETS, DateRange
from apps.dashboard.services import DashboardService
from apps.organizations.middleware import SESSION_KEY


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/home.html"
    extra_context = {
        "page_title": "Dashboard",
        "page_subtitle": "Executive view of the organization's AI estate",
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        date_range = DateRange.from_request(self.request.GET)
        data = DashboardService(self.request.organization, date_range).build()

        context["data"] = data
        context["charts_json"] = json.dumps(data.charts)
        context["date_range"] = date_range
        context["range_presets"] = [
            {"key": key, "label": label} for key, (label, _) in PRESETS.items()
        ]
        return context


class ControlTowerView(LoginRequiredMixin, TemplateView):
    """The flagship AI landscape page (sections 32 and 34).

    Renders the ecosystem as layers -- applications, agents, models, providers
    -- where every node carries live health, risk, cost and execution
    indicators rather than registry metadata alone.
    """

    template_name = "dashboard/control_tower.html"
    extra_context = {
        "page_title": "AI Control Tower",
        "page_subtitle": "The organization's AI ecosystem, end to end",
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization

        ecosystem = ControlTowerService(organization).build()
        context["ecosystem"] = ecosystem
        context["summary"] = ecosystem.summary
        context["attention"] = DashboardService(organization).build().attention
        return context


class IntegrationHandbookView(LoginRequiredMixin, TemplateView):
    """The integration handbook, served by the platform rather than linked out.

    Ships with the code so it is available to every deployment -- including
    air-gapped ones -- and versions alongside the API it documents.
    """

    template_name = "docs/integration_handbook.html"


class SwitchOrganizationView(LoginRequiredMixin, View):
    """Change the active tenant, re-validating membership before switching."""

    def post(self, request, *args, **kwargs):
        organization_id = request.POST.get("organization_id")
        organization = request.user.organizations.filter(pk=organization_id).first() if organization_id else None
        if organization is not None:
            request.session[SESSION_KEY] = organization_id

        # A switch the user is not entitled to make is ignored rather than
        # refused, so the picker cannot be used to probe for other tenants.
        return action_response(
            request,
            message=f"Switched to {organization.name}." if organization else "Organization unchanged.",
            tone="success" if organization else "warning",
            redirect_to=request.POST.get("next") or reverse("dashboard:home"),
        )
