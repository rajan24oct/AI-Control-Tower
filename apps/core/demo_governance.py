"""Governance demo data: pricing, budgets, risks and approvals (section 37).

Kept out of the seed command itself so the command stays readable, and so the
governance fixtures can be reseeded independently.
"""

from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.approvals.models import (
    DEFAULT_RULES,
    ApprovalRequest,
    ApprovalRule,
    ApprovalStatus,
    TriggerAction,
)
from apps.approvals.services import ApprovalContext, ApprovalService
from apps.core.constants import DataClassification, LifecycleStatus, RiskLevel
from apps.costs.models import (
    Budget,
    BudgetAlert,
    BudgetPeriod,
    BudgetScope,
    ModelPricing,
    PricingVersion,
)
from apps.costs.services import BudgetService
from apps.executions.models import Execution, ExecutionStatusChoices
from apps.executions.services import ExecutionService
from apps.policies.models import DEFAULT_POLICIES, Policy, PolicyViolation
from apps.policies.services import PolicyContext, PolicyEngine
from apps.risks.models import Risk, RiskCategory, RiskStatus

# (title, category, agent name or None, likelihood, impact, mitigation, residual L, residual I, status)
RISKS = [
    ("Autonomous account freezing without human review", RiskCategory.AUTONOMOUS_AGENT,
     "Fraud Detection Agent", 4, 5,
     "Daily review of frozen accounts; automatic unfreeze after 24h if unconfirmed.", 3, 4,
     RiskStatus.MITIGATING),
    ("Customer PII leaking into prompts", RiskCategory.PRIVACY,
     "Customer Support Agent", 3, 5,
     "PII redaction filter applied before prompt assembly; quarterly sampling audit.", 2, 4,
     RiskStatus.MONITORING),
    ("Hallucinated policy terms in claim responses", RiskCategory.HALLUCINATION,
     "Claims Triage Agent", 4, 4,
     "Responses grounded in retrieved policy documents; adjuster sign-off required.", 2, 4,
     RiskStatus.MITIGATING),
    ("Bias in underwriting risk recommendations", RiskCategory.BIAS,
     "Underwriting Copilot", 3, 5,
     "Quarterly fairness testing across protected characteristics.", 2, 5,
     RiskStatus.ASSESSED),
    ("Single-provider dependency on Azure OpenAI", RiskCategory.PROVIDER_DEPENDENCY,
     None, 3, 4, "Fallback models configured on the two critical agents.", 2, 4,
     RiskStatus.MITIGATING),
    ("Unapproved models reaching production", RiskCategory.COMPLIANCE,
     None, 3, 4, "", None, None, RiskStatus.IDENTIFIED),
    ("Runaway token spend from retry loops", RiskCategory.COST,
     "Market Research Agent", 3, 3,
     "Per-agent budgets with alerting at 75% and 90%.", 2, 2, RiskStatus.MONITORING),
    ("Regulatory misclassification under the EU AI Act", RiskCategory.COMPLIANCE,
     "Regulatory Change Monitor", 2, 5,
     "Legal review of the classification each quarter.", 2, 4, RiskStatus.MONITORING),
    ("Prompt injection through customer-supplied documents", RiskCategory.SECURITY,
     "Onboarding Document Extractor", 4, 4, "", None, None, RiskStatus.IDENTIFIED),
    ("Provider outage stalling claims intake", RiskCategory.AVAILABILITY,
     "Claims Triage Agent", 3, 4,
     "Health checks every five minutes; manual fallback path documented.", 2, 3,
     RiskStatus.MITIGATING),
    ("Internal documents exposed to the wrong department", RiskCategory.DATA_LEAKAGE,
     "Internal Knowledge Assistant", 2, 4,
     "Retrieval scoped by the requesting user's group membership.", 1, 4,
     RiskStatus.MONITORING),
    ("Reputational damage from an incorrect public response", RiskCategory.REPUTATIONAL,
     "Customer Support Agent", 2, 5,
     "No response is sent without agent review.", 1, 5, RiskStatus.ACCEPTED),
]

# (action, reason, risk level, status, agent name)
# The agent is named per action so the demo reads coherently -- a "freeze
# account" request must come from the fraud agent, not whichever run happened
# to be paused.
APPROVALS = [
    ("Freeze customer account 4471-882 pending fraud review",
     "Agent autonomy level 4 on a critical-risk agent.", RiskLevel.CRITICAL,
     ApprovalStatus.PENDING, "Fraud Detection Agent"),
    ("Send claim rejection letter to policyholder",
     "External communication attempted by an agent.", RiskLevel.HIGH,
     ApprovalStatus.PENDING, "Claims Triage Agent"),
    ("Issue goodwill refund of $250 to customer 88213",
     "Financial transaction requested.", RiskLevel.CRITICAL,
     ApprovalStatus.PENDING, "Customer Support Agent"),
    ("Query the production claims database for bulk export",
     "Production database access with restricted data.", RiskLevel.HIGH,
     ApprovalStatus.PENDING, "Claims Triage Agent"),
    ("Escalate underwriting decision to the pricing committee",
     "Restricted tool requested by the agent.", RiskLevel.MODERATE,
     ApprovalStatus.PENDING, "Underwriting Copilot"),
    ("Publish market briefing to the internal newsletter",
     "External communication attempted by an agent.", RiskLevel.MODERATE,
     ApprovalStatus.APPROVED, "Market Research Agent"),
    ("Update the customer record with an extracted address",
     "Account modification requested.", RiskLevel.MODERATE,
     ApprovalStatus.APPROVED, "Onboarding Document Extractor"),
    ("Run a bulk reclassification across 12,000 documents",
     "High estimated cost for a single run.", RiskLevel.MODERATE,
     ApprovalStatus.APPROVED, "Onboarding Document Extractor"),
    ("Grant the research agent access to the pricing model",
     "Unapproved model requested.", RiskLevel.HIGH,
     ApprovalStatus.REJECTED, "Market Research Agent"),
    ("Email all policyholders about the outage",
     "External communication to a large audience.", RiskLevel.CRITICAL,
     ApprovalStatus.REJECTED, "Customer Support Agent"),
]

BUDGETS = [
    ("Organization AI Spend", BudgetScope.ORGANIZATION, None, Decimal("25.00")),
    ("Customer Experience Team", BudgetScope.TEAM, "Customer Experience", Decimal("6.00")),
    ("Claims Processing Platform", BudgetScope.APPLICATION, "Claims Processing Platform", Decimal("4.00")),
    ("Fraud Detection Agent", BudgetScope.AGENT, "Fraud Detection Agent", Decimal("2.00")),
    ("Azure OpenAI", BudgetScope.PROVIDER, "Azure OpenAI (West Europe)", Decimal("12.00")),
]


def seed_pricing(organization, models: dict) -> PricingVersion:
    """A dated price list matching the registry rates, so cost resolution has
    a real version to find rather than always falling back.
    """
    version, _ = PricingVersion.objects.update_or_create(
        organization=organization,
        name="2026 Standard Rates",
        defaults={
            "description": "Vendor list prices in force for the current year.",
            "currency": "USD",
            "effective_from": timezone.now() - timedelta(days=365),
            "effective_to": None,
            "source": "Vendor public price lists",
            "status": LifecycleStatus.ACTIVE,
        },
    )

    for model in models.values():
        ModelPricing.objects.update_or_create(
            version=version,
            model=model,
            defaults={
                "input_cost_per_1k": model.input_cost_per_1k,
                "output_cost_per_1k": model.output_cost_per_1k,
                "currency": model.currency,
            },
        )
    return version


def seed_budgets(organization, teams: dict, applications: dict, agents: dict, providers: dict, users: dict):
    """Budgets calibrated against the seeded spend so alerts actually fire."""
    owner = next(iter(users.values()), None)
    created = []

    for name, scope, target_name, amount in BUDGETS:
        defaults = {
            "scope": scope,
            "amount": amount,
            "currency": "USD",
            "period": BudgetPeriod.MONTHLY,
            "owner": owner,
            "alert_thresholds": [50, 75, 90, 100],
            "status": LifecycleStatus.ACTIVE,
            "team": None,
            "application": None,
            "agent": None,
            "provider": None,
            "model": None,
        }
        if scope == BudgetScope.TEAM:
            defaults["team"] = teams.get(target_name)
        elif scope == BudgetScope.APPLICATION:
            defaults["application"] = applications.get(target_name)
        elif scope == BudgetScope.AGENT:
            defaults["agent"] = agents.get(target_name)
        elif scope == BudgetScope.PROVIDER:
            defaults["provider"] = providers.get(target_name)

        budget, _ = Budget.objects.update_or_create(
            organization=organization, name=name, defaults=defaults
        )
        created.append(budget)

    BudgetService.evaluate_all(organization)
    # Report what exists, not what this run happened to raise -- evaluation is
    # idempotent, so a reseed legitimately raises nothing new.
    alerts = BudgetAlert.objects.for_organization(organization).count()
    return created, alerts


def seed_approval_rules(organization) -> list[ApprovalRule]:
    rules = []
    for spec in DEFAULT_RULES:
        rule, _ = ApprovalRule.objects.update_or_create(
            organization=organization,
            name=spec["name"],
            defaults={
                "description": spec.get("description", ""),
                "priority": spec.get("priority", 100),
                "min_autonomy_level": spec.get("min_autonomy_level"),
                "risk_levels": spec.get("risk_levels", []),
                "environments": spec.get("environments", []),
                "data_classifications": spec.get("data_classifications", []),
                "tools": spec.get("tools", []),
                "actions": spec.get("actions", []),
                "cost_threshold": spec.get("cost_threshold"),
                "match_restricted_tool": spec.get("match_restricted_tool", False),
                "match_unapproved_provider": spec.get("match_unapproved_provider", False),
                "match_unapproved_model": spec.get("match_unapproved_model", False),
                "severity": spec.get("severity", RiskLevel.MODERATE),
                "status": LifecycleStatus.ACTIVE,
            },
        )
        rules.append(rule)
    return rules


def seed_policies(organization) -> list[Policy]:
    """The seven policy types from section 18, so the architecture is visible."""
    created = []
    for spec in DEFAULT_POLICIES:
        policy, _ = Policy.objects.update_or_create(
            organization=organization,
            name=spec["name"],
            defaults={
                "description": spec.get("description", ""),
                "policy_type": spec["policy_type"],
                "effect": spec["effect"],
                "severity": spec.get("severity", RiskLevel.MODERATE),
                "priority": spec.get("priority", 100),
                "configuration": spec.get("configuration", {}),
                "reference": spec.get("reference", ""),
                "status": LifecycleStatus.ACTIVE,
            },
        )
        created.append(policy)
    return created


def seed_policy_activity(organization, agents: dict, providers: dict) -> int:
    """Run a few realistic checks so the engine has a genuine audit trail.

    These are real evaluations, not fabricated rows -- whatever the engine
    decides is what gets recorded.
    """
    PolicyViolation.objects.for_organization(organization).delete()

    attempts = [
        ("Send marketing email to the customer list", {"tool": "transfer_funds"}),
        ("Export the full claims table", {"tool": "delete_account"}),
        (
            "Summarise restricted claim documents",
            {"agent": "Claims Triage Agent", "data_classification": DataClassification.RESTRICTED},
        ),
        ("Run an evaluation sweep on the sandbox model", {"provider": "Gemini Evaluation Sandbox"}),
        ("Bulk reprocess 40,000 documents", {"estimated_cost": Decimal("18.40")}),
    ]

    recorded = 0
    for action, spec in attempts:
        context = PolicyContext(
            agent=agents.get(spec.get("agent", "")) or agents.get("Customer Support Agent"),
            provider=providers.get(spec["provider"]) if spec.get("provider") else None,
            action=action,
            tool=spec.get("tool", ""),
            data_classification=spec.get("data_classification", ""),
            estimated_cost=spec.get("estimated_cost", Decimal("0")),
        )
        decision = PolicyEngine.check(organization, context, requested_action=action)
        recorded += len(decision.denials) + len(decision.warnings)

    return recorded


def seed_risks(organization, agents: dict, providers: dict, users: dict) -> list[Risk]:
    rng = random.Random(20260909)
    owners = list(users.values())
    today = timezone.now().date()
    created = []

    for index, (
        title, category, agent_name, likelihood, impact,
        mitigation, residual_l, residual_i, status,
    ) in enumerate(RISKS):
        agent = agents.get(agent_name) if agent_name else None
        risk, _ = Risk.objects.update_or_create(
            organization=organization,
            title=title,
            defaults={
                "description": f"{title}. Identified during the AI governance review.",
                "risk_category": category,
                "agent": agent,
                "application": agent.applications.first() if agent else None,
                "provider": providers.get("Azure OpenAI (West Europe)")
                if category == RiskCategory.PROVIDER_DEPENDENCY
                else None,
                "likelihood": likelihood,
                "impact": impact,
                "mitigation": mitigation,
                "residual_likelihood": residual_l,
                "residual_impact": residual_i,
                "owner": owners[index % len(owners)] if owners else None,
                # A couple deliberately overdue, so the attention panel has
                # something real to surface.
                "review_date": today + timedelta(days=rng.choice([-14, -3, 21, 45, 90])),
                "status": status,
            },
        )
        created.append(risk)
    return created


def seed_approvals(organization, agents: dict, users: dict) -> list[ApprovalRequest]:
    """Approval requests, with pending ones attached to genuinely paused runs."""
    approvers = [u for u in users.values()]
    created = []

    for action, reason, risk_level, status, agent_name in APPROVALS:
        existing = ApprovalRequest.objects.filter(
            organization=organization, requested_action=action
        ).first()
        if existing:
            created.append(existing)
            continue

        agent = agents.get(agent_name)
        # Attach to a genuinely paused run belonging to this agent, if there is
        # one; otherwise the request stands on its own.
        execution = None
        if status == ApprovalStatus.PENDING and agent:
            execution = (
                Execution.objects.for_organization(organization)
                .filter(
                    agent=agent,
                    status=ExecutionStatusChoices.WAITING_FOR_APPROVAL,
                    approval_requests__isnull=True,
                )
                .first()
            )

        index = len(created)

        request = ApprovalService.request(
            organization=organization,
            requested_action=action,
            context=ApprovalContext(
                agent=agent,
                execution=execution,
                action=TriggerAction.EXTERNAL_COMMUNICATION if "Send" in action or "Email" in action else "",
                estimated_cost=Decimal("0"),
            ),
            reason=reason,
            risk_level=risk_level,
            requested_by_system="langgraph-runtime",
            assigned_approver=approvers[index % len(approvers)] if approvers else None,
            expires_in_hours=48,
        )

        if status == ApprovalStatus.APPROVED:
            decider = approvers[(index + 1) % len(approvers)] if approvers else None
            ApprovalService.approve(request, user=decider, notes="Reviewed and cleared.")
            # The engine would resume and finish the run; reflect that so the
            # demo does not leave approved runs sitting in "Running" forever.
            if request.execution_id:
                ExecutionService.complete(request.execution)
        elif status == ApprovalStatus.REJECTED:
            decider = approvers[(index + 2) % len(approvers)] if approvers else None
            ApprovalService.reject(
                request, user=decider, notes="Outside the agent's approved boundaries."
            )

        request.refresh_from_db()
        created.append(request)

    return created
