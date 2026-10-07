"""FinOps demo data: who caused the spend, GitHub Copilot, scoped budgets,
alert rules and the first optimization run.

Called by `seed_demo` after the governance step, so budgets and rules are
calibrated against the spend that was actually generated -- a demo whose
alerts never fire, or all fire, shows nothing.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

from apps.core.constants import LifecycleStatus
from apps.costs.alerting import CostAlertService
from apps.costs.devtools import DevToolSyncService
from apps.costs.models import (
    AlertMetric,
    AlertSeverity,
    AlertWindow,
    Budget,
    BudgetPeriod,
    BudgetScope,
    CostAlertRule,
    CostSource,
    DevToolPlatform,
    DevToolSubscription,
)
from apps.costs.optimization import OptimizationService
from apps.costs.services import BudgetService
from apps.executions.models import Execution
from apps.organizations.models import TeamMembership

#: Developers holding Copilot seats beyond the platform's own users. Logins are
#: GitHub's, teams are matched by the granting GitHub team's slug.
DEVELOPERS = [
    # (login, name, github team slug, plan, profile, idle days)
    ("jmorales-nw", "Jorge Morales", "ai-platform", "enterprise", "heavy", 0),
    ("kwang-dev", "Kevin Wang", "ai-platform", "enterprise", "regular", 1),
    ("lpetrova", "Lena Petrova", "ai-platform", "business", "heavy", 0),
    ("abose", "Arjun Bose", "ai-platform", "business", "regular", 2),
    ("tnakamura", "Taro Nakamura", "ai-platform", "enterprise", "light", 3),
    ("hdubois", "Hélène Dubois", "data-science", "business", "heavy", 0),
    ("sokoye", "Sade Okoye", "data-science", "business", "regular", 0),
    ("rgupta-ds", "Rohan Gupta", "data-science", "enterprise", "light", 5),
    ("mfischer", "Mia Fischer", "data-science", "business", "idle", 47),
    ("cpark", "Chloe Park", "customer-experience", "business", "regular", 1),
    ("dnovak", "David Novak", "customer-experience", "business", "light", 4),
    ("eosei", "Esi Osei", "customer-experience", "business", "idle", 64),
    ("fkhan", "Farah Khan", "customer-experience", "enterprise", "light", 2),
    ("glindqvist", "Gustav Lindqvist", "risk-compliance", "business", "light", 6),
    ("ialvarez", "Inés Álvarez", "risk-compliance", "business", "idle", 38),
    ("contractor-qa1", "QA Contractor 1", "", "business", "idle", 90),
]

#: The platform's own users, by e-mail: (login, plan, profile, idle days).
USER_SEATS = {
    "daniel.okafor@northwind.example": ("dokafor", "enterprise", "regular", 0),
    "priya.raman@northwind.example": ("praman-nw", "enterprise", "heavy", 0),
    "marcus.lee@northwind.example": ("marcuslee", "business", "heavy", 0),
    "sofia.hernandez@northwind.example": ("shernandez", "business", "light", 9),
    "ava.mitchell@northwind.example": ("amitchell", "enterprise", "idle", 41),
}

SUBSCRIPTION_NAME = "GitHub Copilot - Northwind Engineering"


def _stable(value: str) -> int:
    return int(hashlib.sha256(value.encode()).hexdigest()[:8], 16)


def attribute_executions(organization, teams: dict, users: dict) -> int:
    """Give generated runs a person: a member of the agent's team.

    Real traffic carries one when it comes through the AI Gateway with a user,
    or from telemetry that names one; the mock generator leaves it empty. The
    choice is a hash of the run's id, so a reseed attributes the same way.
    """
    members_by_team = {}
    for membership in TeamMembership.objects.filter(team__organization=organization).select_related("user"):
        members_by_team.setdefault(membership.team_id, []).append(membership.user)
    everyone = list(users.values())

    pending = list(
        Execution.objects.for_organization(organization)
        .filter(user__isnull=True, source_adapter="mock")
        .select_related("agent", "application")
    )
    for execution in pending:
        team_id = (execution.application.team_id if execution.application_id else None) or (
            execution.agent.team_id if execution.agent_id else None
        )
        pool = members_by_team.get(team_id) or everyone
        execution.user = pool[_stable(execution.external_id or str(execution.pk)) % len(pool)]
    Execution.objects.bulk_update(pending, ["user"], batch_size=500)
    return len(pending)


def seed_copilot(organization, users: dict) -> DevToolSubscription:
    seats = []
    for email, (login, plan, profile, idle) in USER_SEATS.items():
        user = users.get(email)
        if user is None:
            continue
        seats.append({
            "login": login, "name": user.get_full_name(), "email": email, "plan": plan,
            "profile": profile, "idle_days": idle, "assigned_days_ago": 200,
        })
    for login, name, team, plan, profile, idle in DEVELOPERS:
        seats.append({
            "login": login, "name": name, "github_team": team, "plan": plan, "profile": profile,
            "idle_days": idle, "assigned_days_ago": 30 if login.startswith("contractor") else 160,
            "never_used": login.startswith("contractor"),
        })

    owner = next(iter(users.values()), None)
    subscription, _ = DevToolSubscription.objects.update_or_create(
        organization=organization,
        name=SUBSCRIPTION_NAME,
        defaults={
            "platform": DevToolPlatform.GITHUB_COPILOT,
            "adapter_key": "mock_github_copilot",
            "account": "northwind-financial",
            "credential_ref": "GITHUB_COPILOT_TOKEN",
            "options": {"seats": seats},
            "owner": owner,
            "status": LifecycleStatus.ACTIVE,
        },
    )
    # Four months back, so the trend and three-month seat history are full.
    DevToolSyncService.sync(subscription, days=125)
    return subscription


def _calibrated(budget: Budget, factor: str) -> Budget:
    """Set the limit relative to the forecast, so the demo shows each health state."""
    state = BudgetService.status_for(budget)
    basis = state["forecast"] if state["forecast"] else state["spend"]
    amount = max(Decimal("1.00"), (basis * Decimal(factor)).quantize(Decimal("1.00")))
    if budget.amount != amount:
        budget.amount = amount
        budget.save(update_fields=["amount", "updated_at"])
    return budget


def seed_scoped_budgets(organization, teams: dict, users: dict) -> list[Budget]:
    owner = users.get("ava.mitchell@northwind.example")
    specs = [
        # name, scope, target, source, calibration factor, extra
        ("Claims department AI", BudgetScope.DEPARTMENT, "Claims", CostSource.RUNTIME, "1.25", {}),
        ("Technology department AI", BudgetScope.DEPARTMENT, "Technology", CostSource.RUNTIME, "0.85", {}),
        ("Engineering Copilot", BudgetScope.ORGANIZATION, None, CostSource.DEVELOPER_TOOLS, "0.92", {}),
        ("AI Platform team, all AI", BudgetScope.TEAM, "AI Platform", CostSource.ALL, "1.4",
         {"notify_team_lead": True}),
        ("Priya Raman, personal AI", BudgetScope.USER, "priya.raman@northwind.example", CostSource.ALL, "1.6", {}),
    ]
    budgets = []
    for name, scope, target, source, factor, extra in specs:
        defaults = {
            "scope": scope, "cost_source": source, "amount": Decimal("100.00"), "currency": "USD",
            "period": BudgetPeriod.MONTHLY, "owner": owner, "alert_thresholds": [50, 75, 90, 100],
            "forecast_alerts": True, "status": LifecycleStatus.ACTIVE,
            "department": "", "team": None, "user": None, "application": None, "agent": None,
            "provider": None, "model": None, **extra,
        }
        if scope == BudgetScope.DEPARTMENT:
            defaults["department"] = target
        elif scope == BudgetScope.TEAM:
            defaults["team"] = teams.get(target)
        elif scope == BudgetScope.USER:
            defaults["user"] = users.get(target)
        budget, _ = Budget.objects.update_or_create(organization=organization, name=name, defaults=defaults)
        budgets.append(_calibrated(budget, factor))

    finance = users.get("tom.becker@northwind.example")
    if finance is not None:
        budgets[2].extra_recipients.add(finance)
    BudgetService.evaluate_all(organization)
    return budgets


def seed_alert_rules(organization, agents: dict, users: dict) -> list[CostAlertRule]:
    owner = users.get("daniel.okafor@northwind.example")
    claims = agents.get("Claims Triage Agent")
    specs = [
        {
            "name": "Daily spend spike", "metric": AlertMetric.SPEND_INCREASE, "window_hours": AlertWindow.DAY,
            "threshold": Decimal("50"), "minimum_spend": Decimal("0.50"), "severity": AlertSeverity.WARNING,
            "description": "Today's spend is half as much again as the average of the previous four days.",
        },
        {
            "name": "Weekly failure spend", "metric": AlertMetric.FAILED_SPEND, "window_hours": AlertWindow.WEEK,
            "threshold": Decimal("0.25"), "severity": AlertSeverity.WARNING,
            "description": "Money spent on runs that failed or timed out.",
        },
        {
            "name": "Runaway hour", "metric": AlertMetric.SPEND, "window_hours": AlertWindow.HOUR,
            "threshold": Decimal("5.00"), "severity": AlertSeverity.CRITICAL, "cooldown_hours": 2,
            "description": "Catches a looping agent within the hour.",
        },
    ]
    if claims is not None:
        specs.append({
            "name": "Claims triage cost per run", "metric": AlertMetric.COST_PER_RUN,
            "window_hours": AlertWindow.WEEK, "threshold": Decimal("0.02"), "severity": AlertSeverity.INFO,
            "scope": BudgetScope.AGENT, "agent": claims,
            "description": "A prompt or model change that makes every claim dearer to triage.",
        })

    rules = []
    for spec in specs:
        spec = dict(spec)
        name = spec.pop("name")
        defaults = {"scope": BudgetScope.ORGANIZATION, "owner": owner, "status": LifecycleStatus.ACTIVE, **spec}
        rule, _ = CostAlertRule.objects.update_or_create(organization=organization, name=name, defaults=defaults)
        rules.append(rule)
    CostAlertService.evaluate_all(organization)
    return rules


def seed_finops(organization, teams: dict, agents: dict, users: dict) -> dict:
    attributed = attribute_executions(organization, teams, users)
    subscription = seed_copilot(organization, users)
    budgets = seed_scoped_budgets(organization, teams, users)
    rules = seed_alert_rules(organization, agents, users)
    analysis = OptimizationService(organization).run()
    return {
        "attributed": attributed,
        "seats": subscription.seats.filter(is_active=True).count(),
        "budgets": len(budgets),
        "rules": len(rules),
        "recommendations": analysis["found"],
    }
