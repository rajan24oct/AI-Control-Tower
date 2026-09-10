from django.contrib import admin

from apps.audit.admin import AuditedModelAdmin

from apps.approvals.models import ApprovalRequest, ApprovalRule


@admin.register(ApprovalRule)
class ApprovalRuleAdmin(AuditedModelAdmin):
    list_display = ("name", "organization", "priority", "severity", "summary", "status")
    list_filter = ("organization", "severity", "status")
    search_fields = ("name", "description")
    readonly_fields = ("id", "created_at", "updated_at")

    fieldsets = (
        (None, {"fields": ("id", "organization", "name", "description", "priority", "severity", "status")}),
        (
            "Conditions",
            {
                "description": "Leave a condition empty to skip it. A rule matches only when "
                "every condition it specifies is satisfied.",
                "fields": (
                    "min_autonomy_level",
                    "risk_levels",
                    "environments",
                    "data_classifications",
                    "tools",
                    "actions",
                    "cost_threshold",
                    "match_restricted_tool",
                    "match_unapproved_provider",
                    "match_unapproved_model",
                ),
            },
        ),
        ("Audit", {"fields": ("created_at", "updated_at")}),
    )

    @admin.display(description="Conditions")
    def summary(self, obj):
        return "; ".join(obj.conditions) or "no conditions (never matches)"


@admin.register(ApprovalRequest)
class ApprovalRequestAdmin(admin.ModelAdmin):
    list_display = (
        "request_id",
        "requested_action",
        "agent",
        "risk_level",
        "status",
        "requested_at",
        "assigned_approver",
        "decision_by",
        "decision_at",
    )
    list_filter = ("organization", "status", "risk_level", "agent")
    search_fields = ("request_id", "requested_action", "reason", "decision_notes")
    date_hierarchy = "requested_at"
    autocomplete_fields = ("agent", "application", "assigned_approver")
    # Decisions are made in the Approval Centre so they are audited correctly.
    readonly_fields = (
        "id", "request_id", "execution", "matched_rules", "context",
        "decision_at", "decision_by", "created_at", "updated_at",
    )
