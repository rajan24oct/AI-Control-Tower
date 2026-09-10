"""FinOps views (sections 15 and 16)."""

from __future__ import annotations

import json

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.generic import ListView, TemplateView, View

from apps.accounts.roles import Perm
from apps.core.responses import action_response
from apps.core.views import OrganizationScopedMixin
from apps.costs.models import Budget, BudgetAlert, PricingVersion
from apps.costs.services import BudgetService, CostAnalyticsService


class CostDashboardView(LoginRequiredMixin, TemplateView):
    template_name = "costs/dashboard.html"
    extra_context = {
        "page_title": "AI Costs",
        "page_subtitle": "Where the organization's AI spend is going",
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization
        if organization is None:
            return context

        try:
            days = int(self.request.GET.get("days") or 30)
        except ValueError:
            days = 30
        days = days if days in {7, 30, 90} else 30

        analytics = CostAnalyticsService(organization, days=days)
        by_provider = analytics.by("provider", "provider__name")

        context.update(
            {
                "days": days,
                "summary": analytics.summary(),
                "by_provider": by_provider,
                "provider_chart_json": json.dumps(
                    {
                        "labels": [row["label"] for row in by_provider],
                        "values": [round(float(row["cost"]), 4) for row in by_provider],
                    }
                ),
                "by_model": analytics.by("model", "model__model_name"),
                "by_application": analytics.by("application", "application__name"),
                "by_agent": analytics.by("agent", "agent__name"),
                "by_team": analytics.by_team(),
                "series_json": json.dumps(analytics.daily_series()),
                "pricing_versions": PricingVersion.objects.for_organization(organization)[:5],
                "open_alerts": BudgetAlert.objects.for_organization(organization).filter(
                    acknowledged_at__isnull=True
                )[:5],
            }
        )
        return context


class BudgetListView(OrganizationScopedMixin, ListView):
    model = Budget
    template_name = "costs/budget_list.html"
    context_object_name = "budgets"
    ordering = ["name"]
    extra_context = {"page_title": "Budgets", "page_subtitle": "Spend limits and threshold alerts"}
    search_fields = ["name"]
    filter_fields = {"scope": "scope", "period": "period", "status": "status"}

    def get_base_queryset(self):
        return (
            super()
            .get_base_queryset()
            .select_related("team", "application", "agent", "provider", "model", "owner")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Live spend against each budget, so the page never shows a stale figure.
        context["budget_states"] = [
            BudgetService.status_for(budget) for budget in context["budgets"]
        ]
        context["alerts"] = (
            BudgetAlert.objects.for_organization(self.request.organization)
            .select_related("budget")
            .filter(acknowledged_at__isnull=True)[:20]
        )
        return context


class AcknowledgeAlertView(LoginRequiredMixin, View):
    """Mark a budget alert as seen. Requires cost-management permission."""

    def post(self, request, pk, *args, **kwargs):
        organization = request.organization
        redirect_to = request.POST.get("next") or reverse("costs:budgets")

        if not request.user.has_org_permission(organization, Perm.COST_MANAGE):
            return action_response(
                request,
                message="Your role does not allow acknowledging budget alerts.",
                tone="danger",
                redirect_to=redirect_to,
                status=403,
            )

        alert = get_object_or_404(
            BudgetAlert.objects.for_organization(organization), pk=pk
        )
        if alert.is_acknowledged:
            return action_response(
                request,
                message="That alert was already acknowledged.",
                tone="warning",
                redirect_to=redirect_to,
            )

        alert.acknowledged_at = timezone.now()
        alert.acknowledged_by = request.user
        alert.save(update_fields=["acknowledged_at", "acknowledged_by", "updated_at"])

        return action_response(
            request,
            message=f"Acknowledged the {alert.threshold}% alert on {alert.budget.name}.",
            redirect_to=redirect_to,
        )
