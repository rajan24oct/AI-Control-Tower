"""Budgets, cost alert rules and developer-tool subscriptions, created in the web UI."""

from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError
from django.db.models.functions import Trim

from apps.applications_registry.models import AIApplication
from apps.core.forms import IntegerListField, JSONObjectField, OrganizationModelForm
from apps.costs.models import (
    DEFAULT_THRESHOLDS,
    AlertMetric,
    Budget,
    BudgetScope,
    CostAlertRule,
    CostSource,
    DevToolSeat,
    DevToolSubscription,
)
from apps.organizations.models import Team
from integrations.registry import AdapterKind, registry

#: The target field each scope uses; an organization-wide budget has none.
SCOPE_TARGETS = {
    BudgetScope.DEPARTMENT: "department",
    BudgetScope.TEAM: "team",
    BudgetScope.USER: "user",
    BudgetScope.APPLICATION: "application",
    BudgetScope.AGENT: "agent",
    BudgetScope.PROVIDER: "provider",
    BudgetScope.MODEL: "model",
}
#: Scopes a developer-tool budget can be held against: seats belong to people.
DEVTOOL_SCOPES = {BudgetScope.ORGANIZATION, BudgetScope.DEPARTMENT, BudgetScope.TEAM, BudgetScope.USER}


def departments(organization) -> list[str]:
    """Every department named on a team or application, for a picker."""
    names = set()
    for queryset in (
        Team.objects.filter(organization=organization),
        AIApplication.objects.for_organization(organization),
    ):
        names.update(
            name for name in queryset.annotate(d=Trim("department")).values_list("d", flat=True) if name
        )
    return sorted(names, key=str.lower)


def clean_currency_code(value) -> str:
    currency = (value or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValidationError("A three-letter currency code, e.g. USD.")
    return currency


class ScopedFormMixin:
    """The scope picker budgets and alert rules share."""

    def setup_department_choices(self):
        choices = [("", "---------")] + [(name, name) for name in departments(self.organization)]
        current = self.initial.get("department") or getattr(self.instance, "department", "")
        if current and (current, current) not in choices:
            choices.append((current, current))
        self.fields["department"] = forms.ChoiceField(
            choices=choices, required=False, label="Department",
            help_text="As recorded on teams and applications.",
        )

    def clean_scope_targets(self, cleaned):
        scope = cleaned.get("scope")
        wanted = SCOPE_TARGETS.get(scope)
        for target in SCOPE_TARGETS.values():
            if target != wanted:
                cleaned[target] = "" if target == "department" else None  # counts against one thing only
        if wanted and not cleaned.get(wanted):
            noun = "individual" if wanted == "user" else wanted
            self.add_error(wanted, f"Choose the {noun} this covers.")
        return cleaned


class BudgetForm(ScopedFormMixin, OrganizationModelForm):
    alert_thresholds = IntegerListField(
        min_value=1, max_value=1000, label="Alert at (% of the budget)",
        help_text="Comma-separated percentages. Empty uses 50, 75, 90, 100.",
    )

    class Meta:
        model = Budget
        fields = [
            "name", "owner", "status", "cost_source", "scope", "department", "team", "user", "application",
            "agent", "provider", "model", "amount", "currency", "period", "starts_on", "alert_thresholds",
            "forecast_alerts", "notify_owner", "notify_team_lead", "extra_recipients", "enforcement",
        ]
        labels = {"starts_on": "Starts on", "user": "Individual", "cost_source": "Counts"}
        widgets = {"extra_recipients": forms.CheckboxSelectMultiple}

    unique_in_organization = ("name",)
    sections = (
        ("Budget", "", ["name", "owner", "status"]),
        (
            "Scope",
            "What the spend is counted against. Developer-tool spend (GitHub Copilot seats and premium requests) "
            "can be held against the organization, a department, a team or an individual.",
            ["cost_source", "scope", *SCOPE_TARGETS.values()],
        ),
        ("Limit", "", ["amount", "currency", "period", "starts_on"]),
        (
            "Thresholds and notifications",
            "Each threshold alerts once per period. Cost managers are always told; choose who else should be.",
            ["alert_thresholds", "forecast_alerts", "notify_owner", "notify_team_lead", "extra_recipients"],
        ),
        (
            "Enforcement",
            "A hard stop refuses AI Gateway requests once the budget is spent. It turns a finance control into "
            "an outage for whatever the budget covers, so choose it deliberately.",
            ["enforcement"],
        ),
    )
    compact_fields = (
        "owner", "status", "cost_source", "scope", *SCOPE_TARGETS.values(), "amount", "currency", "period", "starts_on",
    )
    show_when = {
        **{target: f"scope:{scope}" for scope, target in SCOPE_TARGETS.items()},
        "notify_team_lead": "scope:team",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_department_choices()
        # Optional so a client that predates developer-tool budgets keeps working.
        self.fields["cost_source"].required = False
        self.apply_styles()
        if self.creating:
            self.initial.setdefault("alert_thresholds", list(DEFAULT_THRESHOLDS))

    def clean_cost_source(self):
        return self.cleaned_data.get("cost_source") or CostSource.RUNTIME

    def clean_currency(self):
        return clean_currency_code(self.cleaned_data.get("currency"))

    def clean_amount(self):
        amount = self.cleaned_data.get("amount")
        if amount is not None and amount <= 0:
            raise ValidationError("A budget is more than zero.")
        return amount

    def clean(self):
        cleaned = self.clean_scope_targets(super().clean())
        source, scope = cleaned.get("cost_source"), cleaned.get("scope")
        if source and source != "runtime" and scope not in DEVTOOL_SCOPES:
            self.add_error(
                "scope", "Developer-tool spend belongs to people: use the organization, a department, a team or an individual."
            )
        if source == "developer_tools" and cleaned.get("enforcement") == "hard_stop":
            self.add_error("enforcement", "The gateway cannot stop Copilot; a developer-tool budget can only alert.")
        return cleaned


class CostAlertRuleForm(ScopedFormMixin, OrganizationModelForm):
    class Meta:
        model = CostAlertRule
        fields = [
            "name", "description", "owner", "status", "scope", "department", "team", "user", "application", "agent",
            "provider", "model", "metric", "window_hours", "threshold", "minimum_spend", "currency", "severity",
            "cooldown_hours", "notify_cost_managers", "recipients",
        ]
        labels = {"user": "Individual", "cooldown_hours": "Quiet period (hours)"}
        widgets = {"recipients": forms.CheckboxSelectMultiple}

    unique_in_organization = ("name",)
    sections = (
        ("Rule", "", ["name", "description", "owner", "status"]),
        ("Scope", "Which AI runtime spend the rule watches.", ["scope", *SCOPE_TARGETS.values()]),
        (
            "Condition",
            "Alert when the metric, measured over the rolling window, reaches the threshold. Spend increase "
            "compares the window with the average of the four windows before it.",
            ["metric", "window_hours", "threshold", "minimum_spend", "currency"],
        ),
        ("Notification", "", ["severity", "cooldown_hours", "notify_cost_managers", "recipients"]),
    )
    compact_fields = (
        "owner", "status", "scope", *SCOPE_TARGETS.values(), "metric", "window_hours", "threshold",
        "minimum_spend", "currency", "severity", "cooldown_hours",
    )
    show_when = {target: f"scope:{scope}" for scope, target in SCOPE_TARGETS.items()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_department_choices()
        self.apply_styles()

    def clean_currency(self):
        return clean_currency_code(self.cleaned_data.get("currency"))

    def clean(self):
        cleaned = self.clean_scope_targets(super().clean())
        threshold = cleaned.get("threshold")
        if threshold is not None and threshold <= 0:
            self.add_error("threshold", "A threshold is more than zero.")
        if cleaned.get("metric") == AlertMetric.SPEND_INCREASE and threshold is not None and threshold < 10:
            self.add_error("threshold", "A spike threshold is a percentage, e.g. 50 for half as much again.")
        return cleaned


class DevToolSubscriptionForm(OrganizationModelForm):
    options = JSONObjectField(
        required=False,
        help_text='Optional. {"login_map": {"github-login": "person@company.com"}} links logins to people.',
    )

    class Meta:
        model = DevToolSubscription
        fields = [
            "name", "owner", "status", "platform", "adapter_key", "account", "endpoint", "credential_ref",
            "currency", "business_seat_price", "enterprise_seat_price", "business_included_requests",
            "enterprise_included_requests", "premium_request_price", "options",
        ]
        labels = {"account": "GitHub organization", "credential_ref": "Token environment variable"}

    unique_in_organization = ("name",)
    sections = (
        ("Subscription", "", ["name", "owner", "status", "platform"]),
        (
            "Connection",
            "The token is read from the environment at sync time and never stored. It needs to read Copilot "
            "seats (manage_billing:copilot or read:org) and the organization's billing usage.",
            ["adapter_key", "account", "endpoint", "credential_ref"],
        ),
        (
            "Rates",
            "GitHub's list prices by default. Change them to match your agreement.",
            ["currency", "business_seat_price", "enterprise_seat_price", "business_included_requests",
             "enterprise_included_requests", "premium_request_price"],
        ),
        ("Advanced", "", ["options"]),
    )
    compact_fields = (
        "owner", "status", "platform", "adapter_key", "account", "credential_ref", "currency",
        "business_seat_price", "enterprise_seat_price", "business_included_requests",
        "enterprise_included_requests", "premium_request_price",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [
            (key, label) for key, label in registry.choices(AdapterKind.COST)
            if hasattr(registry.get_class(AdapterKind.COST, key), "list_seats")
        ]
        self.fields["adapter_key"] = forms.ChoiceField(
            choices=choices or [("github_copilot", "GitHub Copilot")], label="Integration",
        )
        self.apply_styles()

    def clean_currency(self):
        return clean_currency_code(self.cleaned_data.get("currency"))


class DevToolSeatForm(OrganizationModelForm):
    """Who a seat belongs to. The rest of a seat comes from GitHub on every sync."""

    class Meta:
        model = DevToolSeat
        fields = ["user", "team"]
        labels = {"user": "Person"}

    sections = (
        (
            "Attribution",
            "Links the GitHub login to a person and a team, so its cost rolls up correctly. Kept across syncs.",
            ["user", "team"],
        ),
    )
    compact_fields = ("user", "team")
