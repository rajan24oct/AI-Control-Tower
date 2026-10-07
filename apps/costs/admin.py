from django.contrib import admin, messages

from apps.audit.admin import AuditedModelAdmin

from apps.costs.models import (
    Budget,
    BudgetAlert,
    CostAlertEvent,
    CostAlertRule,
    CostRecommendation,
    DevToolSeat,
    DevToolSubscription,
    DevToolUsage,
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
    autocomplete_fields = ("team", "user", "application", "agent", "provider", "model", "owner")
    filter_horizontal = ("extra_recipients",)
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
    list_display = ("budget", "kind", "threshold", "percentage", "spend", "period_start", "acknowledged_at")
    list_filter = ("organization", "kind", "threshold", "budget")
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


@admin.register(CostAlertRule)
class CostAlertRuleAdmin(AuditedModelAdmin):
    list_display = ("name", "organization", "scope", "metric", "window_hours", "threshold", "severity", "status", "last_triggered_at")
    list_filter = ("organization", "metric", "severity", "status")
    search_fields = ("name",)
    autocomplete_fields = ("team", "user", "application", "agent", "provider", "model", "owner")
    filter_horizontal = ("recipients",)
    readonly_fields = ("id", "created_at", "updated_at", "last_evaluated_at", "last_value", "last_triggered_at")


@admin.register(CostAlertEvent)
class CostAlertEventAdmin(admin.ModelAdmin):
    list_display = ("rule", "severity", "value", "threshold", "created_at", "acknowledged_at")
    list_filter = ("organization", "severity", "rule")
    readonly_fields = [f.name for f in CostAlertEvent._meta.fields]

    def has_add_permission(self, request):
        return False


class DevToolSeatInline(admin.TabularInline):
    model = DevToolSeat
    extra = 0
    fields = ("login", "user", "team", "plan", "last_activity_at", "is_active")
    readonly_fields = ("login", "plan", "last_activity_at", "is_active")
    autocomplete_fields = ("user", "team")


@admin.register(DevToolSubscription)
class DevToolSubscriptionAdmin(AuditedModelAdmin):
    list_display = ("name", "organization", "platform", "account", "adapter_key", "last_synced_at", "status")
    list_filter = ("organization", "platform", "status")
    search_fields = ("name", "account")
    readonly_fields = ("id", "created_at", "updated_at", "last_synced_at", "last_sync_message")
    inlines = [DevToolSeatInline]


@admin.register(DevToolSeat)
class DevToolSeatAdmin(AuditedModelAdmin):
    list_display = ("login", "subscription", "user", "team", "plan", "last_activity_at", "is_active")
    list_filter = ("organization", "subscription", "plan", "is_active", "team")
    search_fields = ("login", "display_name", "email", "user__email")
    autocomplete_fields = ("user", "team")


@admin.register(DevToolUsage)
class DevToolUsageAdmin(admin.ModelAdmin):
    list_display = ("seat", "date", "premium_requests")
    list_filter = ("organization",)
    date_hierarchy = "date"
    search_fields = ("seat__login",)


@admin.register(CostRecommendation)
class CostRecommendationAdmin(admin.ModelAdmin):
    list_display = ("title", "organization", "kind", "estimated_monthly_savings", "confidence", "status", "last_detected_at")
    list_filter = ("organization", "category", "kind", "status", "confidence")
    search_fields = ("title", "rationale")
    readonly_fields = ("fingerprint", "first_detected_at", "last_detected_at", "evidence")
