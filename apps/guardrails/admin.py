from django.contrib import admin

from apps.audit.admin import AuditedModelAdmin
from apps.guardrails.models import GuardrailEvent, GuardrailPolicy


@admin.register(GuardrailPolicy)
class GuardrailPolicyAdmin(AuditedModelAdmin):
    list_display = ("name", "organization", "detector", "stage", "action", "status")
    list_filter = ("organization", "detector", "action", "status")
    search_fields = ("name", "description")


@admin.register(GuardrailEvent)
class GuardrailEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "organization", "policy_name", "detector", "action", "source", "agent")
    list_filter = ("organization", "detector", "action", "source")
    readonly_fields = [f.name for f in GuardrailEvent._meta.fields]
