from django.contrib import admin, messages

from apps.audit.admin import AuditedModelAdmin

from apps.providers.models import AIProvider
from apps.providers.services import ProviderCapabilityService, ProviderHealthService


@admin.register(AIProvider)
class AIProviderAdmin(AuditedModelAdmin):
    list_display = (
        "name",
        "organization",
        "provider_type",
        "adapter_key",
        "region",
        "health_status",
        "is_approved",
        "status",
    )
    list_filter = ("organization", "provider_type", "health_status", "is_approved", "status")
    search_fields = ("name", "slug", "adapter_key", "endpoint")
    autocomplete_fields = ("owner",)
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "health_status",
        "last_health_check",
        "last_health_message",
        "last_health_latency_ms",
        "supported_capabilities",
    )
    actions = ("action_health_check", "action_refresh_capabilities")

    fieldsets = (
        (None, {"fields": ("id", "organization", "name", "slug", "description", "owner")}),
        (
            "Connection",
            {
                "fields": (
                    "provider_type",
                    "adapter_key",
                    "endpoint",
                    "region",
                    "deployment",
                    "options",
                )
            },
        ),
        (
            "Credentials",
            {
                "fields": ("authentication_method", "credential_ref"),
                "description": (
                    "<strong>credential_ref names an environment variable or secret-manager "
                    "key. Never paste an API key here.</strong>"
                ),
            },
        ),
        ("Governance", {"fields": ("status", "is_approved", "supported_capabilities")}),
        (
            "Health",
            {
                "fields": (
                    "health_status",
                    "last_health_check",
                    "last_health_latency_ms",
                    "last_health_message",
                )
            },
        ),
        ("Audit", {"fields": ("created_at", "updated_at")}),
    )

    @admin.action(description="Run health check")
    def action_health_check(self, request, queryset):
        for provider in queryset:
            ProviderHealthService.check(provider)
        self.message_user(request, f"Checked {queryset.count()} provider(s).", messages.SUCCESS)

    @admin.action(description="Refresh capabilities from adapter")
    def action_refresh_capabilities(self, request, queryset):
        for provider in queryset:
            ProviderCapabilityService.refresh(provider)
        self.message_user(request, f"Refreshed {queryset.count()} provider(s).", messages.SUCCESS)
