"""Execution monitoring views (sections 13 and 25)."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Avg, Count, Sum
from django.utils import timezone
from django.views.generic import DetailView, ListView, TemplateView

from apps.agents.models import AIAgent
from apps.applications_registry.models import AIApplication
from apps.core.constants import Environment
from apps.core.views import OrganizationScopedMixin, ScopedDetailMixin
from apps.executions.models import ApprovalState, Execution, ExecutionStatusChoices
from apps.executions.prompts import PromptService
from apps.models_registry.models import AIModel
from apps.providers.models import AIProvider

LIVE_STATUSES = [
    ExecutionStatusChoices.QUEUED,
    ExecutionStatusChoices.RUNNING,
    ExecutionStatusChoices.WAITING_FOR_APPROVAL,
]


class ExecutionListView(OrganizationScopedMixin, ListView):
    model = Execution
    template_name = "executions/execution_list.html"
    context_object_name = "executions"
    paginate_by = 50
    ordering = ["-started_at"]
    extra_context = {
        "page_title": "Executions",
        "page_subtitle": "Every AI run reported into the Control Tower",
    }
    search_fields = ["external_id", "trace__trace_id", "error_message"]
    sort_fields = {
        "execution": "external_id",
        "agent": "agent__name",
        "application": "application__name",
        "model": "model__model_name",
        "provider": "provider__name",
        "started": "started_at",
        "duration": "latency_ms",
        "tokens": "total_tokens",
        "cost": "cost",
        "status": "status",
        "approval": "approval_status",
    }
    filter_fields = {
        "status": "status",
        "agent": "agent_id",
        "application": "application_id",
        "provider": "provider_id",
        "model": "model_id",
        "environment": "environment",
        "approval": "approval_status",
    }

    def get_base_queryset(self):
        return (
            super()
            .get_base_queryset()
            .select_related("agent", "application", "provider", "model", "trace")
        )

    def get_queryset(self):
        queryset = super().get_queryset()

        days = self.request.GET.get("days", "").strip()
        if days.isdigit():
            queryset = queryset.filter(
                started_at__gte=timezone.now() - timedelta(days=int(days))
            )
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization

        context.update(
            {
                "statuses": ExecutionStatusChoices.choices,
                "approval_states": ApprovalState.choices,
                "environments": Environment.choices,
                "agents": AIAgent.objects.for_organization(organization).order_by("name"),
                "applications": AIApplication.objects.for_organization(organization).order_by("name"),
                "providers": AIProvider.objects.for_organization(organization).order_by("name"),
                "ai_models": AIModel.objects.for_organization(organization).order_by("model_name"),
                "selected_days": self.request.GET.get("days", ""),
                "summary": self._summary(),
            }
        )
        return context

    def _summary(self) -> dict:
        """Aggregates over the *filtered* set, so the numbers match the table."""
        queryset = self.get_queryset()
        totals = queryset.aggregate(
            total=Count("id"),
            tokens=Sum("total_tokens"),
            cost=Sum("cost"),
            latency=Avg("latency_ms"),
        )
        return {
            "total": totals["total"] or 0,
            "tokens": totals["tokens"] or 0,
            "cost": totals["cost"] or 0,
            "latency": int(totals["latency"]) if totals["latency"] else 0,
            "failed": queryset.filter(status=ExecutionStatusChoices.FAILED).count(),
        }


class ExecutionDetailView(ScopedDetailMixin, DetailView):
    model = Execution
    template_name = "executions/execution_detail.html"
    context_object_name = "execution"
    extra_context = {"page_title": "Execution"}

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("agent", "application", "provider", "model", "trace", "service_account")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        execution = self.object

        # The timeline is built from stored events, never from a hardcoded
        # sequence of steps (section 13).
        context["events"] = execution.events.order_by("sequence", "timestamp")
        context["related_executions"] = (
            Execution.objects.for_organization(execution.organization)
            .filter(trace=execution.trace)
            .exclude(pk=execution.pk)
            .select_related("agent")[:10]
            if execution.trace_id
            else []
        )
        context["raw_payloads"] = execution.raw_payloads.all()[:5]
        return context


class LiveMonitorView(LoginRequiredMixin, TemplateView):
    """Operations view for runs currently in flight (section 25)."""

    template_name = "executions/live_monitor.html"
    extra_context = {
        "page_title": "Live Monitor",
        "page_subtitle": "Executions in flight, refreshed automatically",
    }


class LiveMonitorTableView(LoginRequiredMixin, TemplateView):
    """HTMX polling target.

    Deliberately a plain polled partial rather than a WebSocket: the update
    cadence here is seconds, and section 25 warns against adding real-time
    machinery where it is not needed.
    """

    template_name = "executions/partials/_live_table.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization
        since = timezone.now() - timedelta(hours=24)

        base = Execution.objects.for_organization(organization)
        live = (
            base.filter(status__in=LIVE_STATUSES)
            .select_related("agent", "application", "model", "provider")
            .order_by("-started_at")[:50]
        )
        recent = (
            base.filter(started_at__gte=since)
            .exclude(status__in=LIVE_STATUSES)
            .select_related("agent", "application", "model", "provider")
            .order_by("-started_at")[:25]
        )

        window = base.filter(started_at__gte=since)
        context.update(
            {
                "live_executions": PromptService.attach(
                    live, viewer=self.request.user, organization=organization
                ),
                "recent_executions": recent,
                "stats": {
                    "live": base.filter(status__in=LIVE_STATUSES).count(),
                    "awaiting_approval": base.filter(
                        status=ExecutionStatusChoices.WAITING_FOR_APPROVAL
                    ).count(),
                    "last_24h": window.count(),
                    "failed_24h": window.filter(status=ExecutionStatusChoices.FAILED).count(),
                    "tokens_24h": window.aggregate(total=Sum("total_tokens"))["total"] or 0,
                    "cost_24h": window.aggregate(total=Sum("cost"))["total"] or 0,
                },
                "refreshed_at": timezone.now(),
            }
        )
        return context
