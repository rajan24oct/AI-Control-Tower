from django.contrib import admin

from apps.audit.admin import AuditedModelAdmin
from apps.studio.models import SandboxRun, SandboxSession, SolutionBlueprint, SolutionRequest


@admin.register(SolutionRequest)
class SolutionRequestAdmin(AuditedModelAdmin):
    list_display = ("reference", "title", "organization", "status", "data_classification", "created_at")
    list_filter = ("organization", "status")
    search_fields = ("reference", "title", "problem")


@admin.register(SolutionBlueprint)
class SolutionBlueprintAdmin(admin.ModelAdmin):
    list_display = ("request", "version", "generated_by", "archetype", "created_at")
    list_filter = ("generated_by", "archetype")
    readonly_fields = ("request", "version", "generated_by", "archetype", "content", "input_tokens", "output_tokens")


@admin.register(SandboxSession)
class SandboxSessionAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "owner", "updated_at")
    list_filter = ("organization",)


@admin.register(SandboxRun)
class SandboxRunAdmin(admin.ModelAdmin):
    list_display = ("created_at", "session", "requested_model", "status", "cost", "latency_ms")
    list_filter = ("status",)
    readonly_fields = [f.name for f in SandboxRun._meta.fields]
