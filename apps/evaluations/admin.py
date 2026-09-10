from django.contrib import admin

from apps.evaluations.models import (
    EvaluationCase,
    EvaluationResult,
    EvaluationRun,
    EvaluationSuite,
)


class EvaluationCaseInline(admin.TabularInline):
    model = EvaluationCase
    extra = 1
    fields = ("order", "name", "category", "scorer", "configuration", "weight", "prompt")


@admin.register(EvaluationSuite)
class EvaluationSuiteAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "target_kind", "pass_threshold", "case_count", "status")
    list_filter = ("organization", "target_kind", "status")
    search_fields = ("name", "description")
    inlines = [EvaluationCaseInline]


class EvaluationResultInline(admin.TabularInline):
    model = EvaluationResult
    extra = 0
    fields = ("case_name", "category", "passed", "detail", "latency_ms", "cost")
    readonly_fields = fields
    can_delete = False


@admin.register(EvaluationRun)
class EvaluationRunAdmin(admin.ModelAdmin):
    list_display = ("created_at", "suite", "target_label", "status", "score", "passed")
    list_filter = ("organization", "status", "passed")
    # A run is evidence. Editing one would make it worthless as evidence.
    readonly_fields = [f.name for f in EvaluationRun._meta.fields]
    inlines = [EvaluationResultInline]

    def has_add_permission(self, request):
        return False
