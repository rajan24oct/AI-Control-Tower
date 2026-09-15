"""MCP servers and access grants, managed in the web UI."""

from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError

from apps.core.forms import ChoiceListField, JSONObjectField, OrganizationModelForm
from apps.inventory.forms import clean_credential_ref
from apps.mcp_gateway.models import MCPAccessGrant, MCPServer, ToolStatus


class MCPServerForm(OrganizationModelForm):
    options = JSONObjectField(
        help_text='Static, non-secret headers or a timeout, e.g. {"headers": {"X-Tenant": "acme"}, "timeout": 20}.',
    )

    class Meta:
        model = MCPServer
        fields = [
            "name", "slug", "description", "publisher", "category", "documentation_url",
            "transport", "endpoint", "command", "auth_method", "credential_ref", "auth_header", "options",
            "owner", "team", "data_classification", "risk_level", "access_mode", "status",
        ]
        labels = {
            "slug": "Tool prefix", "documentation_url": "Documentation URL", "auth_method": "Authentication",
            "credential_ref": "Credential reference", "auth_header": "Header name", "access_mode": "Access",
            "risk_level": "Risk",
        }
        help_texts = {
            "slug": "Every tool it contributes is named <prefix>__<tool>. Blank derives it from the name; it cannot change later.",
            "credential_ref": "The environment variable holding the token, e.g. GITHUB_MCP_TOKEN.",
            "access_mode": "Restricted by default: only agents with a grant can reach it.",
        }

    unique_in_organization = ("name",)
    sections = (
        ("Server", "", ["name", "slug", "description", "publisher", "category", "documentation_url"]),
        (
            "Connection",
            "The gateway reaches Streamable HTTP servers. Others are registered for inventory and governance only.",
            ["transport", "endpoint", "command", "auth_method", "credential_ref", "auth_header", "options"],
        ),
        (
            "Governance",
            "Approval, tool review and grants happen on the server's page once it is registered.",
            ["owner", "team", "data_classification", "risk_level", "access_mode", "status"],
        ),
    )
    compact_fields = (
        "slug", "publisher", "category", "documentation_url", "transport", "auth_method", "credential_ref",
        "auth_header", "owner", "team", "data_classification", "risk_level", "access_mode", "status",
    )
    show_when = {
        "endpoint": "transport:streamable_http,sse,mock",
        "command": "transport:stdio",
        "credential_ref": "auth_method:bearer,header",
        "auth_header": "auth_method:header",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.creating:
            # Agents and allow-lists name its tools by this prefix.
            self.fields["slug"].disabled = True
            self.fields["slug"].help_text = "Every tool it contributes is named with this prefix. It cannot change."

    def clean_credential_ref(self):
        return clean_credential_ref(self.cleaned_data.get("credential_ref"))

    def clean(self):
        cleaned = super().clean()
        if self.creating:
            slug = MCPServer.clean_slug(cleaned.get("slug") or cleaned.get("name") or "")
            cleaned["slug"] = self.instance.slug = slug
            if MCPServer.objects.filter(organization=self.organization, slug=slug).exists():
                self.add_error("slug" if self.data.get("slug") else "name",
                               f"Another server already contributes tools as “{slug}”.")
        return cleaned


class AccessGrantForm(OrganizationModelForm):
    allowed_tools = ChoiceListField(
        label="Tools", help_text="Leave every box empty for all of its enabled tools, now and later.",
    )
    expires_at = forms.DateTimeField(
        label="Expires", required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"],
        help_text="Optional. An expired grant stops working on its own.",
    )

    class Meta:
        model = MCPAccessGrant
        fields = ["agent", "allowed_tools", "expires_at", "note"]
        help_texts = {"note": "Why this agent needs it, for whoever reviews the grant later."}

    sections = (("Grant", "", ["agent", "allowed_tools", "expires_at", "note"]),)
    compact_fields = ("agent", "expires_at")

    def __init__(self, *args, server=None, **kwargs):
        self.server = server
        super().__init__(*args, **kwargs)
        tools = server.tools.exclude(status=ToolStatus.REMOVED).order_by("name")
        self.fields["allowed_tools"].choices = [
            (tool.name, f"{tool.name} · {tool.get_status_display().lower()}") for tool in tools
        ]
        self.fields["allowed_tools"].include(self.instance.allowed_tools)
        agents = self.fields["agent"].queryset.order_by("name")
        if self.creating:
            agents = agents.exclude(mcp_grants__server=server)
        else:
            self.fields["agent"].disabled = True
        self.fields["agent"].queryset = agents

    def clean_agent(self):
        agent = self.cleaned_data.get("agent")
        if self.creating and agent and MCPAccessGrant.objects.filter(server=self.server, agent=agent).exists():
            raise ValidationError(f"{agent} already has a grant on this server; edit that one.")
        return agent
