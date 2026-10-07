"""FinOps views (sections 15 and 16): costs, the explorer, developer tools,
governance and optimization."""

from __future__ import annotations

import csv
import json
from datetime import timedelta

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.generic import DetailView, ListView, TemplateView, View

from apps.accounts.roles import Perm
from apps.audit.models import AuditAction
from apps.audit.services import AuditService
from apps.core.manage import can
from apps.core.responses import action_response
from apps.core.views import OrganizationScopedMixin, ScopedDetailMixin
from apps.costs.alerting import CostAlertService, format_value
from apps.costs.devtools import DevToolAnalytics, DevToolSyncService, add_months, month_start, next_month
from apps.costs.explorer import DEFAULT_PATH, DIMENSIONS, METRICS, CostExplorerService
from apps.costs.governance import GovernanceService
from apps.costs.models import (
    Budget,
    BudgetAlert,
    CostAlertEvent,
    CostAlertRule,
    CostRecommendation,
    DevToolSubscription,
    PricingVersion,
    RecommendationCategory,
    RecommendationKind,
    RecommendationStatus,
)
from apps.costs.optimization import OptimizationService, savings_summary
from apps.costs.services import BudgetHealth, BudgetService, CostAnalyticsService


def requested_days(request, default: int = 30) -> int:
    try:
        days = int(request.GET.get("days") or default)
    except ValueError:
        days = default
    return days if days in {7, 30, 90} else default


def script_json(value) -> str:
    """JSON safe to place inside a <script> element: names cannot close it."""
    return json.dumps(value).replace("</", "<\\/")


def denied(request, redirect_to: str, what: str):
    return action_response(
        request,
        message=f"Your role does not allow {what}.",
        tone="danger",
        redirect_to=redirect_to,
        status=403,
    )


# -- Overview -------------------------------------------------------------------------


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

        days = requested_days(self.request)
        analytics = CostAnalyticsService(organization, days=days)
        by_provider = analytics.by("provider", "provider__name")
        today = timezone.now().date()
        devtools = DevToolAnalytics(organization, month_start(today), today + timedelta(days=1))
        governance = GovernanceService(organization)

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
                ).select_related("budget")[:5],
                "devtools_month": sum(row["total"] for row in devtools.individuals()),
                "has_devtools": DevToolSubscription.objects.for_organization(organization).exists(),
                "savings": savings_summary(organization),
                "health": governance.health_counts(governance.budget_states()),
            }
        )
        return context


# -- Explorer -------------------------------------------------------------------------


def explorer_filters(request) -> dict[str, str]:
    return {key: request.GET.get(f"f_{key}", "").strip() for key in DIMENSIONS if request.GET.get(f"f_{key}")}


class CostExplorerView(LoginRequiredMixin, TemplateView):
    template_name = "costs/explorer.html"
    extra_context = {
        "page_title": "Cost Explorer",
        "page_subtitle": "Drill from department to team, agent, model and provider",
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        dimension = self.request.GET.get("dim", "department")
        context["initial_state_json"] = json.dumps({
            "dim": dimension if dimension in DIMENSIONS else "department",
            "metric": self.request.GET.get("metric", "cost") if self.request.GET.get("metric") in METRICS else "cost",
            "days": requested_days(self.request),
            "filters": explorer_filters(self.request),
            "path": DEFAULT_PATH,
        })
        context["data_url"] = reverse("costs:explorer_data")
        return context


class CostExplorerDataView(LoginRequiredMixin, View):
    """The explorer's data, as JSON for the page or CSV for a spreadsheet."""

    def get(self, request, *args, **kwargs):
        organization = request.organization
        if organization is None:
            return JsonResponse({"error": "No organization selected."}, status=400)
        service = CostExplorerService(organization, days=requested_days(request), filters=explorer_filters(request))
        data = service.breakdown(request.GET.get("dim", "department"), request.GET.get("metric", "cost"))

        if request.GET.get("format") == "csv":
            response = HttpResponse(content_type="text/csv")
            response["Content-Disposition"] = (
                f'attachment; filename="ai-cost-by-{data["dimension"]}-{data["period"]["end"]}.csv"'
            )
            writer = csv.writer(response)
            for crumb in data["breadcrumbs"]:
                writer.writerow([f"# {crumb['dimension_label']}", crumb["label"]])
            writer.writerow([
                data["dimension_label"], "Cost (USD)", "Previous period (USD)", "Change %", "Share %",
                "Runs", "Tokens", "Avg cost per run", "Cost of failed runs",
            ])
            for row in data["rows"]:
                writer.writerow([
                    row["label"], row["cost"], row["prev_cost"],
                    "" if row["change_pct"] is None else row["change_pct"], row["share"],
                    row["runs"], row["tokens"], row["avg_cost"], row["failed_cost"],
                ])
            return response
        return JsonResponse(data)


# -- Developer tools (GitHub Copilot) -------------------------------------------------

DEVTOOL_PERIODS = {
    "month": "This month",
    "last_month": "Last month",
    "quarter": "Last 3 months",
}


def devtool_window(period: str):
    today = timezone.now().date()
    this_month = month_start(today)
    if period == "last_month":
        start = add_months(this_month, -1)
        return start, next_month(start)
    if period == "quarter":
        return add_months(this_month, -2), today + timedelta(days=1)
    return this_month, today + timedelta(days=1)


class DeveloperToolsView(LoginRequiredMixin, TemplateView):
    template_name = "costs/developer_tools.html"
    extra_context = {
        "page_title": "Developer Tools",
        "page_subtitle": "GitHub Copilot spend by team and by individual",
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization
        if organization is None:
            return context

        period = self.request.GET.get("period", "month")
        period = period if period in DEVTOOL_PERIODS else "month"
        start, end = devtool_window(period)
        analytics = DevToolAnalytics(organization, start, end)
        individuals = analytics.individuals()
        teams = analytics.by_team(individuals)
        subscriptions = DevToolSubscription.objects.for_organization(organization).annotate(
            seat_count=Count("seats", filter=Q(seats__is_active=True))
        )
        idle = [row for row in individuals if row["is_active"] and (row["idle_days"] is None or row["idle_days"] >= 30)]

        context.update({
            "period": period,
            "periods": DEVTOOL_PERIODS,
            "start": start,
            "end": end - timedelta(days=1),
            "subscriptions": subscriptions,
            "kpis": {
                "seats": sum(1 for row in individuals if row["is_active"]),
                "idle": len(idle),
                "idle_cost": sum(row["license"] for row in idle),
                "license": sum(row["license"] for row in individuals),
                "premium": sum(row["premium_cost"] for row in individuals),
                "total": sum(row["total"] for row in individuals),
                "requests": sum(row["premium_requests"] for row in individuals),
                "overage": sum(row["overage_requests"] for row in individuals),
            },
            "data_json": script_json({
                "individuals": individuals,
                "teams": teams,
                "trend": analytics.monthly_trend(),
                "initial_team": self.request.GET.get("team", ""),
                "seat_edit_url": reverse("costs:seat_edit", args=["00000000-0000-0000-0000-000000000000"]),
            }),
            "can_manage": can(self.request, Perm.COST_MANAGE),
        })
        return context


class SyncSubscriptionView(LoginRequiredMixin, View):
    def post(self, request, pk, *args, **kwargs):
        redirect_to = reverse("costs:developer_tools")
        if not can(request, Perm.COST_MANAGE):
            return denied(request, redirect_to, "syncing developer-tool subscriptions")
        subscription = get_object_or_404(DevToolSubscription.objects.for_organization(request.organization), pk=pk)
        result = DevToolSyncService.sync(subscription)
        if not result.ok:
            return action_response(
                request, message=f"Sync of {subscription.name} failed: {result.message}", tone="danger",
                redirect_to=redirect_to, status=502,
            )
        return action_response(request, message=f"Synced {subscription.name}: {result.message}", redirect_to=redirect_to)


# -- Budgets --------------------------------------------------------------------------


class BudgetListView(OrganizationScopedMixin, ListView):
    model = Budget
    template_name = "costs/budget_list.html"
    context_object_name = "budgets"
    ordering = ["name"]
    extra_context = {"page_title": "Budgets", "page_subtitle": "Spend limits and threshold alerts"}
    search_fields = ["name"]
    filter_fields = {"scope": "scope", "period": "period", "status": "status", "source": "cost_source"}
    # Spend and utilisation are computed per budget, not stored, so they are
    # shown but not offered as sort keys.
    sort_fields = {
        "budget": "name",
        "scope": "scope",
        "period": "period",
        "limit": "amount",
        "owner": ["owner__first_name", "owner__last_name"],
        "status": "status",
    }

    def get_base_queryset(self):
        return (
            super()
            .get_base_queryset()
            .select_related("team", "user", "application", "agent", "provider", "model", "owner")
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


class BudgetDetailView(ScopedDetailMixin, DetailView):
    model = Budget
    template_name = "costs/budget_detail.html"
    context_object_name = "budget"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        budget = self.object
        state = BudgetService.status_for(budget)
        context.update({
            "page_title": budget.name,
            "page_subtitle": f"{budget.get_scope_display()} budget · {budget.scope_label}",
            "state": state,
            "burn_json": script_json(GovernanceService.burn_series(budget, state)),
            "contributors": GovernanceService.contributors(budget, state),
            "alerts": budget.alerts.order_by("-created_at")[:25],
            "recipients": budget.alert_recipients(),
            "can_manage": can(self.request, Perm.COST_MANAGE),
        })
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
            message=f"Acknowledged “{alert}”.",
            redirect_to=redirect_to,
        )


# -- Governance -----------------------------------------------------------------------


class GovernanceView(LoginRequiredMixin, TemplateView):
    template_name = "costs/governance.html"
    extra_context = {
        "page_title": "Cost Governance",
        "page_subtitle": "Budgets, thresholds, alert rules and who is told",
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization
        if organization is None:
            return context
        governance = GovernanceService(organization)
        states = governance.budget_states()
        rules = list(
            CostAlertRule.objects.for_organization(organization)
            .select_related("team", "user", "agent", "application", "provider", "model", "organization")
            .annotate(event_count=Count("events"))
        )
        for rule in rules:
            rule.last_value_display = format_value(rule, rule.last_value)
            rule.threshold_display = format_value(rule, rule.threshold)
        events = list(
            CostAlertEvent.objects.for_organization(organization)
            .select_related("rule").filter(acknowledged_at__isnull=True)[:15]
        )
        budget_alerts = list(
            BudgetAlert.objects.for_organization(organization)
            .select_related("budget").filter(acknowledged_at__isnull=True)[:15]
        )
        context.update({
            "states": states,
            "health": governance.health_counts(states),
            "health_labels": BudgetHealth.LABELS,
            "health_styles": BudgetHealth.STYLES,
            "coverage": governance.coverage(),
            "rules": rules,
            "events": events,
            "budget_alerts": budget_alerts,
            "open_alert_count": len(events) + len(budget_alerts),
            "unbudgeted": CostRecommendation.objects.for_organization(organization).filter(
                kind=RecommendationKind.UNBUDGETED_SPEND,
                status__in=[RecommendationStatus.OPEN, RecommendationStatus.ACCEPTED],
            ),
            "can_manage": can(self.request, Perm.COST_MANAGE),
        })
        return context


class EvaluateNowView(LoginRequiredMixin, View):
    """Check every budget and alert rule now, rather than at the next sweep."""

    def post(self, request, *args, **kwargs):
        redirect_to = reverse("costs:governance")
        if not can(request, Perm.COST_MANAGE):
            return denied(request, redirect_to, "running cost checks")
        alerts = BudgetService.evaluate_all(request.organization)
        events = CostAlertService.evaluate_all(request.organization)
        found = len(alerts) + len(events)
        message = (
            f"Checked budgets and alert rules: {len(alerts)} new budget alert{'s' if len(alerts) != 1 else ''}, "
            f"{len(events)} rule alert{'s' if len(events) != 1 else ''}."
        )
        return action_response(request, message=message, tone="warning" if found else "success", redirect_to=redirect_to)


class AcknowledgeEventView(LoginRequiredMixin, View):
    def post(self, request, pk, *args, **kwargs):
        redirect_to = request.POST.get("next") or reverse("costs:governance")
        if not can(request, Perm.COST_MANAGE):
            return denied(request, redirect_to, "acknowledging cost alerts")
        event = get_object_or_404(CostAlertEvent.objects.for_organization(request.organization), pk=pk)
        if not event.is_acknowledged:
            event.acknowledged_at = timezone.now()
            event.acknowledged_by = request.user
            event.save(update_fields=["acknowledged_at", "acknowledged_by", "updated_at"])
        return action_response(request, message=f"Acknowledged the alert from {event.rule.name}.", redirect_to=redirect_to)


# -- Optimization ---------------------------------------------------------------------


class OptimizationView(OrganizationScopedMixin, ListView):
    model = CostRecommendation
    template_name = "costs/optimization.html"
    context_object_name = "recommendations"
    paginate_by = 25
    extra_context = {
        "page_title": "Cost Optimization",
        "page_subtitle": "Costed recommendations to spend less on AI",
    }
    search_fields = ["title", "rationale"]
    filter_fields = {"category": "category", "kind": "kind", "confidence": "confidence"}
    sort_fields = {
        "savings": "estimated_monthly_savings",
        "title": "title",
        "confidence": "confidence",
        "status": "status",
        "detected": "last_detected_at",
    }

    def get_base_queryset(self):
        return super().get_base_queryset().select_related(
            "agent", "model", "suggested_model", "team", "subscription", "decided_by"
        )

    def get_queryset(self):
        queryset = super().get_queryset()
        status = self.request.GET.get("status", "actionable")
        if status == "actionable":
            queryset = queryset.filter(status__in=[RecommendationStatus.OPEN, RecommendationStatus.ACCEPTED])
        elif status in RecommendationStatus.values:
            queryset = queryset.filter(status=status)
        if not self.request.GET.get("sort"):
            queryset = queryset.order_by("-estimated_monthly_savings", "title")
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization
        summary = savings_summary(organization)
        actionable = CostRecommendation.objects.for_organization(organization).filter(
            status__in=[RecommendationStatus.OPEN, RecommendationStatus.ACCEPTED]
        )
        # Governance findings save nothing directly; a zero bar would only be noise.
        by_kind = [
            row for row in actionable.values("kind")
            .annotate(savings=Sum("estimated_monthly_savings"), count=Count("id")).order_by("-savings")
            if row["savings"]
        ]
        kinds = dict(RecommendationKind.choices)
        context.update({
            "summary": summary,
            "status_filter": self.request.GET.get("status", "actionable"),
            "statuses": RecommendationStatus.choices,
            "categories": RecommendationCategory.choices,
            "kinds": RecommendationKind.choices,
            "category_labels": dict(RecommendationCategory.choices),
            "chart_json": script_json({
                "labels": [kinds.get(row["kind"], row["kind"]) for row in by_kind],
                "values": [float(row["savings"] or 0) for row in by_kind],
                "counts": [row["count"] for row in by_kind],
            }),
            "last_run": actionable.order_by("-last_detected_at").values_list("last_detected_at", flat=True).first(),
            "can_manage": can(self.request, Perm.COST_MANAGE),
        })
        return context


class RunOptimizationView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        redirect_to = reverse("costs:optimization")
        if not can(request, Perm.COST_MANAGE):
            return denied(request, redirect_to, "running the cost analysis")
        result = OptimizationService(request.organization).run()
        return action_response(
            request,
            message=(
                f"Analysis found {result['found']} recommendation{'s' if result['found'] != 1 else ''}: "
                f"{result['created']} new, {result['resolved']} no longer apply."
            ),
            redirect_to=redirect_to,
        )


DECISIONS = {
    "accept": RecommendationStatus.ACCEPTED,
    "implement": RecommendationStatus.IMPLEMENTED,
    "dismiss": RecommendationStatus.DISMISSED,
    "reopen": RecommendationStatus.OPEN,
}


class RecommendationDecisionView(LoginRequiredMixin, View):
    def post(self, request, pk, *args, **kwargs):
        redirect_to = request.POST.get("next") or reverse("costs:optimization")
        if not can(request, Perm.COST_MANAGE):
            return denied(request, redirect_to, "deciding on cost recommendations")
        recommendation = get_object_or_404(
            CostRecommendation.objects.for_organization(request.organization), pk=pk
        )
        status = DECISIONS.get(request.POST.get("decision", ""))
        if status is None:
            return action_response(request, message="Unknown decision.", tone="danger", redirect_to=redirect_to, status=400)
        previous = recommendation.status
        recommendation.status = status
        recommendation.decision_note = request.POST.get("note", "").strip()[:2000]
        recommendation.decided_at = timezone.now() if status != RecommendationStatus.OPEN else None
        recommendation.decided_by = request.user if status != RecommendationStatus.OPEN else None
        recommendation.save(update_fields=["status", "decision_note", "decided_at", "decided_by", "updated_at"])
        AuditService.log(
            action=AuditAction.UPDATE,
            resource_type=CostRecommendation._meta.label,
            resource_id=recommendation.pk,
            resource_label=recommendation.title,
            organization=request.organization,
            actor=request.user,
            old_value={"status": previous},
            new_value={"status": status, "note": recommendation.decision_note,
                       "estimated_monthly_savings": str(recommendation.estimated_monthly_savings)},
        )
        return action_response(
            request,
            message=f"Marked “{recommendation.title}” as {recommendation.get_status_display().lower()}.",
            redirect_to=redirect_to,
        )
