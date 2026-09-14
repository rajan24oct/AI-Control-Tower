from django.contrib import admin

from apps.controlplane.models import DriftRecord, Verification


class _ViewOnly(admin.ModelAdmin):
    """Drift is resolved in the Control Tower, where the resolution rights are
    enforced and audited; verifications are evidence. Neither is edited here."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DriftRecord)
class DriftRecordAdmin(_ViewOnly):
    list_display = ("title", "agent", "category", "tier", "is_incident", "status", "occurrences", "last_seen_at")
    list_filter = ("organization", "status", "category", "is_incident", "tier")
    search_fields = ("title", "agent__name", "application__name", "signal")


@admin.register(Verification)
class VerificationAdmin(_ViewOnly):
    list_display = ("verified_at", "agent", "outcome", "tier", "latency_ms", "ceiling_seconds", "late")
    list_filter = ("organization", "outcome", "late", "tier")
    search_fields = ("agent__name",)
