"""Creating and editing budgets in the web UI."""

from __future__ import annotations

from django.contrib import messages

from apps.accounts.roles import Perm
from apps.core.manage import ManagedFormView
from apps.costs.forms import BudgetForm
from apps.costs.models import Budget
from apps.costs.services import BudgetService


class BudgetFormView(ManagedFormView):
    model = Budget
    form_class = BudgetForm
    permission = Perm.COST_MANAGE
    denied_message = "Budgets are managed by holders of cost.manage."
    noun = "budget"
    create_heading = "New budget"
    create_lead = "A spend limit for one slice of the estate, with alerts as it fills and, if you choose, a hard stop."
    create_submit_label = "Create budget"
    list_url_name = "costs:budgets"
    list_label = "Budgets"
    guide_template = "manage/guides/_budget.html"

    def after_commit(self, obj, form, creating):
        # Against current spend straight away, so a budget set below what is
        # already spent says so now rather than at the next scheduled check.
        raised = BudgetService.evaluate(obj)
        if raised:
            messages.warning(
                self.request,
                f"{len(raised)} threshold{'s' if len(raised) != 1 else ''} already crossed at current spend.",
            )
