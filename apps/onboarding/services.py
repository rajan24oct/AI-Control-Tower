"""Where the organization stands on each onboarding step, and what to do next.

The steps follow the order records depend on each other -- a model needs its
provider, an agent its model, an application its agents -- and end with the
IRGF steps that clear a system for production.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.urls import reverse

from apps.a2a.models import A2AAgentCard, A2AContract, A2ASkill, SkillStatus
from apps.accounts.roles import Perm
from apps.agents.models import AIAgent
from apps.applications_registry.models import AIApplication
from apps.approvals.models import ApprovalRule
from apps.classification.models import ClassificationStatus, RiskClassification
from apps.core.constants import Environment, LifecycleStatus
from apps.costs.models import Budget, BudgetEnforcement
from apps.lifecycle.models import AuthorizationStatus, DeploymentAuthorization
from apps.mcp_gateway.models import MCPServer, MCPTool, ToolStatus
from apps.models_registry.models import AIModel
from apps.policies.models import Policy, PolicyEffect, PolicyType
from apps.providers.models import AIProvider
from apps.risks.models import OPEN_STATUSES, Risk
from apps.routing.models import RoutingPolicy

DONE, ATTENTION, TODO, OPTIONAL = "done", "attention", "todo", "optional"


@dataclass
class Step:
    title: str
    detail: str
    state: str
    facts: list[tuple[str, str]] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)


#: Everything the web UI onboards: (permission, label, URL name, icon, hint).
CREATE_LINKS = (
    (Perm.PROVIDER_MANAGE, "Platform provider", "inventory:provider_create", "bi-hdd-network", "Where models run"),
    (Perm.MODEL_MANAGE, "Model", "inventory:model_create", "bi-cpu", "With its prices"),
    (Perm.AGENT_MANAGE, "Agent", "inventory:agent_create", "bi-robot", "And its Agent Card"),
    (Perm.APPLICATION_MANAGE, "Application", "inventory:application_create", "bi-window-stack", "Made of agents"),
    (Perm.PROVIDER_MANAGE, "Routing policy", "routing:create", "bi-signpost-split", "AI Gateway"),
    (Perm.MCP_MANAGE, "MCP server", "mcp_gateway:server_create", "bi-hdd-stack", "MCP Gateway"),
    (Perm.A2A_MANAGE, "A2A agent", "a2a:card_create", "bi-diagram-2", "A2A Gateway"),
    (Perm.POLICY_MANAGE, "Control policy", "policies:create", "bi-file-earmark-ruled", "Both gateways enforce it"),
    (Perm.POLICY_MANAGE, "Approval rule", "approvals:rule_create", "bi-person-check", "When a person decides"),
    (Perm.RISK_MANAGE, "Risk", "risks:create", "bi-clipboard-data", "In the risk register"),
    (Perm.COST_MANAGE, "Budget", "costs:budget_create", "bi-wallet2", "Alerts and hard stops"),
)


def create_links(allowed) -> list[dict]:
    return [
        {"label": label, "url": reverse(name), "icon": icon, "hint": hint}
        for permission, label, name, icon, hint in CREATE_LINKS
        if allowed(permission)
    ]


def _action(allowed, permission, label, url_name) -> list[dict]:
    return [{"label": label, "url": reverse(url_name), "icon": "bi-plus-lg"}] if allowed(permission) else []


def _link(label, url_name, suffix="") -> dict:
    return {"label": label, "url": reverse(url_name) + suffix}


def _count(number: int, noun: str, plural: str = "") -> str:
    return f"{number} {noun if number == 1 else (plural or noun + 's')}"


def checklist(organization, allowed) -> list[Step]:
    """The onboarding steps for one organization, as seen by someone with `allowed` permissions."""
    return [
        _providers(organization, allowed),
        _models(organization, allowed),
        _agents(organization, allowed),
        _applications(organization, allowed),
        _routing(organization, allowed),
        _mcp(organization, allowed),
        _a2a(organization, allowed),
        _guardrails(organization, allowed),
        _budgets(organization, allowed),
        _risks(organization, allowed),
        _clearance(organization),
    ]


def _providers(organization, allowed) -> Step:
    providers = AIProvider.objects.for_organization(organization)
    active = list(providers.filter(status=LifecycleStatus.ACTIVE))
    unapproved = sum(1 for provider in active if not provider.is_approved)
    missing = sum(1 for provider in active if provider.credential_ref and not provider.has_resolvable_credential)
    facts = [(_count(providers.count(), "provider"), "secondary")]
    if unapproved:
        facts.append((f"{unapproved} active but not approved", "warning"))
    if missing:
        facts.append((_count(missing, "credential") + " not resolvable", "danger"))
    state = TODO if not active else ATTENTION if unapproved or missing else DONE
    return Step(
        "Platform providers",
        "Where your models run, reached through an adapter with a credential only the platform holds.",
        state, facts, _action(allowed, Perm.PROVIDER_MANAGE, "Onboard provider", "inventory:provider_create"),
        [_link("Providers registry", "inventory:providers")],
    )


def _models(organization, allowed) -> Step:
    models = AIModel.objects.for_organization(organization)
    total, approved = models.count(), models.filter(approved_for_use=True).count()
    facts = [(_count(total, "model"), "secondary")]
    if total:
        facts.append((f"{approved} approved for use", "success" if approved == total else "warning"))
    state = TODO if not total else ATTENTION if not approved else DONE
    return Step(
        "Models",
        "Each model version your providers serve, with the prices that drive cost, budgets and routing.",
        state, facts, _action(allowed, Perm.MODEL_MANAGE, "Onboard model", "inventory:model_create"),
        [_link("Models registry", "inventory:models")],
    )


def _agents(organization, allowed) -> Step:
    agents = AIAgent.objects.for_organization(organization).exclude(status=LifecycleStatus.RETIRED)
    total, unclassified = agents.count(), agents.filter(risk_tier__isnull=True).count()
    facts = [(_count(total, "agent"), "secondary")]
    if total:
        facts.append((f"{unclassified} not classified", "warning") if unclassified else ("All classified", "success"))
    state = TODO if not total else ATTENTION if unclassified else DONE
    return Step(
        "Agents",
        "What each agent is for, what it may reach, and the Agent Card IRGF asks for before Gate G3.",
        state, facts, _action(allowed, Perm.AGENT_MANAGE, "Onboard agent", "inventory:agent_create"),
        [_link("Agents registry", "inventory:agents")],
    )


def _applications(organization, allowed) -> Step:
    applications = AIApplication.objects.for_organization(organization).exclude(status=LifecycleStatus.RETIRED)
    total, unclassified = applications.count(), applications.filter(risk_tier__isnull=True).count()
    facts = [(_count(total, "application"), "secondary")]
    if total:
        facts.append((f"{unclassified} not classified", "warning") if unclassified else ("All classified", "success"))
    state = TODO if not total else ATTENTION if unclassified else DONE
    return Step(
        "Applications",
        "The business-facing systems, the agents they are made of, and which of those agents may call which.",
        state, facts,
        _action(allowed, Perm.APPLICATION_MANAGE, "Onboard application", "inventory:application_create"),
        [_link("Applications registry", "inventory:applications")],
    )


def _routing(organization, allowed) -> Step:
    count = RoutingPolicy.objects.for_organization(organization).filter(status=LifecycleStatus.ACTIVE).count()
    return Step(
        "Model routing",
        "Optional. Without a policy the AI Gateway ranks every active, approved model of the requested type.",
        DONE if count else OPTIONAL, [(_count(count, "active routing policy", "active routing policies"), "secondary")],
        _action(allowed, Perm.PROVIDER_MANAGE, "New routing policy", "routing:create"),
        [_link("Routing Simulator", "routing:simulator")],
    )


def _mcp(organization, allowed) -> Step:
    servers = MCPServer.objects.for_organization(organization)
    active = servers.filter(status=LifecycleStatus.ACTIVE)
    unapproved = active.filter(is_approved=False).count()
    pending = MCPTool.objects.for_organization(organization).filter(
        status=ToolStatus.PENDING_REVIEW, server__status=LifecycleStatus.ACTIVE
    ).count()
    facts = [(_count(servers.count(), "server"), "secondary")]
    if unapproved:
        facts.append((f"{unapproved} not approved", "warning"))
    if pending:
        facts.append((_count(pending, "tool") + " awaiting review", "warning"))
    state = OPTIONAL if not active.exists() else ATTENTION if unapproved or pending else DONE
    return Step(
        "MCP servers",
        "Every tool server behind one governed endpoint: approved, its tools reviewed, agents granted access.",
        state, facts, _action(allowed, Perm.MCP_MANAGE, "Register MCP server", "mcp_gateway:server_create"),
        [_link("Server registry", "mcp_gateway:servers"), _link("Tool catalog", "mcp_gateway:tools")],
    )


def _a2a(organization, allowed) -> Step:
    cards = A2AAgentCard.objects.for_organization(organization)
    active = cards.filter(status=LifecycleStatus.ACTIVE)
    unapproved = active.filter(is_approved=False).count()
    pending = A2ASkill.objects.for_organization(organization).filter(
        status=SkillStatus.PENDING_REVIEW, card__status=LifecycleStatus.ACTIVE
    ).count()
    contracts = A2AContract.objects.for_organization(organization).count()
    facts = [(_count(cards.count(), "agent") + " published", "secondary"),
             (_count(contracts, "contract"), "secondary")]
    if unapproved:
        facts.append((f"{unapproved} not approved", "warning"))
    if pending:
        facts.append((_count(pending, "skill") + " awaiting review", "warning"))
    state = OPTIONAL if not active.exists() else ATTENTION if unapproved or pending else DONE
    return Step(
        "Agent-to-agent (A2A)",
        "The agents that may hand each other work: published and approved, their skills reviewed, "
        "and a contract for each pair that talks.",
        state, facts, _action(allowed, Perm.A2A_MANAGE, "Publish an A2A agent", "a2a:card_create"),
        [_link("Agent registry", "a2a:agents"), _link("Contracts", "a2a:contracts")],
    )


def _guardrails(organization, allowed) -> Step:
    policies = Policy.objects.for_organization(organization).filter(status=LifecycleStatus.ACTIVE)
    rules = ApprovalRule.objects.for_organization(organization).filter(status=LifecycleStatus.ACTIVE).count()
    irgf = policies.filter(
        policy_type__in=[PolicyType.RISK_CLASSIFICATION, PolicyType.DEPLOYMENT_AUTHORIZATION],
        effect=PolicyEffect.DENY,
    ).count()
    facts = [
        (_count(policies.count(), "control policy", "control policies"), "secondary"),
        (_count(rules, "approval rule"), "secondary"),
        ("IRGF enforced at the gateways", "success") if irgf else ("IRGF not enforced yet", "secondary"),
    ]
    return Step(
        "Control policies and approval rules",
        "The decisions both gateways make on every call: deny, hold for a person, or warn.",
        DONE if policies.exists() else TODO, facts,
        _action(allowed, Perm.POLICY_MANAGE, "New control policy", "policies:create")
        + _action(allowed, Perm.POLICY_MANAGE, "New approval rule", "approvals:rule_create"),
        [_link("Policies", "policies:list"), _link("Approval rules", "approvals:centre", "#approval-rules")],
    )


def _budgets(organization, allowed) -> Step:
    budgets = Budget.objects.for_organization(organization).filter(status=LifecycleStatus.ACTIVE)
    hard = budgets.filter(enforcement=BudgetEnforcement.HARD_STOP).count()
    facts = [(_count(budgets.count(), "budget"), "secondary")]
    if hard:
        facts.append((f"{hard} with a hard stop", "secondary"))
    return Step(
        "Budgets",
        "Spend limits with alerts as they fill, and, where you choose, a hard stop at the AI Gateway.",
        DONE if budgets.exists() else TODO, facts,
        _action(allowed, Perm.COST_MANAGE, "New budget", "costs:budget_create"),
        [_link("Budgets", "costs:budgets")],
    )


def _risks(organization, allowed) -> Step:
    risks = Risk.objects.for_organization(organization)
    open_risks = risks.filter(status__in=OPEN_STATUSES)
    uncontrolled = open_risks.filter(mitigation="").count()
    facts = [(_count(open_risks.count(), "open risk"), "secondary")]
    if uncontrolled:
        facts.append((f"{uncontrolled} without a recorded control", "warning"))
    return Step(
        "Risk register",
        "Specific AI risks and the controls against them, which Gate G3 asks to see mapped from Tier 3.",
        DONE if risks.exists() else TODO, facts,
        _action(allowed, Perm.RISK_MANAGE, "Record risk", "risks:create"),
        [_link("Risk register", "risks:list"), _link("Risk matrix", "risks:matrix")],
    )


def _clearance(organization) -> Step:
    agents = AIAgent.objects.for_organization(organization).filter(status=LifecycleStatus.ACTIVE)
    applications = AIApplication.objects.for_organization(organization).filter(status=LifecycleStatus.ACTIVE)
    unclassified = (
        agents.filter(risk_tier__isnull=True).count() + applications.filter(risk_tier__isnull=True).count()
    )
    awaiting = RiskClassification.objects.for_organization(organization).filter(
        status=ClassificationStatus.PROVISIONAL
    ).count()
    authorized = DeploymentAuthorization.objects.for_organization(organization).filter(
        status=AuthorizationStatus.AUTHORIZED, environment=Environment.PRODUCTION
    )
    unauthorized = (
        agents.filter(environment=Environment.PRODUCTION)
        .exclude(pk__in=authorized.exclude(agent=None).values("agent_id")).count()
        + applications.filter(environment=Environment.PRODUCTION)
        .exclude(pk__in=authorized.exclude(application=None).values("application_id")).count()
    )
    facts = []
    if unclassified:
        facts.append((f"{unclassified} not classified", "warning"))
    if awaiting:
        facts.append((f"{awaiting} awaiting countersignature", "warning"))
    if unauthorized:
        facts.append((f"{unauthorized} in production without an authorization", "danger"))
    if not facts and (agents.exists() or applications.exists()):
        facts.append(("Every active system classified and authorized", "success"))
    state = TODO if not (agents.exists() or applications.exists()) else ATTENTION if facts[0][1] != "success" else DONE
    return Step(
        "Classify and authorize (IRGF S2 · G3)",
        "On each agent's and application's page: score its tier, have it countersigned, then request Gate G3.",
        state, facts, [],
        [_link("Risk classification", "classification:register"),
         _link("Deployment authorizations", "lifecycle:authorizations")],
    )
