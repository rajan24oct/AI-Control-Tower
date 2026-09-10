from django.contrib import admin, messages

from apps.audit.admin import AuditedModelAdmin

from apps.costs.models import (
    Budget,
    BudgetAlert,
    DailyUsageRollup,
    ModelPricing,
    PricingVersion,
    ProviderPricing,
)
from apps.costs.services import BudgetService


class ModelPricingInline(admin.TabularInline):
    model = ModelPricing
    extra = 0
    autocomplete_fields = ("model",)
    fields = ("model", "input_cost_per_1k", "output_cost_per_1k", "currency")


class ProviderPricingInline(admin.TabularInline):
    model = ProviderPricing
    extra = 0
    autocomplete_fields = ("provider",)
    fields = ("provider", "request_fee", "tool_execution_fee", "currency")


@admin.register(PricingVersion)
class PricingVersionAdmin(AuditedModelAdmin):
    list_display = ("name", "organization", "effective_from", "effective_to", "currency", "status")
    list_filter = ("organization", "status", "currency")
    search_fields = ("name", "source")
    readonly_fields = ("id", "created_at", "updated_at")
    inlines = [ModelPricingInline, ProviderPricingInline]


@admin.register(ModelPricing)
class ModelPricingAdmin(AuditedModelAdmin):
    list_display = ("model", "version", "input_cost_per_1k", "output_cost_per_1k", "currency")
    list_filter = ("version", "currency")
    search_fields = ("model__model_name", "version__name")
    autocomplete_fields = ("model", "version")


@admin.register(ProviderPricing)
class ProviderPricingAdmin(AuditedModelAdmin):
    list_display = ("provider", "version", "request_fee", "tool_execution_fee", "currency")
    list_filter = ("version",)
    search_fields = ("provider__name",)
    autocomplete_fields = ("provider", "version")


@admin.register(Budget)
class BudgetAdmin(AuditedModelAdmin):
    list_display = ("name", "organization", "scope", "scope_label", "amount", "currency", "period", "status")
    list_filter = ("organization", "scope", "period", "status")
    search_fields = ("name",)
    autocomplete_fields = ("team", "application", "agent", "provider", "model", "owner")
    readonly_fields = ("id", "created_at", "updated_at")
    actions = ("action_evaluate",)

    @admin.action(description="Evaluate against current spend")
    def action_evaluate(self, request, queryset):
        raised = sum(len(BudgetService.evaluate(budget)) for budget in queryset)
        self.message_user(
            request,
            f"Evaluated {queryset.count()} budget(s); raised {raised} new alert(s).",
            messages.SUCCESS,
        )


@admin.register(BudgetAlert)
class BudgetAlertAdmin(admin.ModelAdmin):
    list_display = ("budget", "threshold", "percentage", "spend", "period_start", "acknowledged_at")
    list_filter = ("organization", "threshold", "budget")
    search_fields = ("budget__name",)
    readonly_fields = [f.name for f in BudgetAlert._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(DailyUsageRollup)
class DailyUsageRollupAdmin(admin.ModelAdmin):
    list_display = ("date", "organization", "application", "agent", "model", "executions", "total_tokens", "cost")
    list_filter = ("organization", "date")
    date_hierarchy = "date"
    readonly_fields = [f.name for f in DailyUsageRollup._meta.fields]

    def has_add_permission(self, request):
        return False
