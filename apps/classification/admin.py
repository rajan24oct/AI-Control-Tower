from django.contrib import admin

from apps.classification.models import RiskClassification


@admin.register(RiskClassification)
class RiskClassificationAdmin(admin.ModelAdmin):
    """View only.

    Classifications are scored and countersigned in the Control Tower, where the
    independence rule (IRGF M6) is enforced and every step is audited. Editing
    one here would bypass both, and deleting one would erase score history
    IRGF asks to keep.
    """

    list_display = (
        "__str__", "organization", "tier", "impact", "control_deficit", "status",
        "scored_by", "countersigned_by", "scored_at",
    )
    list_filter = ("organization", "status", "tier", "rescore_required")
    search_fields = ("agent__name", "application__name", "rationale")
    date_hierarchy = "scored_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
