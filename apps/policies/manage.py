"""Creating and editing control policies in the web UI."""

from __future__ import annotations

from apps.accounts.roles import Perm
from apps.audit.models import AuditAction
from apps.core.manage import ManagedFormView
from apps.policies.forms import STARTERS, PolicyForm
from apps.policies.models import INHERENT_CHECKS, POLICY_SCHEMA, Policy, PolicyType


class PolicyFormView(ManagedFormView):
    model = Policy
    form_class = PolicyForm
    permission = Perm.POLICY_MANAGE
    denied_message = "Control policies are managed by holders of policy.manage."
    noun = "control policy"
    create_heading = "New control policy"
    create_lead = (
        "A typed control both gateways enforce on every call: deny it, hold it for a person, or let it "
        "through with a warning."
    )
    create_submit_label = "Create policy"
    list_url_name = "policies:list"
    list_label = "Policies"
    detail_url_name = "policies:detail"
    guide_template = "manage/guides/_policy.html"
    audit_update_action = AuditAction.POLICY_CHANGE

    def get_context_data(self):
        return {
            "policy_types": [
                {
                    "value": value,
                    "label": label,
                    # `tiers` has its own boxes on the form, so it is not listed here.
                    "keys": sorted(POLICY_SCHEMA.get(value, set())),
                    "inherent": INHERENT_CHECKS.get(value, ""),
                    "starter": STARTERS.get(value, {}),
                }
                for value, label in PolicyType.choices
            ],
        }

    def success_message(self, obj, creating, changed):
        if creating:
            return f"Policy “{obj}” created. Both gateways enforce it from the next request."
        return super().success_message(obj, creating, changed)
