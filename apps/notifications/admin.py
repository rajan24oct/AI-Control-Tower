from django.contrib import admin

from apps.notifications.models import Notification, NotificationPreference


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "recipient",
        "category",
        "severity",
        "title",
        "read_at",
        "organization",
    )
    list_filter = ("organization", "category", "severity")
    search_fields = ("title", "body", "recipient__email", "dedupe_key")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in Notification._meta.fields]

    def has_add_permission(self, request):
        # Notifications are raised by the emitters so routing and dedupe apply.
        return False


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "organization", "updated_at")
    list_filter = ("organization",)
    search_fields = ("user__email",)
    readonly_fields = ("id", "created_at", "updated_at")
