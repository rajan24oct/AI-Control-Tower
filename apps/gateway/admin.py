from django.contrib import admin

from apps.gateway.models import GatewayMessage, ProviderCircuit


@admin.register(ProviderCircuit)
class ProviderCircuitAdmin(admin.ModelAdmin):
    list_display = ("provider", "state", "consecutive_failures", "trips", "last_failure_at")
    list_filter = ("state",)
    readonly_fields = ("trips", "last_failure_at", "last_error")


@admin.register(GatewayMessage)
class GatewayMessageAdmin(admin.ModelAdmin):
    list_display = ("created_at", "sender", "recipient", "subject", "status")
    list_filter = ("organization", "status")
    search_fields = ("subject", "body", "correlation_id")
    # Brokered traffic is a record of what agents said to each other; editing it
    # would make the log less trustworthy than not keeping one.
    readonly_fields = [f.name for f in GatewayMessage._meta.fields]

    def has_add_permission(self, request):
        return False
