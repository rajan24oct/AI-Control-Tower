"""Registering MCP servers and granting agents access, in the web UI.

Registering a server (`mcp.manage`) points the platform at a new system.
Letting an agent reach it is governance's decision (`mcp.approve`), like
approving the server and reviewing its tools.
"""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View

from apps.accounts.roles import Perm
from apps.audit.models import AuditAction
from apps.audit.services import AuditService, snapshot
from apps.core.manage import ManagedFormView, can
from apps.core.responses import action_response
from apps.mcp_gateway.forms import AccessGrantForm, MCPServerForm
from apps.mcp_gateway.models import MCPAccessGrant, MCPServer


class MCPServerFormView(ManagedFormView):
    model = MCPServer
    form_class = MCPServerForm
    permission = Perm.MCP_MANAGE
    denied_message = "Registering and editing MCP servers needs mcp.manage."
    noun = "MCP server"
    create_heading = "Register an MCP server"
    create_lead = (
        "One governed endpoint in front of it: agents reach its tools only after approval, tool review and, "
        "on a restricted server, a grant."
    )
    create_submit_label = "Register server"
    list_url_name = "mcp_gateway:servers"
    list_label = "Server Registry"
    detail_url_name = "mcp_gateway:server_detail"
    guide_template = "manage/guides/_mcp_server.html"

    def success_message(self, obj, creating, changed):
        if creating:
            return f"Registered {obj.name}. Next, on its page: health check, approval, then sync its tools."
        return super().success_message(obj, creating, changed)


class AccessGrantFormView(ManagedFormView):
    model = MCPAccessGrant
    form_class = AccessGrantForm
    permission = Perm.MCP_APPROVE
    denied_message = "Granting agents access to MCP servers is a governance decision (mcp.approve)."
    noun = "access grant"
    create_submit_label = "Grant access"
    guide_template = "manage/guides/_mcp_grant.html"

    def get_object(self):
        grant = super().get_object()
        if grant is not None:
            self.server = grant.server
        else:
            self.server = get_object_or_404(
                MCPServer.objects.for_organization(self.request.organization), pk=self.kwargs["server_pk"]
            )
        return grant

    def new_instance(self):
        return MCPAccessGrant(server=self.server, organization=self.server.organization)

    def get_form_kwargs(self):
        return {"server": self.server}

    @property
    def create_heading(self):
        return f"Grant an agent access to {self.server.name}"

    @property
    def list_label(self):
        return self.server.name

    def list_url(self):
        return reverse("mcp_gateway:server_detail", args=[self.server.pk])

    def detail_url(self, obj):
        return self.list_url()

    def success_message(self, obj, creating, changed):
        if creating:
            scope = ", ".join(obj.allowed_tools) if obj.allowed_tools else "all of its enabled tools"
            return f"{obj.agent} may now reach {self.server.name}: {scope}."
        return super().success_message(obj, creating, changed)


class AccessGrantRevokeView(LoginRequiredMixin, View):
    def post(self, request, pk, *args, **kwargs):
        grant = get_object_or_404(
            MCPAccessGrant.objects.for_organization(request.organization).select_related("server", "agent"), pk=pk
        )
        redirect_to = reverse("mcp_gateway:server_detail", args=[grant.server_id])
        if not can(request, Perm.MCP_APPROVE):
            return action_response(
                request, message="Revoking access is a governance decision (mcp.approve).", tone="danger",
                redirect_to=redirect_to, status=403,
            )
        before, label = snapshot(grant), str(grant)
        agent, server = grant.agent, grant.server
        grant.delete()
        AuditService.log(
            action=AuditAction.DELETE, resource_type=MCPAccessGrant._meta.label, resource_id=pk,
            resource_label=label, organization=request.organization, actor=request.user, old_value=before,
        )
        return action_response(
            request, message=f"Revoked {agent}'s access to {server}.", tone="warning", redirect_to=redirect_to
        )
