from django.contrib import admin, messages

from apps.telemetry.models import RawTelemetryPayload
from apps.telemetry.services import TelemetryIngestionError, TelemetryService


@admin.register(RawTelemetryPayload)
class RawTelemetryPayloadAdmin(admin.ModelAdmin):
    list_display = (
        "received_at",
        "organization",
        "adapter_key",
        "service_account",
        "status",
        "event_count",
        "execution",
    )
    list_filter = ("status", "adapter_key", "organization")
    search_fields = ("endpoint", "processing_error", "execution__external_id")
    date_hierarchy = "received_at"
    readonly_fields = [f.name for f in RawTelemetryPayload._meta.fields]
    actions = ("action_replay",)

    def has_add_permission(self, request):
        return False

    @admin.action(description="Replay through current normalization")
    def action_replay(self, request, queryset):
        succeeded = failed = 0
        for raw in queryset:
            try:
                TelemetryService.replay(raw)
                succeeded += 1
            except TelemetryIngestionError:
                failed += 1

        self.message_user(
            request,
            f"Replayed {succeeded} payload(s); {failed} failed.",
            messages.WARNING if failed else messages.SUCCESS,
        )
