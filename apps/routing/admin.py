from django.contrib import admin

from apps.routing.models import RoutingDecision, RoutingPolicy


@admin.register(RoutingPolicy)
class RoutingPolicyAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "model_type", "strategy", "priority", "status")
    list_filter = ("organization", "model_type", "strategy", "status")
    search_fields = ("name", "description")
    filter_horizontal = ("agents", "allowed_providers", "blocked_providers")


@admin.register(RoutingDecision)
class RoutingDecisionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "organization", "model_type", "selected_model", "outcome", "attempt")
    list_filter = ("organization", "outcome", "strategy", "model_type")
    # A decision is a record of what was sent; editing one would make the log
    # a worse record than no log at all.
    readonly_fields = [field.name for field in RoutingDecision._meta.fields]

    def has_add_permission(self, request):
        return False
