from django.contrib import admin

from apps.audit.admin import AuditedModelAdmin

from apps.risks.models import Risk


@admin.register(Risk)
class RiskAdmin(AuditedModelAdmin):
    list_display = (
        "risk_id",
        "title",
        "risk_category",
        "likelihood",
        "impact",
        "inherent_risk_score",
        "residual_risk_score",
        "owner",
        "status",
        "review_date",
    )
    list_filter = ("organization", "risk_category", "status", "likelihood", "impact")
    search_fields = ("risk_id", "title", "description", "mitigation")
    autocomplete_fields = ("owner", "agent", "application", "model", "provider")
    # Scores are always derived from likelihood x impact.
    readonly_fields = ("id", "risk_id", "inherent_risk_score", "residual_risk_score", "created_at", "updated_at")

    fieldsets = (
        (None, {"fields": ("id", "risk_id", "organization", "title", "description", "risk_category")}),
        ("Affected assets", {"fields": ("application", "agent", "model", "provider")}),
        ("Inherent assessment", {"fields": ("likelihood", "impact", "inherent_risk_score")}),
        (
            "Mitigation",
            {"fields": ("mitigation", "residual_likelihood", "residual_impact", "residual_risk_score")},
        ),
        ("Ownership", {"fields": ("owner", "status", "review_date")}),
        ("Audit", {"fields": ("created_at", "updated_at")}),
    )
