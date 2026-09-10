from django.contrib import admin

from apps.audit.models import AuditAction, AuditLog
from apps.audit.services import AuditService, diff, snapshot


class AuditedModelAdmin(admin.ModelAdmin):
    """Records every create, change and delete made through the admin.

    Wired explicitly through the admin hooks rather than model signals: the
    admin is where configuration is actually edited, and an explicit override
    keeps the capture visible at the point it happens (section 42).
    """

    #: Overridden per admin where a domain-specific verb reads better, e.g.
    #: `model_approval` instead of a generic `update`.
    audit_update_action = AuditAction.CONFIG_CHANGE

    def save_model(self, request, obj, form, change):
        before = None
        if change and obj.pk:
            existing = type(obj).objects.filter(pk=obj.pk).first()
            if existing is not None:
                before = snapshot(existing)

        super().save_model(request, obj, form, change)

        after = snapshot(obj)
        if change:
            old_value, new_value = diff(before, after)
            if not new_value:
                # Saving without changing anything is not worth a trail entry.
                return
            action = self.audit_update_action
        else:
            old_value, new_value, action = None, after, AuditAction.CREATE

        AuditService.log(
            action=action,
            resource_type=obj._meta.label,
            resource_id=obj.pk,
            resource_label=str(obj),
            organization=getattr(obj, "organization", None),
            actor=request.user,
            old_value=old_value,
            new_value=new_value,
        )

    def delete_model(self, request, obj):
        self._log_delete(request, obj)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            self._log_delete(request, obj)
        super().delete_queryset(request, queryset)

    @staticmethod
    def _log_delete(request, obj):
        AuditService.log(
            action=AuditAction.DELETE,
            resource_type=obj._meta.label,
            resource_id=obj.pk,
            resource_label=str(obj),
            organization=getattr(obj, "organization", None),
            actor=request.user,
            old_value=snapshot(obj),
        )


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Strictly read-only: the audit trail is evidence, not editable data."""

    list_display = (
        "created_at",
        "actor_label",
        "action",
        "resource_type",
        "resource_label",
        "organization",
        "source",
        "integrity",
    )
    list_filter = ("action", "source", "resource_type", "organization")
    search_fields = ("actor_label", "resource_label", "resource_id", "action")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    @admin.display(description="Integrity", boolean=True)
    def integrity(self, obj):
        return obj.verify()

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
