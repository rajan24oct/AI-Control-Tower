from django.contrib import admin

from apps.executions.models import Execution, ExecutionEvent, ExecutionTrace


class ExecutionEventInline(admin.TabularInline):
    model = ExecutionEvent
    extra = 0
    can_delete = False
    fields = ("sequence", "event_type", "timestamp", "duration_ms", "input_tokens", "output_tokens", "cost")
    readonly_fields = fields
    ordering = ("sequence",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Execution)
class ExecutionAdmin(admin.ModelAdmin):
    list_display = (
        "external_id",
        "organization",
        "agent",
        "application",
        "model",
        "status",
        "started_at",
        "latency_ms",
        "total_tokens",
        "cost",
        "approval_status",
    )
    list_filter = (
        "organization",
        "status",
        "environment",
        "approval_status",
        "cost_is_derived",
        "framework",
    )
    search_fields = ("external_id", "trace__trace_id", "error_message")
    date_hierarchy = "started_at"
    autocomplete_fields = ("agent", "application", "provider", "model")
    readonly_fields = ("id", "total_tokens", "created_at", "updated_at")
    inlines = [ExecutionEventInline]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("agent", "application", "model")


@admin.register(ExecutionTrace)
class ExecutionTraceAdmin(admin.ModelAdmin):
    list_display = ("trace_id", "organization", "started_at", "ended_at", "execution_count")
    list_filter = ("organization",)
    search_fields = ("trace_id",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(ExecutionEvent)
class ExecutionEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "execution", "sequence", "timestamp", "duration_ms", "cost")
    list_filter = ("event_type", "organization")
    search_fields = ("execution__external_id", "trace_id")
    date_hierarchy = "timestamp"
    readonly_fields = [f.name for f in ExecutionEvent._meta.fields]

    def has_add_permission(self, request):
        return False
