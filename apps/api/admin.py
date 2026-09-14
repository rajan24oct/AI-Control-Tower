from django.contrib import admin, messages

from apps.api.models import APIKey, ServiceAccount


@admin.register(ServiceAccount)
class ServiceAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "external_system", "role", "bound_agents", "status")
    list_filter = ("organization", "role", "status")
    search_fields = ("name", "external_system")
    readonly_fields = ("id", "created_at", "updated_at")
    # IRGF agent chain link 02: which agents this identity may act as.
    filter_horizontal = ("agents",)
    actions = ("action_issue_key",)

    @admin.display(description="Acts as")
    def bound_agents(self, obj):
        count = obj.agents.count()
        return f"{count} agent{'s' if count != 1 else ''}" if count else "Any, up to Tier 2"

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        # Only the account's own organization's agents can be bound.
        if db_field.name == "agents":
            from apps.agents.models import AIAgent

            account_id = request.resolver_match.kwargs.get("object_id") if request.resolver_match else None
            account = ServiceAccount.objects.filter(pk=account_id).first() if account_id else None
            queryset = AIAgent.objects.all()
            if account is not None:
                queryset = queryset.filter(organization=account.organization)
            kwargs["queryset"] = queryset.order_by("name")
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    @admin.action(description="Issue a new API key")
    def action_issue_key(self, request, queryset):
        for account in queryset:
            _, raw_key = APIKey.generate(account, name="issued-from-admin")
            self.message_user(
                request,
                f"API key for {account.name}: {raw_key} -- copy it now, it cannot be shown again.",
                messages.WARNING,
            )


@admin.register(APIKey)
class APIKeyAdmin(admin.ModelAdmin):
    """Keys are displayed by prefix only; the secret is never recoverable."""

    list_display = ("prefix", "service_account", "name", "last_used_at", "expires_at", "revoked_at")
    list_filter = ("service_account__organization", "service_account")
    search_fields = ("prefix", "name", "service_account__name")
    readonly_fields = ("id", "prefix", "key_hash", "last_used_at", "created_at", "updated_at")
    actions = ("action_revoke",)

    def has_add_permission(self, request):
        # Keys must be created through ServiceAccount so the raw value is shown.
        return False

    @admin.action(description="Revoke selected keys")
    def action_revoke(self, request, queryset):
        for key in queryset:
            key.revoke()
        self.message_user(request, f"Revoked {queryset.count()} key(s).", messages.SUCCESS)
