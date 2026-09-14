"""MCP Gateway views: the registry in one place, and what the gateway is doing."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Avg, Count, Q
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.generic import DetailView, ListView, TemplateView, View

from apps.accounts.roles import Perm
from apps.core.constants import HealthStatus, LifecycleStatus, RiskLevel
from apps.core.responses import action_response
from apps.core.views import OrganizationScopedMixin, ScopedDetailMixin
from apps.gateway.models import CircuitState
from apps.mcp_gateway.models import (
    AccessMode,
    CallStatus,
    MCPServer,
    MCPTool,
    MCPToolCall,
    MCPTransport,
    ToolStatus,
)
from apps.mcp_gateway.registry import MCPRegistryService

#: Reviewers work top-down, so what needs a decision comes first.
_TOOL_ORDER = {
    ToolStatus.PENDING_REVIEW: 0,
    ToolStatus.ENABLED: 1,
    ToolStatus.DISABLED: 2,
    ToolStatus.REMOVED: 3,
}


def _can(request, permission: str) -> bool:
    return request.user.has_org_permission(request.organization, permission)


class MCPOverviewView(LoginRequiredMixin, TemplateView):
    """What needs a decision, and what the gateway is doing to live traffic."""

    template_name = "mcp_gateway/overview.html"
    extra_context = {
        "page_title": "MCP Gateway",
        "page_subtitle": "Every MCP server in one registry, reached through one governed endpoint",
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization
        if organization is None:
            return context

        servers = MCPServer.objects.for_organization(organization)
        tools = MCPTool.objects.for_organization(organization)
        all_calls = MCPToolCall.objects.for_organization(organization)
        calls = all_calls.filter(created_at__gte=timezone.now() - timedelta(hours=24))
        by_status = dict(calls.values_list("status").annotate(total=Count("id")).order_by())

        context["stats"] = {
            "servers": servers.count(),
            "serving": sum(
                1
                for server in servers.filter(status=LifecycleStatus.ACTIVE, is_approved=True)
                if server.is_gateway_reachable
            ),
            "tools_enabled": tools.filter(status=ToolStatus.ENABLED).count(),
            "tools_pending": tools.filter(status=ToolStatus.PENDING_REVIEW).count(),
            "calls": sum(by_status.values()),
            "blocked": by_status.get(CallStatus.BLOCKED, 0),
            "failed": by_status.get(CallStatus.FAILED, 0) + by_status.get(CallStatus.UNAVAILABLE, 0),
            "avg_latency": calls.filter(latency_ms__isnull=False).aggregate(
                avg=Avg("latency_ms")
            )["avg"] or 0,
        }

        # What needs someone: these lead the page rather than sit in a table.
        context["open_circuits"] = servers.filter(circuit_state=CircuitState.OPEN)
        context["pending_tools"] = (
            tools.filter(status=ToolStatus.PENDING_REVIEW)
            .select_related("server")
            .order_by("-changed_since_review", "server__name", "name")[:8]
        )
        context["awaiting_approval"] = servers.filter(
            is_approved=False, status=LifecycleStatus.ACTIVE
        )
        context["sync_failures"] = servers.exclude(last_sync_error="")
        context["missing_credentials"] = [
            server
            for server in servers.filter(status=LifecycleStatus.ACTIVE)
            if not server.has_resolvable_credential
        ]

        context["by_server"] = (
            calls.exclude(server=None)
            .values("server__pk", "server__name")
            .annotate(
                total=Count("id"),
                blocked=Count("id", filter=Q(status=CallStatus.BLOCKED)),
                failed=Count("id", filter=Q(status__in=[CallStatus.FAILED, CallStatus.UNAVAILABLE])),
            )
            .order_by("-total")[:6]
        )
        context["recent"] = all_calls.select_related("server", "agent")[:12]
        context["endpoint_url"] = self.request.build_absolute_uri(reverse("api:mcp-endpoint"))
        return context


class MCPServerListView(OrganizationScopedMixin, ListView):
    model = MCPServer
    template_name = "mcp_gateway/server_list.html"
    context_object_name = "servers"
    paginate_by = 25
    ordering = ["name"]
    extra_context = {
        "page_title": "MCP Server Registry",
        "page_subtitle": "Every MCP server the organization's agents may reach",
    }
    search_fields = ["name", "slug", "description", "publisher", "category", "endpoint"]
    # Credential status is resolved from the environment at request time, so
    # it is shown but not offered as a sort key.
    sort_fields = {
        "server": "name",
        "transport": "transport",
        "tools": "enabled_tools",
        "access": "access_mode",
        "health": ("health_status", HealthStatus.values),
        "approved": "is_approved",
        "status": "status",
    }
    filter_fields = {
        "transport": "transport",
        "approved": "is_approved",
        "access": "access_mode",
        "health": "health_status",
        "status": "status",
    }

    def get_base_queryset(self):
        return (
            super()
            .get_base_queryset()
            .select_related("owner", "team")
            .annotate(
                enabled_tools=Count("tools", filter=Q(tools__status=ToolStatus.ENABLED)),
                pending_tools=Count("tools", filter=Q(tools__status=ToolStatus.PENDING_REVIEW)),
            )
            # Meta.ordering does not survive an aggregate, so pages would shuffle.
            .order_by("name")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "transports": MCPTransport.choices,
                "access_modes": AccessMode.choices,
                "health_statuses": HealthStatus.choices,
                "statuses": LifecycleStatus.choices,
                "can_manage": _can(self.request, Perm.MCP_MANAGE),
            }
        )
        return context


class MCPServerDetailView(ScopedDetailMixin, DetailView):
    model = MCPServer
    template_name = "mcp_gateway/server_detail.html"
    context_object_name = "server"
    extra_context = {"page_title": "MCP Server"}

    def get_queryset(self):
        return super().get_queryset().select_related("owner", "team", "approved_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        server = self.object
        tools = sorted(
            server.tools.select_related("reviewed_by"),
            key=lambda tool: (_TOOL_ORDER.get(tool.status, 9), tool.name),
        )
        context.update(
            {
                "tools": tools,
                "tool_counts": Counter(tool.status for tool in tools),
                "grants": server.grants.select_related("agent"),
                "recent_calls": server.calls.select_related("agent", "tool")[:15],
                "can_manage": _can(self.request, Perm.MCP_MANAGE),
                "can_approve": _can(self.request, Perm.MCP_APPROVE),
                "can_view_content": _can(self.request, Perm.PROMPT_VIEW),
            }
        )
        return context


class MCPToolListView(OrganizationScopedMixin, ListView):
    """Every tool from every server, and the decision made about each."""

    model = MCPTool
    template_name = "mcp_gateway/tool_list.html"
    context_object_name = "tools"
    paginate_by = 50
    ordering = ["server__name", "name"]
    extra_context = {
        "page_title": "MCP Tool Catalog",
        "page_subtitle": "Every tool every server offers, and what governance decided about it",
    }
    search_fields = ["name", "title", "description", "server__name"]
    sort_fields = {
        "tool": "name",
        "server": "server__name",
        "risk": ("risk_level", RiskLevel.values),
        "status": "status",
        "seen": "last_seen_at",
    }
    filter_fields = {"status": "status", "server": "server_id", "risk": "risk_level"}

    def get_base_queryset(self):
        return super().get_base_queryset().select_related("server", "reviewed_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization
        counts = dict(
            MCPTool.objects.for_organization(organization)
            .values_list("status")
            .annotate(total=Count("id"))
            .order_by()
        )
        context.update(
            {
                "tool_statuses": [
                    (value, label, counts.get(value, 0)) for value, label in ToolStatus.choices
                ],
                "risk_levels": RiskLevel.choices,
                "servers": MCPServer.objects.for_organization(organization).only("pk", "name"),
                "can_approve": _can(self.request, Perm.MCP_APPROVE),
            }
        )
        return context


class MCPToolCallListView(OrganizationScopedMixin, ListView):
    model = MCPToolCall
    template_name = "mcp_gateway/call_list.html"
    context_object_name = "calls"
    paginate_by = 50
    ordering = ["-created_at"]
    extra_context = {
        "page_title": "MCP Tool Calls",
        "page_subtitle": "Every call the gateway brokered, and every one it refused",
    }
    search_fields = ["tool_name", "reason"]
    sort_fields = {
        "when": "created_at",
        "tool": "tool_name",
        "caller": ["agent__name", "service_account__name"],
        "status": "status",
        "latency": "latency_ms",
    }
    filter_fields = {"status": "status", "server": "server_id", "agent": "agent_id"}

    def get_base_queryset(self):
        return super().get_base_queryset().select_related(
            "server", "agent", "service_account", "execution"
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "call_statuses": CallStatus.choices,
                "servers": MCPServer.objects.for_organization(self.request.organization).only(
                    "pk", "name"
                ),
                "can_view_content": _can(self.request, Perm.PROMPT_VIEW),
            }
        )
        return context


# -- Actions ------------------------------------------------------------------


class _ServerAction(LoginRequiredMixin, View):
    permission = Perm.MCP_MANAGE
    denied_message = "Your role does not allow managing MCP servers."

    def post(self, request, pk, *args, **kwargs):
        redirect_to = request.POST.get("next") or reverse("mcp_gateway:server_detail", args=[pk])
        if not _can(request, self.permission):
            return action_response(
                request, message=self.denied_message, tone="danger",
                redirect_to=redirect_to, status=403,
            )
        server = get_object_or_404(MCPServer.objects.for_organization(request.organization), pk=pk)
        return self.act(request, server, redirect_to)

    def act(self, request, server, redirect_to):  # pragma: no cover - abstract
        raise NotImplementedError


class SyncServerView(_ServerAction):
    """Ask the server what it offers now. New or changed tools wait for review."""

    def act(self, request, server, redirect_to):
        result = MCPRegistryService.sync(server, actor=request.user)
        return action_response(
            request, message=result.summary, tone="success" if result.ok else "danger",
            redirect_to=redirect_to, status=200 if result.ok else 502,
        )


class HealthCheckServerView(_ServerAction):
    def act(self, request, server, redirect_to):
        check = MCPRegistryService.health_check(server)
        if check is None:
            message, tone = f"{server.get_transport_display()} servers cannot be probed by the gateway.", "warning"
        elif check.healthy:
            message, tone = f"{server.name} answered in {check.latency_ms} ms.", "success"
        else:
            message, tone = f"{server.name} did not answer: {check.message}", "danger"
        return action_response(request, message=message, tone=tone, redirect_to=redirect_to)


class ServerApprovalView(_ServerAction):
    permission = Perm.MCP_APPROVE
    denied_message = "Your role does not allow approving MCP servers."

    def act(self, request, server, redirect_to):
        approve = request.POST.get("decision") == "approve"
        MCPRegistryService.set_approval(server, approved=approve, actor=request.user)
        message = (
            f"Approved {server.name}. Its enabled tools are now available to permitted agents."
            if approve
            else f"Revoked approval for {server.name}. No agent can reach it through the gateway."
        )
        return action_response(
            request, message=message, tone="success" if approve else "warning",
            redirect_to=redirect_to,
        )


class ToolReviewView(LoginRequiredMixin, View):
    def post(self, request, pk, *args, **kwargs):
        redirect_to = request.POST.get("next") or reverse("mcp_gateway:tools")
        if not _can(request, Perm.MCP_APPROVE):
            return action_response(
                request, message="Your role does not allow reviewing MCP tools.",
                tone="danger", redirect_to=redirect_to, status=403,
            )

        tool = get_object_or_404(
            MCPTool.objects.for_organization(request.organization).select_related("server"), pk=pk
        )
        decision = request.POST.get("decision")
        if decision not in {"enable", "disable"}:
            return action_response(
                request, message="Choose to enable or disable the tool.", tone="danger",
                redirect_to=redirect_to, status=400,
            )

        try:
            MCPRegistryService.review(
                tool, enable=decision == "enable", actor=request.user,
                note=request.POST.get("note", ""),
            )
        except ValueError as exc:
            return action_response(
                request, message=str(exc), tone="warning", redirect_to=redirect_to, status=409
            )

        verb = "Enabled" if decision == "enable" else "Disabled"
        return action_response(
            request, message=f"{verb} {tool.qualified_name}.", redirect_to=redirect_to
        )


class ToolLimitsView(LoginRequiredMixin, View):
    """Agent chain link 06: which arguments carry a call's value and counterparty."""

    def post(self, request, pk, *args, **kwargs):
        redirect_to = request.POST.get("next") or reverse("mcp_gateway:tools")
        if not _can(request, Perm.MCP_APPROVE):
            return action_response(
                request, message="Your role does not allow reviewing MCP tools.",
                tone="danger", redirect_to=redirect_to, status=403,
            )
        tool = get_object_or_404(
            MCPTool.objects.for_organization(request.organization).select_related("server"), pk=pk
        )
        try:
            MCPRegistryService.set_limit_arguments(
                tool,
                value_argument=request.POST.get("value_argument", "").strip(),
                counterparty_argument=request.POST.get("counterparty_argument", "").strip(),
                actor=request.user,
            )
        except ValueError as exc:
            return action_response(
                request, message=str(exc), tone="warning", redirect_to=redirect_to, status=409
            )
        return action_response(
            request, message=f"Action-limit arguments saved for {tool.qualified_name}.",
            redirect_to=redirect_to,
        )
