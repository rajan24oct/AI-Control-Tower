from django.contrib import admin

from apps.audit.admin import AuditedModelAdmin
from apps.intake.models import UseCase


@admin.register(UseCase)
class UseCaseAdmin(AuditedModelAdmin):
    list_display = ("reference", "title", "organization", "provisional_tier", "status", "submitted_at", "decided_at")
    list_filter = ("organization", "status", "provisional_tier")
    search_fields = ("reference", "title", "need")
    readonly_fields = ("id", "reference", "provisional_tier", "floor_applied", "outcome", "decided_at", "decided_by",
                       "decided_by_rule", "decision_note", "conditions", "application", "registered_at",
                       "created_at", "updated_at")
    autocomplete_fields = ("sponsor", "team")
