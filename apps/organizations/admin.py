from django.contrib import admin

from apps.audit.admin import AuditedModelAdmin
from apps.audit.models import AuditAction
from apps.organizations.models import Organization, OrganizationMembership, Team, TeamMembership


class OrganizationMembershipInline(admin.TabularInline):
    model = OrganizationMembership
    extra = 0
    autocomplete_fields = ("user",)
    fields = ("user", "role", "is_default")


@admin.register(Organization)
class OrganizationAdmin(AuditedModelAdmin):
    list_display = ("name", "slug", "industry", "status", "created_at")
    list_filter = ("status", "industry")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("id", "created_at", "updated_at")
    inlines = [OrganizationMembershipInline]


@admin.register(Team)
class TeamAdmin(AuditedModelAdmin):
    list_display = ("name", "organization", "department", "lead", "status")
    list_filter = ("organization", "status", "department")
    search_fields = ("name", "department")
    autocomplete_fields = ("lead",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(AuditedModelAdmin):
    # Section 29 calls out user role changes specifically.
    audit_update_action = AuditAction.ROLE_CHANGE
    list_display = ("user", "organization", "role", "is_default")
    list_filter = ("organization", "role")
    search_fields = ("user__email", "organization__name")
    autocomplete_fields = ("user", "organization")


@admin.register(TeamMembership)
class TeamMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "team")
    list_filter = ("team__organization", "team")
    search_fields = ("user__email", "team__name")
    autocomplete_fields = ("user", "team")
