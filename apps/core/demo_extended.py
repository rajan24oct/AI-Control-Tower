"""A second, deeper layer of demo data for AI FinOps and AI governance.

`seed_demo` builds the estate and a fortnight of telemetry. This adds what a
FinOps or governance review would expect to find after a quarter in service:
ninety days of spend with growth, weekends and an incident; a larger Copilot
estate; budgets across every scope, with last months' alerts already handled;
alert rules that have fired before; and more risks, approval decisions, policy
checks and evaluation runs.

Everything goes through the platform's own services -- policies are judged by
the policy engine, evaluations actually run, budgets and rules are evaluated by
their services -- so the demo shows what the platform does, not rows typed in.
Safe to run again: it replaces what it generated before.
"""

from __future__ import annotations

import hashlib
import math
import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model

from django.utils import timezone

from apps.agents.models import AIAgent
from apps.applications_registry.models import AIApplication
from apps.approvals.models import ApprovalRequest, ApprovalStatus, TriggerAction
from apps.approvals.services import ApprovalContext, ApprovalService
from apps.core.constants import DataClassification, LifecycleStatus, RiskLevel
from apps.costs.alerting import CostAlertService
from apps.costs.demo import SUBSCRIPTION_NAME
from apps.costs.devtools import DevToolSyncService, add_months, month_start
from apps.costs.models import (
    AlertMetric,
    AlertSeverity,
    AlertWindow,
    Budget,
    BudgetAlert,
    BudgetEnforcement,
    BudgetPeriod,
    BudgetScope,
    CostAlertEvent,
    CostAlertRule,
    CostSource,
    DevToolSubscription,
)
from apps.costs.optimization import OptimizationService
from apps.costs.services import BudgetService, PricingService, RollupService
from apps.executions.models import Execution, ExecutionStatusChoices
from apps.models_registry.models import AIModel
from apps.organizations.models import Team, TeamMembership
from apps.policies.models import PolicyViolation
from apps.policies.services import PolicyContext, PolicyEngine
from apps.providers.models import AIProvider
from apps.risks.models import Risk, RiskCategory, RiskStatus

User = get_user_model()
HISTORY_SOURCE = "demo_history"
HISTORY_DAYS = 90
ZERO = Decimal("0")

# -- Spend history ------------------------------------------------------------------

#: Per agent: weekday runs at the end of the period, mean input and output
#: tokens per run, and the failure rate.
AGENT_PROFILES = {
    "Customer Support Agent": (70, 9000, 700, 0.03),
    "Claims Triage Agent": (40, 14000, 1300, 0.04),
    "Fraud Detection Agent": (220, 1500, 0, 0.01),
    "Market Research Agent": (8, 32000, 3500, 0.09),
    "Regulatory Change Monitor": (9, 40000, 1800, 0.02),
    "Internal Knowledge Assistant": (110, 7000, 500, 0.02),
    "Underwriting Copilot": (18, 18000, 2200, 0.05),
    "Onboarding Document Extractor": (50, 9000, 900, 0.03),
}
#: A retry loop: the research agent ran away for three days, eleven days ago.
INCIDENT = ("Market Research Agent", range(10, 13), 6.0)
ERRORS = [
    "Upstream provider returned 429: rate limit exceeded.",
    "Tool call to the document store timed out after 30s.",
    "Model output failed schema validation after 3 retries.",
    "Context window exceeded while assembling retrieved documents.",
]


def _unit(*parts) -> float:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def seed_spend_history(organization, rng: random.Random) -> int:
    """Ninety days of runs: growing, quieter at weekends, with one incident."""
    Execution.objects.for_organization(organization).filter(source_adapter=HISTORY_SOURCE).delete()
    agents = {
        agent.name: agent
        for agent in AIAgent.objects.for_organization(organization).select_related("primary_model", "provider")
    }
    members = {}
    for membership in TeamMembership.objects.filter(team__organization=organization).select_related("user"):
        members.setdefault(membership.team_id, []).append(membership.user)
    everyone = list(User.objects.filter(organization_memberships__organization=organization))

    now = timezone.now()
    today = timezone.localdate(now)
    rows = []
    for name, (base, mean_in, mean_out, fail_rate) in AGENT_PROFILES.items():
        agent = agents.get(name)
        if agent is None or agent.primary_model is None:
            continue
        model = agent.primary_model
        pricing = PricingService.resolve(model, now)
        in_rate = pricing.input_cost_per_1k if pricing else model.input_cost_per_1k
        out_rate = pricing.output_cost_per_1k if pricing else model.output_cost_per_1k
        application = agent.applications.first()
        team_id = (application.team_id if application else None) or agent.team_id
        pool = members.get(team_id) or everyone

        for offset in range(HISTORY_DAYS, 0, -1):
            day = today - timedelta(days=offset)
            growth = 0.55 + 0.5 * (HISTORY_DAYS - offset) / HISTORY_DAYS
            weekend = 0.35 if day.weekday() >= 5 else 1.0
            surge = INCIDENT[2] if name == INCIDENT[0] and offset in INCIDENT[1] else 1.0
            runs = max(0, round(base * growth * weekend * surge * (0.8 + 0.4 * rng.random())))
            for index in range(runs):
                started = timezone.make_aware(datetime.combine(day, time(7))) + timedelta(
                    minutes=int(rng.triangular(0, 720, 300))
                )
                failed = rng.random() < (fail_rate * (3 if surge > 1 else 1))
                status = (
                    rng.choice([ExecutionStatusChoices.FAILED, ExecutionStatusChoices.TIMED_OUT])
                    if failed else ExecutionStatusChoices.COMPLETED
                )
                input_tokens = max(50, int(rng.lognormvariate(math.log(mean_in), 0.45)))
                output_tokens = int(rng.lognormvariate(math.log(mean_out), 0.5)) if mean_out else 0
                if failed:
                    output_tokens //= 4
                cost = (Decimal(input_tokens) / 1000 * in_rate + Decimal(output_tokens) / 1000 * out_rate).quantize(
                    Decimal("0.000001")
                )
                latency = int(rng.uniform(600, 9000) * (3 if status == ExecutionStatusChoices.TIMED_OUT else 1))
                external_id = f"hist-{agent.agent_id}-{day:%Y%m%d}-{index:04d}"
                rows.append(Execution(
                    organization=organization,
                    external_id=external_id,
                    agent=agent,
                    application=application,
                    provider=agent.provider,
                    model=model,
                    framework=agent.framework,
                    user=pool[int(_unit(external_id) * len(pool))],
                    source_adapter=HISTORY_SOURCE,
                    started_at=started,
                    ended_at=started + timedelta(milliseconds=latency),
                    latency_ms=latency,
                    status=status,
                    environment=agent.environment,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                    cost=cost,
                    cost_is_derived=True,
                    error_message=rng.choice(ERRORS) if failed else "",
                ))
    # bulk_create skips save signals: the Control Plane verifies actions, and
    # history imported after the fact has nothing to verify.
    Execution.objects.bulk_create(rows, batch_size=1000)
    RollupService.rebuild(organization, days=HISTORY_DAYS + 1)
    return len(rows)


# -- GitHub Copilot ---------------------------------------------------------------------

FIRST = ["Aisha", "Ben", "Carlos", "Dana", "Elif", "Finn", "Grace", "Hugo", "Ivy", "Jonas", "Kira", "Liam",
         "Maya", "Noah", "Olu", "Pia", "Quinn", "Rosa", "Sam", "Tara", "Umar", "Vera", "Wes", "Ximena",
         "Yusuf", "Zoe", "Arno", "Bea", "Cyrus", "Dilan", "Ella", "Felix", "Gita", "Hana"]
LAST = ["Adeyemi", "Brandt", "Costa", "Dahl", "Eriksen", "Fontaine", "Gallo", "Haddad", "Iqbal", "Jansen",
        "Kowalski", "Larsen", "Moreau", "Nilsson", "Ortiz", "Pereira", "Quinn", "Rossi", "Sato", "Tanaka",
        "Ueda", "Varga", "Weber", "Xu", "Yilmaz", "Zimmer", "Abbott", "Bauer", "Chen", "Duarte", "Ekström",
        "Ferreira", "Gupta", "Holm"]
#: Where the extra developers sit, and how heavily each team uses Copilot.
TEAM_MIX = [("ai-platform", 12), ("data-science", 9), ("customer-experience", 8), ("risk-compliance", 5)]
PROFILE_WEIGHTS = [("heavy", 0.18), ("regular", 0.45), ("light", 0.25), ("idle", 0.12)]


def seed_more_copilot(organization, rng: random.Random) -> int:
    subscription = DevToolSubscription.objects.for_organization(organization).filter(name=SUBSCRIPTION_NAME).first()
    if subscription is None:
        return 0
    seats = [seat for seat in (subscription.options or {}).get("seats", []) if not seat.get("extra")]
    index = 0
    for slug, count in TEAM_MIX:
        for _ in range(count):
            first, last = FIRST[index % len(FIRST)], LAST[(index * 7) % len(LAST)]
            profile = rng.choices([p for p, _ in PROFILE_WEIGHTS], [w for _, w in PROFILE_WEIGHTS])[0]
            seats.append({
                "login": f"{first.lower()}-{last.lower()}-nw"[:39],
                "name": f"{first} {last}",
                "github_team": slug,
                "plan": "enterprise" if rng.random() < 0.3 else "business",
                "profile": profile,
                "idle_days": rng.randint(31, 80) if profile == "idle" else rng.randint(0, 6),
                "assigned_days_ago": rng.randint(40, 400),
                "extra": True,
            })
            index += 1
    subscription.options = {**(subscription.options or {}), "seats": seats}
    subscription.save(update_fields=["options", "updated_at"])
    DevToolSyncService.sync(subscription, days=125)
    return index


# -- Budgets and alerts -----------------------------------------------------------------

#: (name, scope, target, cost source, period, enforcement, calibration factor).
#: The factor sets the limit against the forecast, so the demo shows every health state.
EXTRA_BUDGETS = [
    ("Customer Support Portal", BudgetScope.APPLICATION, "Customer Support Portal", CostSource.RUNTIME,
     BudgetPeriod.MONTHLY, BudgetEnforcement.ADVISORY, "1.15"),
    ("GPT-4o usage", BudgetScope.MODEL, "GPT-4o", CostSource.RUNTIME, BudgetPeriod.MONTHLY,
     BudgetEnforcement.ADVISORY, "0.95"),
    ("OpenAI Platform", BudgetScope.PROVIDER, "OpenAI Platform", CostSource.RUNTIME, BudgetPeriod.MONTHLY,
     BudgetEnforcement.ADVISORY, "1.5"),
    ("Market Research weekly cap", BudgetScope.AGENT, "Market Research Agent", CostSource.RUNTIME,
     BudgetPeriod.WEEKLY, BudgetEnforcement.HARD_STOP, "1.1"),
    ("Fraud Operations quarterly", BudgetScope.APPLICATION, "Fraud Operations Console", CostSource.RUNTIME,
     BudgetPeriod.QUARTERLY, BudgetEnforcement.ADVISORY, "1.3"),
    ("Operations department AI", BudgetScope.DEPARTMENT, "Operations", CostSource.RUNTIME,
     BudgetPeriod.MONTHLY, BudgetEnforcement.ADVISORY, "1.05"),
    ("Financial Crime department AI", BudgetScope.DEPARTMENT, "Financial Crime", CostSource.RUNTIME,
     BudgetPeriod.MONTHLY, BudgetEnforcement.ADVISORY, "1.4"),
    ("Strategy department AI", BudgetScope.DEPARTMENT, "Strategy", CostSource.RUNTIME, BudgetPeriod.MONTHLY,
     BudgetEnforcement.ADVISORY, "s0.95"),
    ("Data Science Copilot", BudgetScope.TEAM, "Data Science", CostSource.DEVELOPER_TOOLS, BudgetPeriod.MONTHLY,
     BudgetEnforcement.ADVISORY, "1.2"),
    ("Customer Experience Copilot", BudgetScope.TEAM, "Customer Experience", CostSource.DEVELOPER_TOOLS,
     BudgetPeriod.MONTHLY, BudgetEnforcement.ADVISORY, "0.97"),
    ("Annual AI programme", BudgetScope.ORGANIZATION, None, CostSource.ALL, BudgetPeriod.ANNUAL,
     BudgetEnforcement.ADVISORY, "1.25"),
    ("Marcus Lee, personal AI", BudgetScope.USER, "marcus.lee@northwind.example", CostSource.ALL,
     BudgetPeriod.MONTHLY, BudgetEnforcement.ADVISORY, "1.3"),
]
#: Limits for the budgets seed_demo created, re-set against the larger spend.
RECALIBRATE = {
    "Organization AI Spend": "1.2", "Customer Experience Team": "s0.9", "Claims Processing Platform": "1.1",
    "Fraud Detection Agent": "1.6", "Azure OpenAI": "0.98", "Claims department AI": "1.35",
    "Technology department AI": "0.94", "Engineering Copilot": "1.08", "AI Platform team, all AI": "1.3",
    "Priya Raman, personal AI": "1.5",
}


def _forecast_limit(budget: Budget, factor: str, on: date) -> Decimal:
    """The limit: a factor of the forecast, or with an "s" prefix, of spend so far
    -- the way to show a budget genuinely over early in a period."""
    state = BudgetService.status_for(budget, on)
    if factor.startswith("s"):
        basis, factor = state["spend"], factor[1:]
    else:
        basis = state["forecast"] or state["spend"]
    return max(Decimal("5.00"), (basis * Decimal(factor)).quantize(Decimal("1.00")))


def seed_more_budgets(organization, users: dict) -> tuple[int, int]:
    """Budgets across every scope, last months' alerts already acknowledged."""
    today = timezone.localdate()
    owners = [u for u in users.values() if u.email in (
        "ava.mitchell@northwind.example", "daniel.okafor@northwind.example", "chen.wei@northwind.example")]
    teams = {team.name: team for team in Team.objects.filter(organization=organization)}
    targets = {
        BudgetScope.APPLICATION: lambda n: AIApplication.objects.for_organization(organization).filter(name=n).first(),
        BudgetScope.MODEL: lambda n: AIModel.objects.for_organization(organization).filter(model_name=n).first(),
        BudgetScope.PROVIDER: lambda n: AIProvider.objects.for_organization(organization).filter(name=n).first(),
        BudgetScope.AGENT: lambda n: AIAgent.objects.for_organization(organization).filter(name=n).first(),
        BudgetScope.TEAM: lambda n: teams.get(n),
        BudgetScope.USER: lambda n: users.get(n),
    }
    field = {BudgetScope.APPLICATION: "application", BudgetScope.MODEL: "model", BudgetScope.PROVIDER: "provider",
             BudgetScope.AGENT: "agent", BudgetScope.TEAM: "team", BudgetScope.USER: "user"}

    factors = dict(RECALIBRATE)
    for index, (name, scope, target, source, period, enforcement, factor) in enumerate(EXTRA_BUDGETS):
        defaults = {
            "scope": scope, "cost_source": source, "period": period, "enforcement": enforcement,
            "amount": Decimal("100.00"), "currency": "USD", "owner": owners[index % len(owners)] if owners else None,
            "alert_thresholds": [50, 75, 90, 100], "forecast_alerts": True, "status": LifecycleStatus.ACTIVE,
            "notify_owner": True, "department": "", "team": None, "user": None, "application": None,
            "agent": None, "provider": None, "model": None,
        }
        if scope == BudgetScope.DEPARTMENT:
            defaults["department"] = target
        elif scope in field:
            defaults[field[scope]] = targets[scope](target)
        if scope == BudgetScope.TEAM:
            defaults["notify_team_lead"] = True
        Budget.objects.update_or_create(organization=organization, name=name, defaults=defaults)
        factors[name] = factor

    budgets = list(Budget.objects.for_organization(organization).filter(name__in=factors))
    for budget in budgets:
        budget.amount = _forecast_limit(budget, factors[budget.name], today)
        budget.save(update_fields=["amount", "updated_at"])

    # Alerts raised against the old, tiny limits no longer describe anything.
    BudgetAlert.objects.filter(budget__in=budgets).delete()

    # Last two months, as they would have played out: alerts raised and, since
    # the period is over, acknowledged by the budget's owner. No notifications
    # for history -- nobody needs telling about August now.
    history = 0
    with mock.patch("apps.notifications.emitters.budget_alert"):
        for budget in budgets:
            if budget.period != BudgetPeriod.MONTHLY:
                continue
            for back in (2, 1):
                start = add_months(month_start(today), -back)
                end = add_months(start, 1)
                for check in (start + timedelta(days=9), start + timedelta(days=19), end - timedelta(days=1)):
                    for alert in BudgetService.evaluate(budget, check):
                        alert.created_at = timezone.make_aware(datetime.combine(check, time(9, 15)))
                        alert.acknowledged_at = alert.created_at + timedelta(hours=5)
                        alert.acknowledged_by = budget.owner
                        alert.save(update_fields=["created_at", "acknowledged_at", "acknowledged_by"])
                        history += 1
    current = len(BudgetService.evaluate_all(organization))
    return len(EXTRA_BUDGETS), history + current


#: (name, scope, target, metric, window, severity, description, threshold or None to calibrate)
EXTRA_RULES = [
    ("Research agent runaway hour", BudgetScope.AGENT, "Market Research Agent", AlertMetric.SPEND,
     AlertWindow.HOUR, AlertSeverity.CRITICAL, "A looping research job, caught within the hour.", None),
    ("Daily token surge", BudgetScope.ORGANIZATION, None, AlertMetric.TOKENS, AlertWindow.DAY,
     AlertSeverity.WARNING, "Token use well above the usual day.", None),
    ("Claims department weekly spend", BudgetScope.DEPARTMENT, "Claims", AlertMetric.SPEND, AlertWindow.WEEK,
     AlertSeverity.WARNING, "Claims spend for the week, ahead of the monthly budget.", None),
    ("Support cost per conversation", BudgetScope.AGENT, "Customer Support Agent", AlertMetric.COST_PER_RUN,
     AlertWindow.DAY, AlertSeverity.INFO, "A prompt or model change that made every conversation dearer.", None),
    ("Underwriting failure spend", BudgetScope.AGENT, "Underwriting Copilot", AlertMetric.FAILED_SPEND,
     AlertWindow.WEEK, AlertSeverity.WARNING, "Money spent on underwriting runs that failed.", None),
    ("Strategy spend spike", BudgetScope.DEPARTMENT, "Strategy", AlertMetric.SPEND_INCREASE, AlertWindow.DAY,
     AlertSeverity.WARNING, "Strategy spend half as much again as the usual day.", Decimal("50")),
]


def seed_more_rules(organization, users: dict) -> tuple[int, int]:
    """More rules, calibrated against the data, replayed over the last month."""
    owner = users.get("daniel.okafor@northwind.example")
    recipient = users.get("tom.becker@northwind.example")
    agents = {agent.name: agent for agent in AIAgent.objects.for_organization(organization)}
    now = timezone.now()
    rules = []
    for name, scope, target, metric, window, severity, description, threshold in EXTRA_RULES:
        defaults = {
            "scope": scope, "metric": metric, "window_hours": window, "severity": severity,
            "description": description, "owner": owner, "status": LifecycleStatus.ACTIVE,
            "department": target if scope == BudgetScope.DEPARTMENT else "",
            "agent": agents.get(target) if scope == BudgetScope.AGENT else None,
            "threshold": threshold or Decimal("1"), "minimum_spend": Decimal("1.00"),
            "cooldown_hours": 24,
        }
        rule, _ = CostAlertRule.objects.update_or_create(organization=organization, name=name, defaults=defaults)
        if recipient:
            rule.recipients.add(recipient)
        if threshold is None:
            # Fire on the worst few days of the month, not every day or never.
            samples = sorted(
                v for v in (
                    CostAlertService.measure(rule, now - timedelta(days=d, hours=6)).value for d in range(30)
                ) if v is not None
            )
            if samples:
                rule.threshold = Decimal(samples[int(len(samples) * 0.85)]).quantize(Decimal("0.0001"))
                rule.save(update_fields=["threshold", "updated_at"])
        rules.append(rule)

    # Replay the month for every rule, oldest first, so cool-downs behave.
    all_rules = list(CostAlertRule.objects.for_organization(organization).filter(status=LifecycleStatus.ACTIVE))
    CostAlertEvent.objects.filter(rule__in=all_rules).delete()
    CostAlertRule.objects.filter(pk__in=[r.pk for r in all_rules]).update(last_triggered_at=None)
    replayed = 0
    with mock.patch("apps.notifications.emitters.cost_alert"):
        for days_back in range(30, 0, -1):
            moment = now - timedelta(days=days_back, hours=4)
            for rule in all_rules:
                event = CostAlertService.evaluate(rule, moment)
                if event is not None:
                    CostAlertEvent.objects.filter(pk=event.pk).update(
                        created_at=moment,
                        acknowledged_at=moment + timedelta(hours=2) if days_back > 2 else None,
                        acknowledged_by=owner if days_back > 2 else None,
                    )
                    replayed += 1
    replayed += len(CostAlertService.evaluate_all(organization))
    return len(rules), replayed


# -- Governance ----------------------------------------------------------------------------

#: (title, category, agent, likelihood, impact, mitigation, residual L, residual I, status, review in days)
EXTRA_RISKS = [
    ("Model drift degrading fraud recall", RiskCategory.MODEL_FAILURE, "Fraud Detection Agent", 3, 5,
     "Weekly recall check against the labelled sample; retrain trigger at 2 points drop.", 2, 4,
     RiskStatus.MONITORING, 14),
    ("Over-reliance on the copilot in underwriting decisions", RiskCategory.ETHICS, "Underwriting Copilot", 3, 4,
     "Underwriters record their own rationale before seeing the suggestion.", 2, 3, RiskStatus.MITIGATING, 30),
    ("Unredacted claim photos sent to a third-party model", RiskCategory.PRIVACY, "Claims Triage Agent", 2, 5,
     "", None, None, RiskStatus.IDENTIFIED, -6),
    ("Knowledge assistant citing superseded HR policies", RiskCategory.HALLUCINATION,
     "Internal Knowledge Assistant", 3, 3, "Documents carry an expiry date; expired ones are excluded.", 2, 2,
     RiskStatus.MITIGATING, 60),
    ("Regulatory feed outage missing a rule change", RiskCategory.AVAILABILITY, "Regulatory Change Monitor", 2, 4,
     "Daily heartbeat on each feed; legal subscribes to the regulator's mailing list as backup.", 1, 4,
     RiskStatus.MONITORING, 90),
    ("Prompt injection via scraped market websites", RiskCategory.SECURITY, "Market Research Agent", 4, 3,
     "Fetched pages are sanitised and tool use is disabled while summarising them.", 2, 3,
     RiskStatus.MITIGATING, 21),
    ("Copilot suggesting licensed code into products", RiskCategory.COMPLIANCE, None, 2, 4,
     "Public-code filter enforced at organization level; duplicate-detection report reviewed monthly.", 1, 3,
     RiskStatus.MONITORING, 45),
    ("Spend growth outpacing the AI programme budget", RiskCategory.COST, None, 4, 3,
     "Department budgets with forecast alerts; monthly optimization review.", 3, 2, RiskStatus.MITIGATING, 7),
    ("Extracted onboarding data written without validation", RiskCategory.OPERATIONAL,
     "Onboarding Document Extractor", 3, 4, "", None, None, RiskStatus.ASSESSED, -2),
    ("Customer tone complaints after model upgrade", RiskCategory.REPUTATIONAL, "Customer Support Agent", 2, 3,
     "Tone evaluation suite added to the model approval baseline.", 1, 3, RiskStatus.CLOSED, 120),
    ("Bedrock region outage taking down claims triage", RiskCategory.PROVIDER_DEPENDENCY, "Claims Triage Agent",
     2, 4, "Fallback route to Azure OpenAI configured and tested quarterly.", 1, 3, RiskStatus.MONITORING, 35),
    ("Fraud model unexplainable to the regulator", RiskCategory.COMPLIANCE, "Fraud Detection Agent", 3, 5,
     "Feature attributions logged per decision; model card refreshed each release.", 2, 4,
     RiskStatus.MITIGATING, 10),
]


def seed_more_risks(organization, users: dict) -> int:
    agents = {agent.name: agent for agent in AIAgent.objects.for_organization(organization)}
    owners = list(users.values())
    today = timezone.localdate()
    for index, (title, category, agent_name, lk, im, mitigation, rl, ri, status, review) in enumerate(EXTRA_RISKS):
        agent = agents.get(agent_name) if agent_name else None
        Risk.objects.update_or_create(
            organization=organization, title=title,
            defaults={
                "description": f"{title}. Raised in the quarterly AI risk review.",
                "risk_category": category, "agent": agent,
                "application": agent.applications.first() if agent else None,
                "likelihood": lk, "impact": im, "mitigation": mitigation,
                "residual_likelihood": rl, "residual_impact": ri,
                "owner": owners[(index + 3) % len(owners)] if owners else None,
                "review_date": today + timedelta(days=review), "status": status,
            },
        )
    return len(EXTRA_RISKS)


#: (action, reason, risk level, status, agent, trigger action, estimated cost)
EXTRA_APPROVALS = [
    ("Reverse a duplicate premium charge of $1,180", "Financial transaction requested.", RiskLevel.HIGH,
     ApprovalStatus.PENDING, "Customer Support Agent", TriggerAction.FINANCIAL_TRANSACTION, "0.40"),
    ("Export 3,400 flagged transactions to the case system", "Data export of restricted data.", RiskLevel.HIGH,
     ApprovalStatus.PENDING, "Fraud Detection Agent", TriggerAction.DATA_EXPORT, "0.10"),
    ("Run the quarterly market sizing across 220 sources", "High estimated cost for a single run.",
     RiskLevel.MODERATE, ApprovalStatus.PENDING, "Market Research Agent", "", "42.00"),
    ("Amend policyholder address from extracted ID document", "Account modification requested.",
     RiskLevel.MODERATE, ApprovalStatus.APPROVED, "Onboarding Document Extractor",
     TriggerAction.ACCOUNT_MODIFICATION, "0.05"),
    ("Notify 14 brokers of a wording change", "External communication attempted by an agent.",
     RiskLevel.MODERATE, ApprovalStatus.APPROVED, "Regulatory Change Monitor",
     TriggerAction.EXTERNAL_COMMUNICATION, "0.20"),
    ("Execute a pricing simulation script", "Code execution requested.", RiskLevel.HIGH,
     ApprovalStatus.APPROVED, "Underwriting Copilot", TriggerAction.CODE_EXECUTION, "1.10"),
    ("Close claim 77-20311 as fraudulent", "Account modification on a restricted record.", RiskLevel.CRITICAL,
     ApprovalStatus.REJECTED, "Claims Triage Agent", TriggerAction.ACCOUNT_MODIFICATION, "0.08"),
    ("Query production policy ledger for all EU customers", "Production database access with restricted data.",
     RiskLevel.HIGH, ApprovalStatus.REJECTED, "Internal Knowledge Assistant",
     TriggerAction.PRODUCTION_DB_ACCESS, "0.30"),
    ("Send a personalised retention offer to 900 customers", "External communication to a large audience.",
     RiskLevel.HIGH, ApprovalStatus.APPROVED, "Customer Support Agent", TriggerAction.EXTERNAL_COMMUNICATION,
     "3.60"),
    ("Unfreeze account 9921-004 after customer verification", "Agent autonomy level 4 on a critical-risk agent.",
     RiskLevel.CRITICAL, ApprovalStatus.APPROVED, "Fraud Detection Agent", TriggerAction.ACCOUNT_MODIFICATION,
     "0.02"),
]


def seed_more_approvals(organization, users: dict) -> int:
    agents = {agent.name: agent for agent in AIAgent.objects.for_organization(organization)}
    approvers = [u for u in users.values() if u.email != "nadia.faris@northwind.example"]
    count = 0
    for index, (action, reason, level, status, agent_name, trigger, cost) in enumerate(EXTRA_APPROVALS):
        if ApprovalRequest.objects.filter(organization=organization, requested_action=action).exists():
            continue
        request = ApprovalService.request(
            organization=organization, requested_action=action,
            context=ApprovalContext(agent=agents.get(agent_name), action=trigger, estimated_cost=Decimal(cost)),
            reason=reason, risk_level=level, requested_by_system="langgraph-runtime",
            assigned_approver=approvers[index % len(approvers)], expires_in_hours=72,
        )
        if status != ApprovalStatus.PENDING:
            decide = ApprovalService.approve if status == ApprovalStatus.APPROVED else ApprovalService.reject
            notes = ("Checked against the procedure; proceed." if status == ApprovalStatus.APPROVED
                     else "Needs a human-led process, not an agent.")
            # IRGF keeps a high-tier system's owners from deciding on it, so take
            # the first person the platform accepts rather than rotating blindly.
            for offset in range(1, len(approvers) + 1):
                try:
                    decide(request, user=approvers[(index + offset) % len(approvers)], notes=notes)
                    break
                except ValueError:
                    continue
        count += 1
    return count


#: (action, spec): what agents asked to do, judged by the live policies.
POLICY_ATTEMPTS = [
    ("Wire $12,000 to a new beneficiary", {"agent": "Customer Support Agent", "tool": "transfer_funds"}),
    ("Delete the dormant account 5512", {"agent": "Customer Support Agent", "tool": "delete_account"}),
    ("Summarise sanctions screening results", {"agent": "Fraud Detection Agent",
                                               "data_classification": DataClassification.RESTRICTED}),
    ("Draft a claims letter using the sandbox model", {"agent": "Claims Triage Agent",
                                                       "provider": "Gemini Evaluation Sandbox"}),
    ("Crawl 300 competitor pages", {"agent": "Market Research Agent", "estimated_cost": "25.00"}),
    ("Translate restricted underwriting notes", {"agent": "Underwriting Copilot",
                                                 "data_classification": DataClassification.RESTRICTED}),
    ("Answer an HR question with confidential payroll data", {"agent": "Internal Knowledge Assistant",
                                                              "data_classification": DataClassification.CONFIDENTIAL}),
    ("Reprocess 60,000 scanned forms", {"agent": "Onboarding Document Extractor", "estimated_cost": "31.50"}),
    ("Post the weekly digest to the public site", {"agent": "Regulatory Change Monitor", "tool": "transfer_funds"}),
    ("Close a fraud case without review", {"agent": "Fraud Detection Agent", "tool": "delete_account"}),
    ("Benchmark prompts on the evaluation sandbox", {"agent": "Market Research Agent",
                                                     "provider": "Gemini Evaluation Sandbox"}),
    ("Bulk-tag 9,000 claim documents", {"agent": "Claims Triage Agent", "estimated_cost": "19.80"}),
]


def seed_more_policy_activity(organization, rng: random.Random) -> int:
    agents = {agent.name: agent for agent in AIAgent.objects.for_organization(organization)}
    providers = {p.name: p for p in AIProvider.objects.for_organization(organization)}
    before = set(PolicyViolation.objects.for_organization(organization).values_list("pk", flat=True))
    now = timezone.now()
    for round_ in range(3):
        for action, spec in POLICY_ATTEMPTS:
            PolicyEngine.check(
                organization,
                PolicyContext(
                    agent=agents.get(spec["agent"]),
                    provider=providers.get(spec["provider"]) if spec.get("provider") else None,
                    action=action, tool=spec.get("tool", ""),
                    data_classification=spec.get("data_classification", ""),
                    estimated_cost=Decimal(spec.get("estimated_cost", "0")),
                ),
                requested_action=action,
            )
    new = PolicyViolation.objects.for_organization(organization).exclude(pk__in=before)
    # Spread over the last month, so the activity reads as a history.
    for violation in new:
        PolicyViolation.objects.filter(pk=violation.pk).update(
            created_at=now - timedelta(days=rng.randint(0, 30), hours=rng.randint(0, 20))
        )
    return new.count()


def seed_more_evaluations(organization) -> int:
    from django.core.management import call_command

    from apps.evaluations.models import EvaluationSuite, TargetKind
    from apps.evaluations.services import EvaluationService

    from apps.evaluations.models import EvaluationRun

    if not EvaluationSuite.objects.filter(organization=organization, target_kind=TargetKind.MODEL).exists():
        call_command("seed_evaluations", organization=organization.name, verbosity=0)
    runs = 0
    # Model suites: every chat model the mock provider can actually answer for.
    models = AIModel.objects.for_organization(organization).filter(provider__adapter_key="mock", model_type="chat")
    for suite in EvaluationSuite.objects.filter(organization=organization, target_kind=TargetKind.MODEL):
        for model in models:
            for _ in range(2):
                run = EvaluationService.create_run(suite, model=model, triggered_by_system="scheduled-baseline")
                EvaluationService.execute(run)
                runs += 1
    # Agent suites: again against the agent each was last run on.
    for suite in EvaluationSuite.objects.filter(organization=organization, target_kind=TargetKind.AGENT):
        last = EvaluationRun.objects.filter(suite=suite, agent__isnull=False).order_by("-created_at").first()
        if last is None:
            continue
        if not suite.cases.exists():
            continue
        for _ in range(3):
            run = EvaluationService.create_run(suite, agent=last.agent, triggered_by_system="nightly-regression")
            EvaluationService.execute(run)
            runs += 1
    return runs


def seed_extended(organization, users: dict, *, say=print) -> dict:
    rng = random.Random(20261009)
    result = {}
    say("  spend history ...")
    result["executions"] = seed_spend_history(organization, rng)
    say("  Copilot seats ...")
    result["seats"] = seed_more_copilot(organization, rng)
    say("  budgets and alerts ...")
    result["budgets"], result["budget_alerts"] = seed_more_budgets(organization, users)
    say("  alert rules ...")
    result["rules"], result["rule_events"] = seed_more_rules(organization, users)
    say("  risks, approvals, policy checks ...")
    result["risks"] = seed_more_risks(organization, users)
    result["approvals"] = seed_more_approvals(organization, users)
    result["policy_findings"] = seed_more_policy_activity(organization, rng)
    say("  use-case intake ...")
    result["use_cases"] = seed_use_cases(organization, users)
    say("  compliance scope and attestations ...")
    result["compliance"] = seed_compliance(organization, users)
    say("  evaluations ...")
    result["evaluation_runs"] = seed_more_evaluations(organization)
    say("  optimization analysis ...")
    result["recommendations"] = OptimizationService(organization).run()["found"]
    return result


# -- Use-case intake (IRGF S0-S1, Gate G1) ---------------------------------------------

#: (title, proposer, sponsor, team, scores D1 D2 D3 D4 D5data D5model, data class, sector, outcome, note, value)
USE_CASES = [
    ("Broker email triage", "marcus.lee@northwind.example", "chen.wei@northwind.example", "Customer Experience",
     (1, 1, 1, 1, 2, 1), "internal", "", "auto", "", 85000,
     "Route inbound broker emails to the right queue.", "Which queue an email belongs in."),
    ("Medical report summaries for claims", "priya.raman@northwind.example", "chen.wei@northwind.example",
     "Customer Experience", (2, 2, 2, 2, 3, 3), "confidential", "", "submitted", "", 420000,
     "Handlers spend 45 minutes reading medical reports on complex claims.",
     "Which parts of a medical report matter for this claim."),
    ("Automated credit limit increases", "daniel.okafor@northwind.example", "ava.mitchell@northwind.example",
     "Risk & Compliance", (4, 4, 3, 3, 4, 3), "restricted", "Consumer credit regulation; fair lending",
     "submitted", "", 1200000, "Customers wait days for credit limit reviews.",
     "Whether to raise a customer's credit limit, and by how much."),
    ("Customer complaint classification", "marcus.lee@northwind.example", "chen.wei@northwind.example",
     "Customer Experience", (2, 2, 2, 3, 3, 3), "confidential", "Complaint handling rules (DISP)",
     "approve_with_conditions", "Proceed once a DPIA is done and the regulatory categories are signed off.",
     260000, "Complaints are miscategorised and miss regulatory deadlines.",
     "Which regulatory category a complaint falls in."),
    ("Underwriting appetite assistant", "priya.raman@northwind.example", "ava.mitchell@northwind.example",
     "Data Science", (2, 1, 2, 2, 2, 3), "internal", "", "remediate",
     "Quantify the target: how much underwriter time, measured how? Name the non-AI alternative you rejected.",
     150000, "Underwriters search long appetite guides for edge cases.",
     "Whether a risk is within appetite."),
    ("Autonomous payment dispute refunds", "daniel.okafor@northwind.example", "chen.wei@northwind.example",
     "Customer Experience", (3, 4, 4, 3, 3, 3), "restricted", "Payment services regulation", "reject",
     "Fully autonomous, irreversible refunds to external parties: not proportionate for this use. "
     "Resubmit as a recommendation a handler approves.", 300000, "Dispute backlog of three weeks.",
     "Whether to refund a disputed payment."),
    ("Contract clause extraction", "marcus.lee@northwind.example", "sofia.hernandez@northwind.example",
     "Risk & Compliance", (2, 1, 1, 2, 3, 3), "confidential", "", "register", "", 95000,
     "Legal reviews reinsurance contracts by hand.", "Which clauses in a contract need legal review."),
    ("Call centre agent assist", "priya.raman@northwind.example", "chen.wei@northwind.example",
     "Customer Experience", (2, 2, 1, 3, 3, 3), "confidential", "", "draft", "", 510000,
     "Agents put callers on hold to look things up.", "What to tell a caller about their policy."),
]
DECIDERS = {"tier": ["tom.becker@northwind.example", "sofia.hernandez@northwind.example",
                     "ava.mitchell@northwind.example"]}


def seed_use_cases(organization, users: dict) -> int:
    from datetime import date as _date

    from apps.applications_registry.models import AIApplication
    from apps.intake.models import G1Outcome, UseCase
    from apps.intake.services import IntakeService, can_decide

    titles = [spec[0] for spec in USE_CASES]
    old = UseCase.objects.for_organization(organization).filter(title__in=titles)
    AIApplication.objects.filter(pk__in=old.exclude(application=None).values("application")).delete()
    old.delete()
    teams = {team.name: team for team in Team.objects.filter(organization=organization)}
    people = [users[email] for email in DECIDERS["tier"] if email in users]

    for (title, proposer, sponsor, team, scores, data, sector, outcome, note, value, need, decision) in USE_CASES:
        d1, d2, d3, d4, d5d, d5m = scores
        use_case = UseCase.objects.create(
            organization=organization, title=title, created_by=users.get(proposer), sponsor=users.get(sponsor),
            team=teams.get(team), need=need, current_measure="Measured in the team's monthly report",
            target_measure="Halve the time or error rate within two quarters",
            non_ai_alternative="More staff or a rules engine; neither keeps up with the volume.",
            decision_supported=decision, affected_parties="Staff who act on the output; customers indirectly.",
            intended_autonomy=d2, data_path="From the system of record, read-only, through the AI Gateway.",
            data_classification=data, sector_constraints=sector, no_commitment=True,
            expected_annual_value=value,
            d1_consequence=d1, d2_autonomy=d2, d3_reversibility=d3, d4_exposure=d4,
            d5_data_sensitivity=d5d, d5_model_uncertainty=d5m,
        )
        if outcome == "draft":
            continue
        IntakeService.submit(use_case, use_case.created_by)
        if outcome in ("auto", "submitted"):
            continue
        decider = next(person for person in people if can_decide(use_case, person)[0])
        if outcome == "register":
            IntakeService.decide(use_case, decider, G1Outcome.APPROVE)
            IntakeService.register(use_case, use_case.created_by)
            continue
        conditions = []
        if outcome == "approve_with_conditions":
            due = (_date.today() + timedelta(days=21)).isoformat()
            conditions = [
                {"text": "Complete a data protection impact assessment", "owner_id": str(use_case.created_by.pk),
                 "owner": use_case.created_by.get_full_name(), "due": due, "done": False},
                {"text": "Compliance signs off the regulatory complaint categories",
                 "owner_id": str(users["sofia.hernandez@northwind.example"].pk), "owner": "Sofia Hernandez",
                 "due": (_date.today() - timedelta(days=2)).isoformat(), "done": False},
            ]
        IntakeService.decide(use_case, decider, outcome, note, conditions)
    return len(USE_CASES)


# -- Compliance scope decisions and attestations ---------------------------------------

#: (system name, kind, EU AI Act category, rationale)
SCOPES = [
    ("Underwriting Copilot", "agent", "high_risk",
     "Supports risk assessment and pricing for life and health insurance: an Annex III use. Confirmed by Legal."),
    ("Claims Processing Platform", "application", "high_risk",
     "Claims decisions on health products affect access to essential services; treated as high-risk pending "
     "Legal's final view."),
    ("Customer Support Agent", "agent", "limited",
     "Talks to customers directly: Article 50 transparency duties apply. Not an Annex III use."),
    ("Internal Knowledge Assistant", "agent", "minimal", "Internal answers over published policies; no Annex III use."),
    ("Market Research Agent", "agent", "minimal", "Desk research on public sources for the strategy team."),
]
#: (system name, kind, requirement, statement, evidence reference, valid days)
ATTESTATIONS = [
    ("Underwriting Copilot", "agent", "EU-AIA-4",
     "All 14 underwriters using the copilot completed the AI literacy module, including its limits.",
     "LMS course AI-101 completion report, Q3", 365),
    ("Claims Processing Platform", "application", "EU-AIA-4",
     "Claims handlers briefed on how triage suggestions are produced and when to override them.",
     "Claims operations briefing pack v2", 365),
    ("Claims Processing Platform", "application", "EU-AIA-27",
     "Fundamental rights impact assessment completed with Legal and the DPO before go-live.",
     "FRIA-2026-004 (Legal SharePoint)", 365),
    ("Customer Support Agent", "agent", "EU-AIA-4",
     "Support team trained on the assistant's purpose, limits and escalation path.",
     "Training log CS-AI-07", 365),
]


def seed_compliance(organization, users: dict) -> int:
    from apps.agents.models import AIAgent as _Agent
    from apps.applications_registry.models import AIApplication as _App
    from apps.compliance.models import Attestation, RegulatoryScope

    lookup = {"agent": _Agent, "application": _App}
    decider = users.get("sofia.hernandez@northwind.example")
    count = 0
    for name, kind, category, rationale in SCOPES:
        obj = lookup[kind].objects.for_organization(organization).filter(name=name).first()
        if obj is None:
            continue
        RegulatoryScope.objects.update_or_create(
            organization=organization, regime="eu_ai_act", **{kind: obj},
            defaults={"eu_category": category, "applicability": "applies", "rationale": rationale,
                      "assessed_by": decider},
        )
        count += 1
    for name, kind, requirement, statement, reference, days in ATTESTATIONS:
        obj = lookup[kind].objects.for_organization(organization).filter(name=name).first()
        if obj is None:
            continue
        Attestation.objects.filter(organization=organization, requirement=requirement, **{kind: obj}).delete()
        Attestation.objects.create(
            organization=organization, requirement=requirement, statement=statement, evidence_reference=reference,
            attested_by=decider, valid_until=timezone.localdate() + timedelta(days=days), **{kind: obj},
        )
        count += 1
    return count
