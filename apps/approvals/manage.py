"""Creating and editing approval rules in the web UI."""

from __future__ import annotations

from django.urls import reverse

from apps.accounts.roles import Perm
from apps.approvals.forms import ApprovalRuleForm
from apps.approvals.models import ApprovalRule
from apps.audit.models import AuditAction
from apps.core.manage import ManagedFormView


class ApprovalRuleFormView(ManagedFormView):
    model = ApprovalRule
    form_class = ApprovalRuleForm
    # Which actions wait for a person is governance configuration, like the
    # control policies that delegate to these rules.
    permission = Perm.POLICY_MANAGE
    denied_message = "Approval rules are managed by holders of policy.manage."
    noun = "approval rule"
    create_heading = "New approval rule"
    create_lead = "Which actions wait for a person to decide, and how serious a request raised by it is."
    create_submit_label = "Create rule"
    list_url_name = "approvals:centre"
    list_label = "Approval Centre"
    guide_template = "manage/guides/_approval_rule.html"
    audit_update_action = AuditAction.POLICY_CHANGE

    def detail_url(self, obj) -> str:
        return reverse("approvals:centre") + "#approval-rules"
