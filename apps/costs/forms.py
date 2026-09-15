"""Budgets, created in the web UI."""

from __future__ import annotations

from django.core.exceptions import ValidationError

from apps.core.forms import IntegerListField, OrganizationModelForm
from apps.costs.models import DEFAULT_THRESHOLDS, Budget, BudgetScope

#: The target field each scope uses; an organization-wide budget has none.
SCOPE_TARGETS = {
    BudgetScope.TEAM: "team",
    BudgetScope.APPLICATION: "application",
    BudgetScope.AGENT: "agent",
    BudgetScope.PROVIDER: "provider",
    BudgetScope.MODEL: "model",
}


class BudgetForm(OrganizationModelForm):
    alert_thresholds = IntegerListField(
        min_value=1, max_value=1000, label="Alert at (% of the budget)",
        help_text="Comma-separated percentages. Empty uses 50, 75, 90, 100.",
    )

    class Meta:
        model = Budget
        fields = [
            "name", "owner", "status", "scope", "team", "application", "agent", "provider", "model",
            "amount", "currency", "period", "starts_on", "alert_thresholds", "enforcement",
        ]
        labels = {"starts_on": "Starts on"}

    unique_in_organization = ("name",)
    sections = (
        ("Budget", "", ["name", "owner", "status"]),
        ("Scope", "What the spend is counted against.", ["scope", *SCOPE_TARGETS.values()]),
        ("Limit", "", ["amount", "currency", "period", "starts_on", "alert_thresholds"]),
        (
            "Enforcement",
            "A hard stop refuses AI Gateway requests once the budget is spent. It turns a finance control into "
            "an outage for whatever the budget covers, so choose it deliberately.",
            ["enforcement"],
        ),
    )
    compact_fields = ("owner", "status", "scope", *SCOPE_TARGETS.values(), "amount", "currency", "period", "starts_on")
    show_when = {target: f"scope:{scope}" for scope, target in SCOPE_TARGETS.items()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.creating:
            self.initial.setdefault("alert_thresholds", list(DEFAULT_THRESHOLDS))

    def clean_currency(self):
        currency = (self.cleaned_data.get("currency") or "").strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValidationError("A three-letter currency code, e.g. USD.")
        return currency

    def clean_amount(self):
        amount = self.cleaned_data.get("amount")
        if amount is not None and amount <= 0:
            raise ValidationError("A budget is more than zero.")
        return amount

    def clean(self):
        cleaned = super().clean()
        scope = cleaned.get("scope")
        wanted = SCOPE_TARGETS.get(scope)
        for target in SCOPE_TARGETS.values():
            if target != wanted:
                cleaned[target] = None  # a budget counts against one thing only
        if wanted and not cleaned.get(wanted):
            self.add_error(wanted, f"Choose the {wanted} this budget covers.")
        return cleaned
