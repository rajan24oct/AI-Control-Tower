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
        "autonomy_level",
        "requires_human_approval",
        "status",
    )
    list_filter = (
        "organization",
        "framework",
        "environment",
        "risk_level",
        "autonomy_level",
        "requires_human_approval",
        "status",
    )
    search_fields = ("name", "agent_id", "description")
    autocomplete_fields = ("owner", "team", "provider", "primary_model", "fallback_model")
    readonly_fields = ("id", "created_at", "updated_at")

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
        ("Audit", {"fields": ("created_at", "updated_at")}),
    )
