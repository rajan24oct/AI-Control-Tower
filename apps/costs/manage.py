"""Creating and editing budgets, alert rules and developer-tool subscriptions in the web UI."""

from __future__ import annotations

from django.contrib import messages
from django.urls import reverse

from apps.accounts.roles import Perm
from apps.core.manage import ManagedFormView
from apps.costs.alerting import CostAlertService
from apps.costs.devtools import DevToolSyncService
from apps.costs.forms import BudgetForm, CostAlertRuleForm, DevToolSeatForm, DevToolSubscriptionForm
from apps.costs.models import Budget, CostAlertRule, DevToolSeat, DevToolSubscription
from apps.costs.services import BudgetService


class BudgetFormView(ManagedFormView):
    model = Budget
    form_class = BudgetForm
    permission = Perm.COST_MANAGE
    denied_message = "Budgets are managed by holders of cost.manage."
    noun = "budget"
    create_heading = "New budget"
    create_lead = (
        "A spend limit for one slice of the estate -- a department, team, individual, agent or model -- with "
        "alerts as it fills, a forecast warning before it runs out and, if you choose, a hard stop."
    )
    create_submit_label = "Create budget"
    list_url_name = "costs:budgets"
    list_label = "Budgets"
    detail_url_name = "costs:budget_detail"
    guide_template = "manage/guides/_budget.html"

    def snapshot(self, obj) -> dict:
        data = super().snapshot(obj)
        data["extra_recipients"] = sorted(str(pk) for pk in obj.extra_recipients.values_list("pk", flat=True))
        return data

    def after_commit(self, obj, form, creating):
        # Against current spend straight away, so a budget set below what is
        # already spent says so now rather than at the next scheduled check.
        raised = BudgetService.evaluate(obj)
        if raised:
            messages.warning(
                self.request,
                f"{len(raised)} alert{'s' if len(raised) != 1 else ''} raised at current spend.",
            )


class CostAlertRuleFormView(ManagedFormView):
    model = CostAlertRule
    form_class = CostAlertRuleForm
    permission = Perm.COST_MANAGE
    denied_message = "Cost alert rules are managed by holders of cost.manage."
    noun = "alert rule"
    create_heading = "New cost alert rule"
    create_lead = (
        "A guardrail on how spend behaves right now: a level, a spike against the recent baseline, an "
        "expensive average run, or money spent on failures."
    )
    create_submit_label = "Create rule"
    list_url_name = "costs:governance"
    list_label = "Cost Governance"
    guide_template = "manage/guides/_cost_alert_rule.html"

    def success_message(self, obj, creating: bool, changed: bool) -> str:
        if creating:
            return f"Alert rule “{obj}” created."
        return super().success_message(obj, creating, changed)

    def after_commit(self, obj, form, creating):
        if obj.status == "active":
            event = CostAlertService.evaluate(obj)
            if event is not None:
                messages.warning(self.request, f"The rule fired straight away: {event.summary}")


class DevToolSubscriptionFormView(ManagedFormView):
    model = DevToolSubscription
    form_class = DevToolSubscriptionForm
    permission = Perm.COST_MANAGE
    denied_message = "Developer-tool subscriptions are managed by holders of cost.manage."
    noun = "subscription"
    create_heading = "Connect GitHub Copilot"
    create_lead = (
        "Reads Copilot seats and premium-request usage from GitHub, so licence and usage spend can be seen "
        "by team and by individual, budgeted and optimized."
    )
    create_submit_label = "Connect"
    list_url_name = "costs:developer_tools"
    list_label = "Developer Tools"
    guide_template = "manage/guides/_devtool_subscription.html"

    def success_message(self, obj, creating: bool, changed: bool) -> str:
        if creating:
            return f"Connected “{obj}”."
        return super().success_message(obj, creating, changed)

    def after_commit(self, obj, form, creating):
        if creating and obj.status == "active":
            result = DevToolSyncService.sync(obj)
            if result.ok:
                messages.info(self.request, f"First sync: {result.message}")
            else:
                messages.warning(self.request, f"Saved, but the first sync failed: {result.message}")


class DevToolSeatFormView(ManagedFormView):
    model = DevToolSeat
    form_class = DevToolSeatForm
    permission = Perm.COST_MANAGE
    denied_message = "Seat attribution is managed by holders of cost.manage."
    noun = "seat"
    list_url_name = "costs:developer_tools"
    list_label = "Developer Tools"

    def get_object(self):
        obj = super().get_object()
        if obj is None:
            from django.http import Http404

            raise Http404("Seats come from GitHub; they are attributed here, not created.")
        return obj

    def detail_url(self, obj) -> str:
        return reverse("costs:developer_tools")
