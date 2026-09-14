from django.contrib import admin

from apps.audit.admin import AuditedModelAdmin

from apps.agents.models import AIAgent


@admin.register(AIAgent)
class AIAgentAdmin(AuditedModelAdmin):
    list_display = (
        "name",
        "organization",
        "framework",
        "primary_model",
        "environment",
        "risk_level",
        "risk_tier",
        "autonomy_level",
        "requires_human_approval",
        "suspended",
        "status",
    )
    list_filter = (
        "organization",
        "framework",
        "environment",
        "risk_level",
        "risk_tier",
        "autonomy_level",
        "requires_human_approval",
        "status",
    )
    search_fields = ("name", "agent_id", "description")
    autocomplete_fields = ("owner", "team", "provider", "primary_model", "fallback_model")
    # The tier comes from the S2 classification record and the kill-switch
    # state from the suspend and resume actions; both are audited there, so
    # neither is editable here.
    readonly_fields = (
        "id",
        "risk_tier",
        "suspended_at",
        "suspended_by",
        "suspended_by_label",
        "suspension_reason",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        (None, {"fields": ("id", "organization", "name", "agent_id", "description")}),
        ("Ownership", {"fields": ("business_purpose", "owner", "team")}),
        (
            "Execution",
            {
                "fields": (
                    "framework",
                    "adapter_key",
                    "remote_endpoint",
                    "provider",
                    "primary_model",
                    "fallback_model",
                    "options",
                )
            },
        ),
        (
            "Governance",
            {
                "fields": (
                    "status",
                    "environment",
                    "risk_level",
                    "risk_tier",
                    "autonomy_level",
                    "requires_human_approval",
                    "data_classification",
                )
            },
        ),
        (
            "Boundaries",
            {
                "fields": (
                    "allowed_tools",
                    "restricted_tools",
                    "knowledge_sources",
                    "system_prompt_reference",
                )
            },
        ),
        (
            "Agent Card (IRGF S3, required at Gate G3)",
            {
                "fields": (
                    "out_of_scope",
                    "action_limits",
                    "escalation_thresholds",
                    "kill_switch_conditions",
                    "kill_switch_triggers",
                )
            },
        ),
        (
            "Kill-switch",
            {
                "fields": (
                    "suspended_at",
                    "suspended_by",
                    "suspended_by_label",
                    "suspension_reason",
                )
            },
        ),
        ("Audit", {"fields": ("created_at", "updated_at")}),
    )

    @admin.display(boolean=True, description="Suspended")
    def suspended(self, obj):
        return obj.is_suspended
