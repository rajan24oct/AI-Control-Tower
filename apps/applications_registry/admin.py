from django.contrib import admin

from apps.audit.admin import AuditedModelAdmin

from apps.applications_registry.models import AIApplication


@admin.register(AIApplication)
class AIApplicationAdmin(AuditedModelAdmin):
    list_display = (
        "name",
        "organization",
        "team",
        "environment",
        "criticality",
        "risk_level",
        "business_owner",
        "status",
    )
    list_filter = ("organization", "environment", "criticality", "risk_level", "status", "team")
    search_fields = ("name", "slug", "description", "department")
    autocomplete_fields = ("business_owner", "technical_owner", "team")
    filter_horizontal = ("ai_models", "agents", "providers")
    readonly_fields = ("id", "created_at", "updated_at")

    fieldsets = (
        (None, {"fields": ("id", "organization", "name", "slug", "description")}),
        (
            "Ownership",
            {
                "fields": (
                    "business_purpose",
                    "business_owner",
                    "technical_owner",
                    "team",
                    "department",
                )
            },
        ),
        (
            "Classification",
            {
                "fields": (
                    "status",
                    "environment",
                    "criticality",
                    "data_classification",
                    "risk_level",
                )
            },
        ),
        ("Lifecycle", {"fields": ("production_date", "review_date")}),
        ("AI Landscape", {"fields": ("agents", "ai_models", "providers", "tools")}),
        ("Audit", {"fields": ("created_at", "updated_at")}),
    )
