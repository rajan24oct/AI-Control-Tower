from django.contrib import admin, messages

from apps.a2a.models import A2AAgentCard, A2AContract, A2ASkill, A2ATask
from apps.a2a.registry import A2ARegistryService
from apps.audit.admin import AuditedModelAdmin


@admin.register(A2AAgentCard)
class A2AAgentCardAdmin(AuditedModelAdmin):
    list_display = (
        "name", "origin", "transport", "is_approved", "access_mode", "data_classification",
        "health_status", "circuit_state", "status",
    )
    list_filter = ("organization", "origin", "transport", "is_approved", "access_mode", "status")
    search_fields = ("name", "slug", "description", "publisher", "endpoint")
    # Approval goes through the actions so it is stamped and audited as an
    # approval; the card, health and the breaker are written by the platform.
    readonly_fields = (
        "id", "is_approved", "approved_at", "approved_by",
        "card", "card_hash", "protocol_version", "agent_version", "capabilities",
        "last_synced_at", "last_sync_error",
        "health_status", "last_health_check", "last_health_latency_ms", "last_health_message",
        "circuit_state", "consecutive_failures", "circuit_opened_at", "last_failure_at",
        "last_error", "circuit_trips", "created_at", "updated_at",
    )
    fieldsets = (
        (None, {"fields": (
            "id", "organization", "origin", "agent", "name", "slug", "description", "publisher", "documentation_url",
        )}),
        ("Delivery", {"fields": (
            "transport", "endpoint", "card_url", "auth_method", "credential_ref", "auth_header", "options",
            "declared_skills",
        )}),
        ("Governance", {"fields": (
            "status", "is_approved", "approved_at", "approved_by", "access_mode",
            "data_classification", "risk_level", "owner", "team",
        )}),
        ("Card", {"fields": (
            "card", "card_hash", "protocol_version", "agent_version", "capabilities",
            "last_synced_at", "last_sync_error",
        )}),
        ("Health and breaker", {"classes": ("collapse",), "fields": (
            "health_status", "last_health_check", "last_health_latency_ms", "last_health_message",
            "circuit_state", "consecutive_failures", "circuit_opened_at", "last_failure_at",
            "last_error", "circuit_trips",
        )}),
        ("Audit", {"fields": ("created_at", "updated_at")}),
    )
    actions = ("action_sync", "action_approve", "action_revoke")

    @admin.action(description="Sync skills from the agent's card")
    def action_sync(self, request, queryset):
        for card in queryset:
            result = A2ARegistryService.sync(card, actor=request.user)
            self.message_user(request, result.summary, messages.SUCCESS if result.ok else messages.ERROR)

    def _set_approval(self, request, queryset, approved: bool):
        for card in queryset:
            A2ARegistryService.set_approval(card, approved=approved, actor=request.user)
        verb = "Approved" if approved else "Revoked approval for"
        self.message_user(request, f"{verb} {queryset.count()} agent(s).", messages.SUCCESS)

    @admin.action(description="Approve for agent-to-agent traffic")
    def action_approve(self, request, queryset):
        self._set_approval(request, queryset, True)

    @admin.action(description="Revoke approval")
    def action_revoke(self, request, queryset):
        self._set_approval(request, queryset, False)


@admin.register(A2ASkill)
class A2ASkillAdmin(AuditedModelAdmin):
    list_display = ("skill_id", "card", "status", "risk_level", "changed_since_review", "last_seen_at")
    list_filter = ("organization", "status", "risk_level", "changed_since_review", "card")
    search_fields = ("skill_id", "name", "description", "card__name")
    fields = (
        "organization", "card", "skill_id", "name", "description", "tags", "examples", "input_modes",
        "output_modes", "definition_hash", "status", "risk_level", "changed_since_review", "first_seen_at",
        "last_seen_at", "reviewed_at", "reviewed_by", "review_note",
    )
    # Only the reviewer's judgement is editable here; the definition is what
    # the agent said, and the status moves through the review actions.
    readonly_fields = tuple(f for f in fields if f not in {"risk_level", "review_note"})
    actions = ("action_enable", "action_disable")

    def has_add_permission(self, request):
        # Skills come from the card. One typed in by hand would describe a skill
        # the agent may not actually have.
        return False

    def _review(self, request, queryset, enable: bool):
        reviewed = skipped = 0
        for skill in queryset.select_related("card"):
            try:
                A2ARegistryService.review(skill, enable=enable, actor=request.user)
                reviewed += 1
            except ValueError:
                skipped += 1
        verb = "Enabled" if enable else "Disabled"
        message = f"{verb} {reviewed} skill(s)."
        if skipped:
            message += f" Skipped {skipped} no longer offered by their agent."
        self.message_user(request, message, messages.SUCCESS)

    @admin.action(description="Enable (approve for agents)")
    def action_enable(self, request, queryset):
        self._review(request, queryset, True)

    @admin.action(description="Disable")
    def action_disable(self, request, queryset):
        self._review(request, queryset, False)


@admin.register(A2AContract)
class A2AContractAdmin(AuditedModelAdmin):
    list_display = ("sender", "recipient", "skill_scope", "max_data_classification", "max_messages_per_hour", "expires_at")
    list_filter = ("recipient",)
    search_fields = ("sender__name", "sender__agent_id", "recipient__name")
    # The organization follows the recipient; it is never chosen separately.
    fields = ("recipient", "sender", "allowed_skills", "max_data_classification", "max_messages_per_hour",
              "expires_at", "purpose")

    @admin.display(description="Skills")
    def skill_scope(self, obj):
        return ", ".join(obj.allowed_skills) if obj.allowed_skills else "All enabled skills"


@admin.register(A2ATask)
class A2ATaskAdmin(admin.ModelAdmin):
    list_display = ("created_at", "sender", "recipient_name", "skill_name", "status", "state", "hops", "channel")
    list_filter = ("organization", "status", "state", "channel")
    search_fields = ("recipient_name", "skill_name", "reason", "context_id")
    # The log is a record of what agents asked of each other; editing it would
    # make it less trustworthy than not keeping one.
    readonly_fields = [f.name for f in A2ATask._meta.fields]

    def has_add_permission(self, request):
        return False
