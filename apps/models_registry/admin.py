from django.contrib import admin, messages

from apps.audit.admin import AuditedModelAdmin

from apps.audit.models import AuditAction
from apps.audit.services import AuditService, snapshot
from apps.models_registry.models import AIModel


@admin.register(AIModel)
class AIModelAdmin(AuditedModelAdmin):
    list_display = (
        "model_name",
        "provider",
        "model_type",
        "version",
        "context_window",
        "input_cost_per_1k",
        "output_cost_per_1k",
        "approved_for_use",
        "risk_classification",
        "status",
    )
    list_filter = (
        "organization",
        "provider",
        "model_type",
        "approved_for_use",
        "risk_classification",
        "status",
    )
    search_fields = ("model_name", "model_id", "model_family")
    autocomplete_fields = ("provider",)
    readonly_fields = ("id", "created_at", "updated_at")
    actions = ("action_approve", "action_revoke_approval")

    fieldsets = (
        (None, {"fields": ("id", "organization", "provider", "model_name", "model_id")}),
        (
            "Classification",
            {"fields": ("model_family", "model_type", "version", "capabilities")},
        ),
        ("Limits", {"fields": ("context_window", "max_output_tokens")}),
        (
            "Pricing (per 1,000 tokens)",
            {"fields": ("input_cost_per_1k", "output_cost_per_1k", "currency")},
        ),
        ("Deployment", {"fields": ("region", "deployment")}),
        (
            "Governance",
            {"fields": ("status", "approved_for_use", "risk_classification", "notes")},
        ),
        ("Audit", {"fields": ("created_at", "updated_at")}),
    )

    def _set_approval(self, request, queryset, approved: bool):
        for model in queryset:
            before = snapshot(model, ["approved_for_use"])
            model.approved_for_use = approved
            model.save(update_fields=["approved_for_use", "updated_at"])
            AuditService.log_change(
                action=AuditAction.MODEL_APPROVAL,
                instance=model,
                before=before,
                actor=request.user,
            )
        verb = "Approved" if approved else "Revoked approval for"
        self.message_user(request, f"{verb} {queryset.count()} model(s).", messages.SUCCESS)

    @admin.action(description="Approve for use")
    def action_approve(self, request, queryset):
        self._set_approval(request, queryset, True)

    @admin.action(description="Revoke approval")
    def action_revoke_approval(self, request, queryset):
        self._set_approval(request, queryset, False)
