"""Recording and editing risks in the web UI."""

from __future__ import annotations

from django.conf import settings

from apps.accounts.roles import Perm
from apps.core.manage import ManagedFormView
from apps.risks.forms import RiskForm
from apps.risks.models import Risk

SUBJECT_FIELDS = ("application", "agent", "model", "provider")


class RiskFormView(ManagedFormView):
    model = Risk
    form_class = RiskForm
    permission = Perm.RISK_MANAGE
    denied_message = "The risk register is managed by holders of risk.manage."
    noun = "risk"
    create_heading = "Record a risk"
    create_lead = "One AI risk, scored before and after the controls that mitigate it."
    create_submit_label = "Record risk"
    list_url_name = "risks:list"
    list_label = "Risk register"
    detail_url_name = "risks:detail"
    guide_template = "manage/guides/_risk.html"

    def get_form(self, data=None):
        form = super().get_form(data)
        if self.object is None and data is None:
            for name in SUBJECT_FIELDS:
                if self.request.GET.get(name):
                    form.initial[name] = self.request.GET[name]
        return form

    def get_context_data(self):
        return {"risk_thresholds": settings.CONTROL_TOWER["RISK_THRESHOLDS"]}

    def success_message(self, obj, creating, changed):
        if creating:
            return f"Risk {obj.risk_id} recorded, scored {obj.inherent_risk_score} ({obj.inherent_level})."
        return super().success_message(obj, creating, changed)
