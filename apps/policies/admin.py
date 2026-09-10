from django.contrib import admin

from apps.audit.admin import AuditedModelAdmin
from apps.audit.models import AuditAction
from apps.policies.models import POLICY_SCHEMA, Policy, PolicyViolation


@admin.register(Policy)
class PolicyAdmin(AuditedModelAdmin):
    audit_update_action = AuditAction.POLICY_CHANGE
    list_display = ("name", "organization", "policy_type", "effect", "severity", "priority", "status")
    list_filter = ("organization", "policy_type", "effect", "severity", "status")
    search_fields = ("name", "description", "reference")
    readonly_fields = ("id", "created_at", "updated_at", "allowed_settings")

    fieldsets = (
        (None, {"fields": ("id", "organization", "name", "description", "reference")}),
        ("Behaviour", {"fields": ("policy_type", "effect", "severity", "priority", "status")}),
        (
            "Configuration",
            {
                "description": (
                    "Typed settings for the selected policy type. Unknown keys are "
                    "rejected on save so a typo cannot silently disable a control."
                ),
                "fields": ("allowed_settings", "configuration"),
            },
        ),
        ("Audit", {"fields": ("created_at", "updated_at")}),
    )

    @admin.display(description="Settings this type accepts")
    def allowed_settings(self, obj):
        if not obj or not obj.policy_type:
            return "Choose a policy type and save to see its settings."
        allowed = sorted(POLICY_SCHEMA.get(obj.policy_type, set()))
        return ", ".join(allowed) or "This type takes no settings."


@admin.register(PolicyViolation)
class PolicyViolationAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "policy",
        "effect",
        "severity",
        "agent",
        "requested_action",
        "acknowledged_at",
    )
    list_filter = ("organization", "effect", "severity", "policy")
    search_fields = ("reason", "requested_action", "policy__name")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in PolicyViolation._meta.fields]

    def has_add_permission(self, request):
        return False
