from django.contrib import admin

from apps.compliance.models import Attestation, RegulatoryScope


@admin.register(RegulatoryScope)
class RegulatoryScopeAdmin(admin.ModelAdmin):
    list_display = ("__str__", "organization", "regime", "applicability", "eu_category", "assessed_by", "assessed_at")
    list_filter = ("organization", "regime", "eu_category")


@admin.register(Attestation)
class AttestationAdmin(admin.ModelAdmin):
    list_display = ("requirement", "__str__", "organization", "status", "attested_by", "attested_at", "valid_until")
    list_filter = ("organization", "status")
    search_fields = ("requirement", "statement")
