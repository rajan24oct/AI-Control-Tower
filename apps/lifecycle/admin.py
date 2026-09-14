from django.contrib import admin

from apps.lifecycle.models import (
    AuthorizationCondition,
    ChangeRecord,
    DeploymentAuthorization,
    GovernanceException,
)


class _ViewOnly(admin.ModelAdmin):
    """Gate records are decided in the Control Tower, where decision rights are
    enforced and every signature is audited. Editing one here would bypass both."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ConditionInline(admin.TabularInline):
    model = AuthorizationCondition
    extra = 0
    can_delete = False
    fields = ("description", "owner", "due_date", "met_at", "met_by")
    readonly_fields = fields


@admin.register(DeploymentAuthorization)
class DeploymentAuthorizationAdmin(_ViewOnly):
    list_display = ("agent", "application", "environment", "tier", "status", "requested_by", "requested_at", "decided_at")
    list_filter = ("organization", "status", "environment", "tier")
    search_fields = ("agent__name", "agent__agent_id", "application__name")
    inlines = [ConditionInline]


@admin.register(ChangeRecord)
class ChangeRecordAdmin(_ViewOnly):
    list_display = ("agent", "application", "change_type", "classification", "status", "detected_at", "changed_by_label")
    list_filter = ("organization", "status", "classification")
    search_fields = ("agent__name", "application__name", "description")


@admin.register(GovernanceException)
class GovernanceExceptionAdmin(_ViewOnly):
    list_display = ("agent", "policy", "tier", "status", "requested_at", "expires_at")
    list_filter = ("organization", "status", "tier")
    search_fields = ("agent__name", "policy__name", "justification")
