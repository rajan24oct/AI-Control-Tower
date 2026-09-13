from django.contrib import admin, messages

from apps.audit.admin import AuditedModelAdmin
from apps.mcp_gateway.models import MCPAccessGrant, MCPServer, MCPTool, MCPToolCall
from apps.mcp_gateway.registry import MCPRegistryService


@admin.register(MCPServer)
class MCPServerAdmin(AuditedModelAdmin):
    list_display = (
        "name", "transport", "endpoint", "is_approved", "access_mode",
        "health_status", "circuit_state", "status",
    )
    list_filter = ("organization", "transport", "is_approved", "access_mode", "health_status", "status")
    search_fields = ("name", "slug", "description", "publisher", "endpoint")
    # Approval goes through the actions so it is stamped and audited as an
    # approval; discovery, health and the breaker are written by the platform.
    readonly_fields = (
        "id", "is_approved", "approved_at", "approved_by",
        "server_info", "protocol_version", "advertised_capabilities",
        "last_synced_at", "last_sync_error",
        "health_status", "last_health_check", "last_health_latency_ms", "last_health_message",
        "circuit_state", "consecutive_failures", "circuit_opened_at", "last_failure_at",
        "last_error", "circuit_trips", "created_at", "updated_at",
    )
    fieldsets = (
        (None, {"fields": (
            "id", "organization", "name", "slug", "description", "publisher", "category",
            "documentation_url",
        )}),
        ("Connection", {"fields": (
            "transport", "endpoint", "command", "auth_method", "credential_ref", "auth_header",
            "options",
        )}),
        ("Governance", {"fields": (
            "status", "is_approved", "approved_at", "approved_by", "access_mode",
            "data_classification", "risk_level", "owner", "team",
        )}),
        ("Discovery", {"fields": (
            "server_info", "protocol_version", "advertised_capabilities",
            "last_synced_at", "last_sync_error",
        )}),
        ("Health and breaker", {"classes": ("collapse",), "fields": (
            "health_status", "last_health_check", "last_health_latency_ms", "last_health_message",
            "circuit_state", "consecutive_failures", "circuit_opened_at", "last_failure_at",
            "last_error", "circuit_trips",
        )}),
        ("Audit", {"fields": ("created_at", "updated_at")}),
    )
    actions = ("action_sync", "action_approve", "action_revoke")

    @admin.action(description="Sync tools from the server")
    def action_sync(self, request, queryset):
        for server in queryset:
            result = MCPRegistryService.sync(server, actor=request.user)
            self.message_user(request, result.summary, messages.SUCCESS if result.ok else messages.ERROR)

    def _set_approval(self, request, queryset, approved: bool):
        for server in queryset:
            MCPRegistryService.set_approval(server, approved=approved, actor=request.user)
        verb = "Approved" if approved else "Revoked approval for"
        self.message_user(request, f"{verb} {queryset.count()} server(s).", messages.SUCCESS)

    @admin.action(description="Approve for agent use")
    def action_approve(self, request, queryset):
        self._set_approval(request, queryset, True)

    @admin.action(description="Revoke approval")
    def action_revoke(self, request, queryset):
        self._set_approval(request, queryset, False)


@admin.register(MCPTool)
class MCPToolAdmin(AuditedModelAdmin):
    list_display = ("name", "server", "status", "risk_level", "changed_since_review", "last_seen_at")
    list_filter = ("organization", "status", "risk_level", "changed_since_review", "server")
    search_fields = ("name", "title", "description", "server__name")
    fields = (
        "organization", "server", "name", "title", "description", "input_schema",
        "output_schema", "annotations", "definition_hash", "status", "risk_level",
        "changed_since_review", "first_seen_at", "last_seen_at", "reviewed_at", "reviewed_by",
        "review_note",
    )
    # Only the reviewer's judgement is editable here; the definition is what
    # the server said, and the status moves through the review actions.
    readonly_fields = tuple(f for f in fields if f not in {"risk_level", "review_note"})
    actions = ("action_enable", "action_disable")

    def has_add_permission(self, request):
        # Tools come from discovery. One typed in by hand would describe a
        # tool the server may not actually have.
        return False

    def _review(self, request, queryset, enable: bool):
        reviewed = skipped = 0
        for tool in queryset.select_related("server"):
            try:
                MCPRegistryService.review(tool, enable=enable, actor=request.user)
                reviewed += 1
            except ValueError:
                skipped += 1
        verb = "Enabled" if enable else "Disabled"
        message = f"{verb} {reviewed} tool(s)."
        if skipped:
            message += f" Skipped {skipped} no longer offered by their server."
        self.message_user(request, message, messages.SUCCESS)

    @admin.action(description="Enable (approve for agents)")
    def action_enable(self, request, queryset):
        self._review(request, queryset, True)

    @admin.action(description="Disable")
    def action_disable(self, request, queryset):
        self._review(request, queryset, False)


@admin.register(MCPAccessGrant)
class MCPAccessGrantAdmin(AuditedModelAdmin):
    list_display = ("agent", "server", "tool_scope", "expires_at")
    list_filter = ("server",)
    search_fields = ("agent__name", "agent__agent_id", "server__name")
    # The organization follows the server; it is never chosen separately.
    fields = ("server", "agent", "allowed_tools", "expires_at", "note")

    @admin.display(description="Tools")
    def tool_scope(self, obj):
        return ", ".join(obj.allowed_tools) if obj.allowed_tools else "All enabled tools"


@admin.register(MCPToolCall)
class MCPToolCallAdmin(admin.ModelAdmin):
    list_display = ("created_at", "tool_name", "agent", "status", "latency_ms", "channel")
    list_filter = ("organization", "status", "channel", "server")
    search_fields = ("tool_name", "reason")
    # The call log is a record of what agents did; editing it would make it
    # less trustworthy than not keeping one.
    readonly_fields = [f.name for f in MCPToolCall._meta.fields]

    def has_add_permission(self, request):
        return False
