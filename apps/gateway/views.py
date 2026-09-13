"""AI Gateway views."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Avg, Count, Sum
from django.utils import timezone
from django.views.generic import ListView, TemplateView

from apps.core.views import OrganizationScopedMixin
from apps.executions.models import Execution, ExecutionStatusChoices
from apps.gateway.models import CircuitState, GatewayMessage, ProviderCircuit
from apps.providers.models import AIProvider
from apps.routing.services import RoutingStats


class GatewayOverviewView(LoginRequiredMixin, TemplateView):
    """What the gateway is doing to live traffic right now."""

    template_name = "gateway/overview.html"
    extra_context = {
        "page_title": "AI Gateway",
        "page_subtitle": "Agent traffic served in-path, with policy and budget enforced",
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization
        if organization is None:
            return context

        since = timezone.now() - timedelta(hours=24)
        served = Execution.objects.for_organization(organization).filter(
            source_adapter="gateway", started_at__gte=since
        )

        context["stats"] = {
            "requests": served.count(),
            "failed": served.filter(status=ExecutionStatusChoices.FAILED).count(),
            "tokens": served.aggregate(total=Sum("total_tokens"))["total"] or 0,
            "cost": served.aggregate(total=Sum("cost"))["total"] or 0,
            "median_latency": served.aggregate(avg=Avg("latency_ms"))["avg"] or 0,
            "fell_back": served.filter(metadata__used_fallback=True).count(),
        }
        context["routing"] = RoutingStats(organization).build()

        # Breakers are the thing an operator needs at a glance: an open one
        # means live traffic is being steered away from a provider right now.
        circuits = (
            ProviderCircuit.objects.filter(provider__organization=organization)
            .select_related("provider")
            .order_by("-state", "provider__name")
        )
        context["circuits"] = circuits
        context["open_circuits"] = [c for c in circuits if c.state == CircuitState.OPEN]

        context["providers_without_credentials"] = [
            provider
            for provider in AIProvider.objects.for_organization(organization).filter(
                status="active"
            )
            if not provider.has_resolvable_credential
        ]

        context["recent"] = (
            served.select_related("agent", "model", "model__provider")
            .order_by("-started_at")[:15]
        )
        context["messages_summary"] = (
            GatewayMessage.objects.for_organization(organization)
            .filter(created_at__gte=since)
            .values("status")
            .annotate(count=Count("id"))
            .order_by()
        )
        return context


class GatewayMessageListView(OrganizationScopedMixin, ListView):
    model = GatewayMessage
    template_name = "gateway/message_list.html"
    context_object_name = "messages_page"
    paginate_by = 50
    ordering = ["-created_at"]
    extra_context = {
        "page_title": "Agent Messages",
        "page_subtitle": "Agent-to-agent traffic brokered by the gateway",
    }
    search_fields = ["subject", "body", "correlation_id"]
    sort_fields = {
        "when": "created_at",
        "from": "sender__name",
        "to": "recipient__name",
        "subject": "subject",
        "status": "status",
    }
    filter_fields = {"status": "status", "sender": "sender_id", "recipient": "recipient_id"}

    def get_queryset(self):
        return super().get_queryset().select_related("sender", "recipient")
