"""IRGF lifecycle services: authorize at G3, govern change at G4, grant M17 exceptions.

The authorization is the hinge. Gate G3 freezes the agent's configuration as it
was approved; from then on every edit to the agent is compared with that
snapshot, classified by IRGF's change table, and either recorded (minor), sent
to Gate G4 (material), or sent to G4 with the authorization suspended until it
is decided (major). The Control Plane of Phase 3 compares running state with
the same snapshot.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Perm
from apps.audit.context import get_audit_context
from apps.audit.models import AuditAction
from apps.audit.services import AuditService
from apps.classification.services import ClassificationService
from apps.lifecycle import decisions, irgf
from apps.lifecycle.models import (
    BASELINE_STATUSES,
    OPEN_CHANGE_STATUSES,
    AuthorizationCondition,
    AuthorizationStatus,
    ChangeRecord,
    ChangeStatus,
    DeploymentAuthorization,
    ExceptionStatus,
    GovernanceException,
)


class LifecycleError(ValueError):
    """A request the IRGF rules do not allow."""


def _person(actor):
    if actor is None or getattr(actor, "is_service_account", False) or not getattr(actor, "pk", None):
        return None
    return actor


# -- Systems: an agent, or an application and its agents -------------------------


def is_application(subject) -> bool:
    from apps.applications_registry.models import AIApplication

    return isinstance(subject, AIApplication)


def subject_filter(subject) -> dict:
    """The lookup that finds a system's records, whichever kind it is."""
    return {"application": subject} if is_application(subject) else {"agent": subject}


def subject_owner(subject):
    if is_application(subject):
        return subject.business_owner or subject.technical_owner
    return subject.owner


def _audit(action, record, *, actor=None, old_value=None, new_value=None, metadata=None):
    AuditService.log(
        action=action,
        resource_type=record._meta.label,
        resource_id=record.pk,
        resource_label=str(record),
        organization=record.organization,
        actor=_person(actor),
        old_value=old_value,
        new_value=new_value,
        metadata={"irgf": irgf.GATE_LABELS.get(record.gate, record.gate), **(metadata or {})},
    )


def _notify(record, *, title, body, url, recipients=None, severity=None, dedupe_key=""):
    """Tell whoever holds the next signature, or the given people."""
    from apps.notifications import emitters
    from apps.notifications.models import NotificationCategory, NotificationSeverity

    seats = {seat.key for seat in decisions.next_seats(record)} if recipients is None else set()
    category = NotificationCategory.GATE_DECISION
    if recipients is None and irgf.OWNER.key in seats and record.subject_owner_id:
        recipients = [record.subject_owner]
    elif recipients is None and irgf.RISK_LEAD.key in seats:
        category = NotificationCategory.RISK_REVIEW
    return emitters.lifecycle_notice(
        record, category=category, title=title, body=body, url=url, recipients=recipients,
        severity=severity or NotificationSeverity.WARNING, dedupe_key=dedupe_key,
    )


# -- The authorized configuration --------------------------------------------

SNAPSHOT_FIELDS = (
    "primary_model",
    "fallback_model",
    "provider",
    "autonomy_level",
    "requires_human_approval",
    "allowed_tools",
    "restricted_tools",
    "mcp_grants",
    "a2a_contracts",
    "knowledge_sources",
    "data_classification",
    "environment",
    "system_prompt_reference",
    "action_limits",
    "kill_switch_conditions",
    "kill_switch_triggers",
    "identities",
    "risk_tier",
)

FIELD_LABELS = {
    "primary_model": "Primary model",
    "fallback_model": "Fallback model",
    "provider": "Provider",
    "autonomy_level": "Autonomy level",
    "requires_human_approval": "Human approval required",
    "allowed_tools": "Allowed tools",
    "restricted_tools": "Restricted tools",
    "mcp_grants": "MCP access grants",
    "a2a_contracts": "A2A communication contracts",
    "knowledge_sources": "Knowledge sources",
    "data_classification": "Data classification",
    "environment": "Environment",
    "system_prompt_reference": "System prompt reference",
    "action_limits": "Action limits",
    "kill_switch_conditions": "Kill-switch conditions",
    "kill_switch_triggers": "Automatic kill-switch triggers",
    "identities": "Bound identities",
    "risk_tier": "Risk tier",
    # An application and the agents it composes.
    "models": "Models",
    "providers": "Providers",
    "agents": "Member agents",
    "delegations": "Delegation graph",
    "composite": "Composite reachable authority",
    "tools": "Tools",
}

_REFERENCES = ("primary_model", "fallback_model", "provider")
_SENSITIVITY = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}

#: Fields the snapshot gained after authorizations were already being made. An
#: authorization from before a field existed never recorded it -- which means
#: "not recorded", not "empty" -- so the field is not compared until the system
#: is authorized again. Before the A2A Gateway, any agent could message any
#: other: an authorization from then never constrained who an agent talks to.
LATER_FIELDS = frozenset({"a2a_contracts"})


def recorded(configuration: dict, field: str) -> bool:
    """Whether an authorized configuration says anything about this field."""
    return field not in LATER_FIELDS or field in (configuration or {})


def _model_ref(model) -> dict | None:
    if model is None:
        return None
    return {
        "id": str(model.pk),
        "model_id": model.model_id,
        "name": model.model_name,
        "family": model.model_family,
        "provider": str(model.provider_id),
    }


def snapshot(subject) -> dict:
    """The system's configuration in the terms a Deployment Authorization records."""
    return _application_snapshot(subject) if is_application(subject) else _agent_snapshot(subject)


def _agent_snapshot(agent) -> dict:
    """An agent's configuration, as a Deployment Authorization records it."""
    grants = sorted(
        (
            {"server": grant.server.slug, "tools": sorted(grant.allowed_tools or [])}
            for grant in agent.mcp_grants.select_related("server")
        ),
        key=lambda grant: grant["server"],
    )
    # The agents it may hand work to through the A2A Gateway, and for what.
    contracts = sorted(
        (
            {"to": contract.recipient.address, "skills": sorted(contract.allowed_skills or [])}
            for contract in agent.a2a_contracts.select_related("recipient", "recipient__agent")
        ),
        key=lambda contract: contract["to"],
    )
    return {
        "primary_model": _model_ref(agent.primary_model),
        "fallback_model": _model_ref(agent.fallback_model),
        "provider": (
            {"id": str(agent.provider_id), "name": agent.provider.name} if agent.provider_id else None
        ),
        "autonomy_level": agent.autonomy_level,
        "requires_human_approval": agent.requires_human_approval,
        "allowed_tools": sorted(agent.allowed_tools or []),
        "restricted_tools": sorted(agent.restricted_tools or []),
        "mcp_grants": grants,
        "a2a_contracts": contracts,
        "knowledge_sources": sorted(agent.knowledge_sources or []),
        "data_classification": agent.data_classification,
        "environment": agent.environment,
        "system_prompt_reference": agent.system_prompt_reference or "",
        "action_limits": agent.action_limits or {},
        "kill_switch_conditions": agent.kill_switch_conditions or "",
        "kill_switch_triggers": sorted(agent.kill_switch_triggers or []),
        "identities": sorted(agent.service_accounts.values_list("name", flat=True)),
        "risk_tier": agent.risk_tier,
    }


#: What an application's authorization records (IRGF Phase 4).
APPLICATION_FIELDS = (
    "models",
    "providers",
    "agents",
    "delegations",
    "composite",
    "tools",
    "data_classification",
    "environment",
    "risk_tier",
)


def composite_authority(application) -> dict:
    """IRGF chapter 16: the authority reachable through all of a system's agents.

    D2 and D3 are scored against this, not against whichever agent happens to be
    the entry point, and widening it is a change for Gate G4.
    """
    agents = list(application.agents.all())
    tools = set()
    for agent in agents:
        tools.update(agent.allowed_tools or [])
        for grant in agent.mcp_grants.select_related("server"):
            if grant.allowed_tools:
                tools.update(f"{grant.server.slug}__{tool}" for tool in grant.allowed_tools)
            else:
                tools.add(f"{grant.server.slug}__*")
    return {
        "autonomy": max((agent.autonomy_level for agent in agents), default=0),
        "unsupervised": sorted(agent.name for agent in agents if agent.acts and not agent.approval_required),
        "tools": sorted(tools),
    }


def _application_snapshot(application) -> dict:
    agents = sorted(application.agents.all(), key=lambda agent: agent.agent_id)
    return {
        "models": sorted((_model_ref(model) for model in application.ai_models.all()), key=lambda ref: ref["id"]),
        "providers": sorted(
            ({"id": str(provider.pk), "name": provider.name} for provider in application.providers.all()),
            key=lambda ref: ref["id"],
        ),
        "agents": [{"id": str(agent.pk), "agent_id": agent.agent_id, "name": agent.name} for agent in agents],
        "delegations": sorted(
            (
                {"from": edge["from"], "to": edge["to"], "authority": edge.get("authority", "own")}
                for edge in application.delegations or []
            ),
            key=lambda edge: (edge["from"], edge["to"]),
        ),
        "composite": composite_authority(application),
        "tools": sorted(str(tool) for tool in application.tools or []),
        "data_classification": application.data_classification,
        "environment": application.environment,
        "risk_tier": application.risk_tier,
    }


def comparable(field: str, value):
    """What counts as the same: a renamed model is the same model."""
    if field in _REFERENCES:
        return value.get("id") if isinstance(value, dict) else None
    if field == "kill_switch_triggers":
        # Authorized before Phase 3 recorded triggers: none is the same as empty.
        return sorted(value or [])
    if field == "a2a_contracts":
        return sorted((item["to"], tuple(sorted(item.get("skills") or []))) for item in value or [])
    if field in ("models", "providers", "agents"):
        return sorted(item.get("id") for item in value or [])
    if field == "delegations":
        return sorted((edge["from"], edge["to"], edge.get("authority", "own")) for edge in value or [])
    if field == "composite":
        value = value or {}
        return (value.get("autonomy", 0), sorted(value.get("unsupervised") or []), sorted(value.get("tools") or []))
    if field == "tools":
        return sorted(value or [])
    return value


def display(field: str, value) -> str:
    if value in (None, "", [], {}):
        return "—"
    if field in ("primary_model", "fallback_model"):
        return value.get("name") or value.get("model_id") or "—"
    if field == "provider":
        return value.get("name") or "—"
    if field == "kill_switch_triggers":
        return ", ".join(str(item).replace("_", " ") for item in value)
    if field in ("models", "providers", "agents"):
        return ", ".join(item.get("name") or item.get("model_id") or "?" for item in value)
    if field == "delegations":
        return "; ".join(
            f"{edge['from']} → {edge['to']}" + (" (with the caller's authority)" if edge.get("authority") == "caller" else "")
            for edge in value
        )
    if field == "composite":
        tools = value.get("tools") or []
        shown = ", ".join(tools[:8]) + ("…" if len(tools) > 8 else "")
        return (
            f"Autonomy up to level {value.get('autonomy', 0)}; "
            f"acting without approval: {', '.join(value.get('unsupervised') or []) or 'none'}; "
            f"{len(tools)} tool{'s' if len(tools) != 1 else ''}: {shown or 'none'}"
        )
    if field == "mcp_grants":
        return "; ".join(
            f"{grant['server']} ({', '.join(grant['tools']) or 'all tools'})" for grant in value
        )
    if field == "a2a_contracts":
        return "; ".join(
            f"{contract['to']} ({', '.join(contract['skills']) or 'all skills'})" for contract in value
        )
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return ", ".join(f"{key} {item}" for key, item in value.items())
    return str(value)


def fields_of(configuration: dict) -> tuple:
    """The fields a configuration records: an application's, or an agent's."""
    return APPLICATION_FIELDS if "agents" in (configuration or {}) else SNAPSHOT_FIELDS


def configuration_rows(configuration: dict) -> list[tuple[str, str]]:
    return [(FIELD_LABELS[field], display(field, configuration.get(field))) for field in fields_of(configuration)]


def differences(subject, configuration: dict) -> list[str]:
    current = snapshot(subject)
    return [
        field for field in fields_of(current)
        if recorded(configuration, field)
        and comparable(field, configuration.get(field)) != comparable(field, current.get(field))
    ]


# -- G3 entry criteria ---------------------------------------------------------


@dataclass
class Check:
    key: str
    label: str
    irgf: str
    required: bool
    ok: bool
    detail: str

    @property
    def blocking(self) -> bool:
        return self.required and not self.ok


def _evaluation_run(agent, *, passing=False, max_age_days=None):
    from apps.evaluations.models import EvaluationRun, RunStatus

    target = Q(agent=agent)
    if agent.primary_model_id:
        target |= Q(model_id=agent.primary_model_id)
    runs = EvaluationRun.objects.filter(target, status=RunStatus.COMPLETED)
    if passing:
        runs = runs.filter(passed=True)
    if max_age_days:
        runs = runs.filter(finished_at__gte=timezone.now() - timedelta(days=max_age_days))
    return runs.select_related("suite").order_by("-finished_at").first()


def readiness(subject, *, assurance_summary="", oversight_point="", monitoring_commitments="") -> list[Check]:
    """IRGF Gate G3 entry criteria for this system, scaled to its tier."""
    statements = {
        "assurance_summary": assurance_summary, "oversight_point": oversight_point,
        "monitoring_commitments": monitoring_commitments,
    }
    if is_application(subject):
        return _application_readiness(subject, **statements)
    return _agent_readiness(subject, **statements)


def _classification_check(classification) -> Check:
    if classification is None:
        return Check("classification", "Risk tier confirmed", "S2 · G2", True, False,
                     "Not classified yet: score it at S2.")
    tier = classification.tier
    if not classification.is_confirmed:
        return Check("classification", "Risk tier confirmed", "S2 · G2", True, False,
                     f"Tier {tier} is provisional; it needs an independent countersignature (M6).")
    if classification.rescore_due:
        return Check("classification", "Risk tier confirmed", "S2 · G2", True, False,
                     "A re-score is due: " + ("; ".join(classification.rescore_reasons)
                                              or "the annual review has come round."))
    return Check("classification", "Risk tier confirmed", "S2 · G2", True, True,
                 f"Tier {tier}, countersigned {classification.countersigned_at:%d %b %Y}.")


def _agent_readiness(agent, *, assurance_summary="", oversight_point="", monitoring_commitments="") -> list[Check]:
    from apps.risks.models import Risk

    classification = ClassificationService.current(agent)
    tier = classification.tier if classification else 1
    checks = [_classification_check(classification)]

    checks.append(Check(
        "owner", "Named accountable owner", "G3", True, bool(agent.owner_id),
        (agent.owner.get_full_name() or agent.owner.email) if agent.owner_id
        else "Set the agent's owner.",
    ))
    checks.append(Check(
        "state", "Active, and not suspended", "S8", True,
        agent.is_active and not agent.is_suspended,
        "Active." if agent.is_active and not agent.is_suspended
        else ("Suspended by its kill-switch." if agent.is_suspended else f"Status: {agent.get_status_display()}."),
    ))

    if tier <= 1:
        run = _evaluation_run(agent)
        checks.append(Check("assurance", "Assurance", "S6", False, True,
                            "Not required at Tier 1." + (f" Latest run: {run.suite.name}." if run else "")))
    elif tier == 2:
        run = _evaluation_run(agent, max_age_days=irgf.ASSURANCE_MAX_AGE_DAYS[2])
        ok = run is not None or bool((assurance_summary or "").strip())
        detail = (f"Evaluated with '{run.suite.name}' ({run.score}%)." if run
                  else "Summarised in this request." if ok
                  else "Run an evaluation, or summarise the assurance in this request.")
        checks.append(Check("assurance", "Light assurance summary", "S6", True, ok, detail))
    else:
        days = irgf.ASSURANCE_MAX_AGE_DAYS[tier]
        run = _evaluation_run(agent, passing=True, max_age_days=days)
        checks.append(Check(
            "assurance", "Full assurance: a passing evaluation", "S6", True, run is not None,
            f"'{run.suite.name}' passed at {run.score}% on {run.finished_at:%d %b %Y}." if run
            else f"Needs a passing evaluation of the agent or its primary model within {days} days.",
        ))

    risks = Risk.objects.filter(organization=agent.organization).filter(
        Q(agent=agent) | Q(application__in=agent.applications.all())
    ).exclude(mitigation="")
    count = risks.count()
    checks.append(Check(
        "controls", "Controls mapped to specific risks", "S6 · control matrix", tier >= 3, count > 0,
        f"{count} risk{'s' if count != 1 else ''} with a recorded control." if count
        else "Record the risks this agent carries and the control on each.",
    ))

    if agent.acts:
        boundary = bool(agent.allowed_tools) and bool(agent.action_limits)
        checks.append(Check(
            "boundary", "Machine-readable boundary", "Links 04 · 06", True, boundary,
            "Tool allow-list and action limits declared." if boundary
            else "Declare the tool allow-list and action limits on the Agent Card.",
        ))
    else:
        checks.append(Check("boundary", "Machine-readable boundary", "Links 04 · 06", False, True,
                            "Advisory agent: no boundary needed."))

    # IRGF: at Tier 3-4 the conditions must be objectively evaluable and fire
    # without waiting for a person, so at least one must be an automatic trigger.
    conditions = bool((agent.kill_switch_conditions or "").strip())
    triggers = [key.replace("_", " ") for key in agent.kill_switch_triggers or []]
    if not conditions:
        detail = "State the conditions on the Agent Card."
    elif triggers:
        detail = "Declared on the Agent Card; triggers automatically on " + ", ".join(triggers) + "."
    elif tier >= 3:
        detail = "Declared, but none triggers automatically: Tier 3–4 needs at least one automatic trigger."
    else:
        detail = "Declared on the Agent Card."
    checks.append(Check(
        "kill_switch", "Pre-approved kill-switch conditions", "Link 06", tier >= 3,
        conditions and (bool(triggers) or tier < 3), detail,
    ))
    identity = agent.service_accounts.exists()
    checks.append(Check(
        "identity", "An identity of its own", "Link 02", tier >= 3, identity,
        "Bound: " + ", ".join(agent.service_accounts.values_list("name", flat=True)) if identity
        else "Bind a service account to the agent.",
    ))
    checks.append(Check(
        "oversight", "Human oversight point", "G3", tier >= 2, bool((oversight_point or "").strip()),
        "Stated in this request." if (oversight_point or "").strip()
        else "Who reviews, with what information, how fast, and with what authority.",
    ))
    checks.append(Check(
        "monitoring", "Monitoring commitments", "S8", tier >= 3,
        bool((monitoring_commitments or "").strip()),
        "Stated in this request." if (monitoring_commitments or "").strip()
        else "Which signals are watched, at what thresholds, by whom.",
    ))
    return checks


def _external_agents(organization) -> set[str]:
    """The addresses of agents other organizations run, published to the A2A Gateway."""
    from apps.a2a.models import A2AAgentCard, CardOrigin

    return set(
        A2AAgentCard.objects.for_organization(organization).filter(origin=CardOrigin.EXTERNAL)
        .values_list("slug", flat=True)
    )


def _unprovisioned(organization, edges) -> list[str]:
    """Declared delegations the A2A Gateway would refuse: no card, or no contract for a restricted one."""
    from apps.a2a.models import A2AAccessMode, A2AAgentCard, A2AContract

    cards = {card.address: card for card in A2AAgentCard.objects.for_organization(organization).select_related("agent")}
    missing = []
    for edge in edges:
        card = cards.get(edge.get("to"))
        if card is None:
            missing.append(f"{edge['from']} → {edge['to']} (not published)")
        elif card.access_mode != A2AAccessMode.OPEN and not A2AContract.objects.filter(
            recipient=card, sender__organization=organization, sender__agent_id=edge["from"]
        ).exists():
            missing.append(f"{edge['from']} → {edge['to']}")
    return missing


def _system_evaluation_run(application, agents, *, passing=False, max_age_days=None):
    """The latest evaluation of any of the application's agents or models."""
    from apps.evaluations.models import EvaluationRun, RunStatus

    target = Q(agent__in=agents) | Q(model__in=application.ai_models.all())
    runs = EvaluationRun.objects.filter(target, status=RunStatus.COMPLETED)
    if passing:
        runs = runs.filter(passed=True)
    if max_age_days:
        runs = runs.filter(finished_at__gte=timezone.now() - timedelta(days=max_age_days))
    return runs.select_related("suite").order_by("-finished_at").first()


def _application_readiness(application, *, assurance_summary="", oversight_point="",
                           monitoring_commitments="") -> list[Check]:
    """IRGF Gate G3 entry criteria for an application and the agents it composes."""
    from apps.classification.irgf import AUTONOMY_TO_D2
    from apps.risks.models import Risk

    classification = ClassificationService.current(application)
    tier = classification.tier if classification else 1
    agents = list(application.agents.all())
    acting = [agent for agent in agents if agent.acts]
    checks = [_classification_check(classification)]

    owner = subject_owner(application)
    checks.append(Check(
        "owner", "Named accountable owner", "G3", True, owner is not None,
        (owner.get_full_name() or owner.email) if owner else "Set the application's business owner.",
    ))
    checks.append(Check(
        "state", "Active", "S8", True, application.is_active,
        "Active." if application.is_active else f"Status: {application.get_status_display()}.",
    ))

    # Each acting agent carries its own G3 authorization; the application's adds the whole.
    environment = application.get_environment_display().lower()
    missing = [agent.name for agent in acting if AuthorizationService.in_force(agent, application.environment) is None]
    if not acting:
        detail = "No member agent acts; advisory agents need no authorization of their own."
    elif missing:
        detail = f"Not authorized for {environment}: {', '.join(missing)}."
    else:
        detail = f"All {len(acting)} acting agent{'s' if len(acting) != 1 else ''} authorized for {environment}."
    checks.append(Check("members", "Every acting agent authorized", "S7 · per agent", bool(acting), not missing, detail))

    composite = composite_authority(application)
    needed = AUTONOMY_TO_D2.get(composite["autonomy"], 1)
    d2 = classification.d2_autonomy if classification else None
    checks.append(Check(
        "composite", "S2 scored against the composite authority", "S2 · D2 · IRGF ch. 16", True,
        d2 is not None and d2 >= needed,
        (f"D2 is {d2}; its agents reach autonomy level {composite['autonomy']}, which needs D2 of at least {needed}."
         if d2 is not None else "Score the application at S2 against what its agents can reach together."),
    ))

    members = {agent.agent_id for agent in agents}
    # Work may also be handed to an agent another organization runs, once it is
    # published to the A2A Gateway: its address is how the graph names it.
    targets = members | _external_agents(application.organization)
    edges = application.delegations or []
    strays = [f"{edge.get('from')} → {edge.get('to')}" for edge in edges
              if edge.get("from") not in members or edge.get("to") not in targets]
    several = len(acting) > 1
    if strays:
        ok, detail = False, "Name only member agents, or external agents published for A2A: " + ", ".join(strays) + "."
    elif edges:
        ok, detail = True, f"{len(edges)} delegation{'s' if len(edges) != 1 else ''} declared."
    elif several:
        ok, detail = False, "Several agents act: declare which may invoke which, and with whose authority."
    else:
        ok, detail = True, "One acting agent at most: nothing to delegate."
    checks.append(Check(
        "delegations", "Delegation graph declared", "IRGF ch. 16", bool(strays) or (several and tier >= 3), ok, detail,
    ))
    if edges and not strays:
        missing = _unprovisioned(application.organization, edges)
        checks.append(Check(
            "provisioned", "Delegations provisioned at the A2A Gateway", "IRGF ch. 16 · A2A", False, not missing,
            "Each declared delegation has a contract, or reaches an agent open to every agent." if not missing
            else "The gateway will refuse: " + ", ".join(missing) + ". Grant each a contract on the recipient's A2A page.",
        ))

    unapproved = [model.model_name for model in application.ai_models.all() if not model.approved_for_use]
    unapproved += [provider.name for provider in application.providers.all() if not provider.is_approved]
    checks.append(Check(
        "components", "Models and providers approved", "S5", True, not unapproved,
        "All approved for use." if not unapproved else "Not approved: " + ", ".join(unapproved) + ".",
    ))

    if tier <= 1:
        checks.append(Check("assurance", "Assurance", "S6", False, True, "Not required at Tier 1."))
    elif tier == 2:
        run = _system_evaluation_run(application, agents, max_age_days=irgf.ASSURANCE_MAX_AGE_DAYS[2])
        ok = run is not None or bool((assurance_summary or "").strip())
        detail = (f"Evaluated with '{run.suite.name}' ({run.score}%)." if run
                  else "Summarised in this request." if ok
                  else "Run an evaluation of its agents or models, or summarise the assurance in this request.")
        checks.append(Check("assurance", "Light assurance summary", "S6", True, ok, detail))
    else:
        days = irgf.ASSURANCE_MAX_AGE_DAYS[tier]
        run = _system_evaluation_run(application, agents, passing=True, max_age_days=days)
        checks.append(Check(
            "assurance", "Full assurance: a passing evaluation", "S6", True, run is not None,
            f"'{run.suite.name}' passed at {run.score}% on {run.finished_at:%d %b %Y}." if run
            else f"Needs a passing evaluation of one of its agents or models within {days} days.",
        ))

    risks = Risk.objects.filter(organization=application.organization).filter(
        Q(application=application) | Q(agent__in=agents)
    ).exclude(mitigation="")
    count = risks.count()
    checks.append(Check(
        "controls", "Controls mapped to specific risks", "S6 · control matrix", tier >= 3, count > 0,
        f"{count} risk{'s' if count != 1 else ''} with a recorded control." if count
        else "Record the risks the application carries and the control on each.",
    ))
    checks.append(Check(
        "oversight", "Human oversight point", "G3", tier >= 2, bool((oversight_point or "").strip()),
        "Stated in this request." if (oversight_point or "").strip()
        else "Who reviews, with what information, how fast, and with what authority.",
    ))
    checks.append(Check(
        "monitoring", "Monitoring commitments", "S8", tier >= 3, bool((monitoring_commitments or "").strip()),
        "Stated in this request." if (monitoring_commitments or "").strip()
        else "Which signals are watched, at what thresholds, by whom.",
    ))
    return checks


# -- S7 · Gate G3 ----------------------------------------------------------------


class AuthorizationService:
    readiness = staticmethod(readiness)

    @staticmethod
    def baseline(subject) -> DeploymentAuthorization | None:
        """The authorization the system is measured against: in force or suspended."""
        if subject is None or subject.pk is None:
            return None
        return (
            DeploymentAuthorization.objects.filter(**subject_filter(subject), status__in=BASELINE_STATUSES)
            .order_by("-decided_at", "-requested_at")
            .first()
        )

    @classmethod
    def in_force(cls, subject, environment: str | None = None) -> DeploymentAuthorization | None:
        authorization = cls.baseline(subject)
        if authorization is None or not authorization.is_in_force:
            return None
        if environment and authorization.environment != environment:
            return None
        return authorization

    @staticmethod
    def pending(subject) -> DeploymentAuthorization | None:
        return (
            DeploymentAuthorization.objects.filter(**subject_filter(subject), status=AuthorizationStatus.PENDING)
            .order_by("-requested_at").first()
        )

    @staticmethod
    def current(subject) -> DeploymentAuthorization | None:
        """The newest record that still says something about the system."""
        return (
            DeploymentAuthorization.objects.filter(**subject_filter(subject))
            .exclude(status=AuthorizationStatus.SUPERSEDED)
            .order_by("-requested_at").first()
        )

    @classmethod
    @transaction.atomic
    def request(cls, subject, *, actor, assurance_summary="", oversight_point="",
                monitoring_commitments="", conditions=()) -> DeploymentAuthorization:
        if cls.pending(subject):
            raise LifecycleError("A request for this system is already awaiting a decision at Gate G3.")
        checks = readiness(
            subject, assurance_summary=assurance_summary, oversight_point=oversight_point,
            monitoring_commitments=monitoring_commitments,
        )
        blocking = [check.label for check in checks if check.blocking]
        if blocking:
            raise LifecycleError("Gate G3 entry criteria are not met: " + "; ".join(blocking) + ".")

        classification = ClassificationService.current(subject)
        person = _person(actor)
        authorization = DeploymentAuthorization.objects.create(
            organization=subject.organization,
            **subject_filter(subject),
            environment=subject.environment,
            tier=classification.tier,
            classification=classification,
            classification_confirmed_at=classification.countersigned_at,
            configuration=snapshot(subject),
            evidence=[asdict(check) for check in checks],
            assurance_summary=(assurance_summary or "").strip(),
            oversight_point=(oversight_point or "").strip(),
            monitoring_commitments=(monitoring_commitments or "").strip(),
            accountable_owner=subject_owner(subject),
            requested_by=person,
            created_by=person,
        )
        for condition in conditions:
            AuthorizationCondition.objects.create(
                organization=subject.organization,
                authorization=authorization,
                description=condition["description"],
                owner=condition.get("owner"),
                due_date=condition["due_date"],
            )

        _audit(AuditAction.GATE_REQUEST, authorization, actor=actor,
               new_value={"environment": authorization.environment, "tier": authorization.tier},
               metadata={"stage": "S7", "decided_by": decisions.describe(authorization)})
        _notify(
            authorization,
            title=f"Gate G3: authorize {subject.name} for {authorization.get_environment_display().lower()}",
            body=(f"Tier {authorization.tier}. Decision needed from "
                  f"{decisions.describe(authorization)}."),
            url=reverse("lifecycle:authorization_detail", args=[authorization.pk]),
            dedupe_key=f"g3_request:{authorization.pk}",
        )
        return authorization

    @classmethod
    @transaction.atomic
    def sign(cls, authorization, *, actor, note="", dissent="") -> DeploymentAuthorization:
        if not authorization.is_pending:
            raise LifecycleError("Only a request awaiting a decision can be signed.")
        verdict = decisions.check(authorization, actor)
        if not verdict.ok:
            raise LifecycleError(verdict.reason)
        if differences(authorization.subject, authorization.configuration):
            raise LifecycleError(
                f"The {authorization.subject_kind} has changed since this request was made. Request authorization again, "
                "so the configuration signed is the one that runs."
            )

        authorization.signatures = [*authorization.signatures, decisions.signature(actor, verdict.seat, note)]
        if (dissent or "").strip():
            name = authorization.signatures[-1]["name"]
            authorization.dissent = "\n".join(filter(None, [authorization.dissent, f"{name}: {dissent.strip()}"]))

        complete = decisions.is_complete(authorization)
        if complete:
            cls._authorize(authorization, note=note)
        else:
            authorization.save(update_fields=["signatures", "dissent", "updated_at"])

        _audit(AuditAction.GATE_SIGNATURE, authorization, actor=actor,
               new_value={"seat": verdict.seat.label, "complete": complete})
        url = reverse("lifecycle:authorization_detail", args=[authorization.pk])
        if complete:
            if authorization.requested_by_id:
                _notify(authorization, title=f"{authorization.subject.name} is authorized (Gate G3)",
                        body=f"Authorized for {authorization.get_environment_display().lower()} at Tier {authorization.tier}.",
                        url=url, recipients=[authorization.requested_by])
        else:
            _notify(authorization, title=f"Gate G3: second signature needed for {authorization.subject.name}",
                    body=f"Tier 4 needs {decisions.describe(authorization)}.", url=url,
                    dedupe_key=f"g3_second:{authorization.pk}")
        return authorization

    @staticmethod
    def _authorize(authorization, *, note=""):
        now = timezone.now()
        earlier = DeploymentAuthorization.objects.filter(
            **subject_filter(authorization.subject), status__in=BASELINE_STATUSES
        ).exclude(pk=authorization.pk)
        for old in earlier:
            old.status = AuthorizationStatus.SUPERSEDED
            old.superseded_by = authorization
            old.save(update_fields=["status", "superseded_by", "updated_at"])
            old.changes.filter(status__in=OPEN_CHANGE_STATUSES).update(
                status=ChangeStatus.SUPERSEDED, resolved_at=now
            )
        authorization.status = AuthorizationStatus.AUTHORIZED
        authorization.decided_at = now
        authorization.decision_note = note
        authorization.save()

    @classmethod
    @transaction.atomic
    def reject(cls, authorization, *, actor, note) -> DeploymentAuthorization:
        if not authorization.is_pending:
            raise LifecycleError("Only a request awaiting a decision can be rejected.")
        verdict = decisions.check(authorization, actor, approving=False)
        if not verdict.ok:
            raise LifecycleError(verdict.reason)
        if not (note or "").strip():
            raise LifecycleError("Say why, so the request can be made again with what is missing.")
        authorization.status = AuthorizationStatus.REJECTED
        authorization.decided_at = timezone.now()
        authorization.decision_note = note.strip()
        authorization.save()
        _audit(AuditAction.GATE_REJECTION, authorization, actor=actor, new_value={"note": note})
        if authorization.requested_by_id:
            _notify(authorization, title=f"Gate G3 refused {authorization.subject.name}", body=note,
                    url=reverse("lifecycle:authorization_detail", args=[authorization.pk]),
                    recipients=[authorization.requested_by])
        return authorization

    @classmethod
    @transaction.atomic
    def revoke(cls, authorization, *, actor, reason) -> DeploymentAuthorization:
        if authorization.status not in (*BASELINE_STATUSES, AuthorizationStatus.PENDING):
            raise LifecycleError("This authorization is no longer in force.")
        person = _person(actor)
        allowed = person is not None and (
            person.has_org_permission(authorization.organization, Perm.GATE_DECIDE)
            or (authorization.tier <= 2 and person.pk == authorization.subject_owner_id)
        )
        if not allowed:
            raise LifecycleError("Revoking needs the AI Governance Body, or the owner at Tier 1-2.")
        if not (reason or "").strip():
            raise LifecycleError("Say why the authorization is revoked.")
        authorization.status = AuthorizationStatus.REVOKED
        authorization.decision_note = reason.strip()
        authorization.save()
        _audit(AuditAction.AUTHORIZATION_REVOKE, authorization, actor=actor, new_value={"reason": reason})
        return authorization

    @staticmethod
    def suspend(authorization, *, reason: str, for_changes: bool = True) -> None:
        if authorization.status != AuthorizationStatus.AUTHORIZED:
            return
        authorization.status = AuthorizationStatus.SUSPENDED
        authorization.suspended_at = timezone.now()
        authorization.suspension_reason = reason
        authorization.suspended_for_changes = for_changes
        authorization.save()
        _audit(AuditAction.AUTHORIZATION_SUSPEND, authorization, new_value={"reason": reason})
        _notify(authorization, title=f"{authorization.subject.name}'s authorization is suspended",
                body=reason, url=reverse("lifecycle:authorization_detail", args=[authorization.pk]),
                recipients=[user for user in (authorization.subject_owner,) if user] or None,
                dedupe_key=f"g3_suspended:{authorization.pk}:{authorization.suspended_at:%Y%m%d%H%M%S}")

    @staticmethod
    def restore(authorization) -> None:
        if authorization.status != AuthorizationStatus.SUSPENDED or not authorization.suspended_for_changes:
            return
        authorization.status = AuthorizationStatus.AUTHORIZED
        authorization.suspended_at = None
        authorization.suspension_reason = ""
        authorization.suspended_for_changes = False
        authorization.save()
        _audit(AuditAction.UPDATE, authorization, new_value={"status": authorization.status},
               metadata={"reason": "No change awaiting Gate G4 blocks the authorization any more."})


# -- S9 · Gate G4 ----------------------------------------------------------------


def _decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _grants_widened(before, after) -> bool:
    previous = {grant["server"]: set(grant["tools"]) for grant in before or []}
    for grant in after or []:
        tools = set(grant["tools"])
        if grant["server"] not in previous:
            return True
        earlier = previous[grant["server"]]
        # An empty tool list means every enabled tool: the widest grant there is.
        if earlier and (not tools or tools - earlier):
            return True
    return False


def _contracts_widened(before, after) -> bool:
    """Another agent to reach, or more of one: an empty skill list means every skill."""

    def as_grants(contracts):
        return [{"server": item["to"], "tools": item.get("skills") or []} for item in contracts or []]

    return _grants_widened(as_grants(before), as_grants(after))


def _limits_widened(before, after) -> bool:
    before, after = before or {}, after or {}
    for key, value in before.items():
        if key not in after:
            return True
        if key in ("max_value_per_action", "max_value_per_day", "max_actions_per_hour"):
            old, new = _decimal(value), _decimal(after[key])
            if old is not None and new is not None and new > old:
                return True
        if key == "allowed_counterparties" and set(after[key]) - set(value):
            return True
    return False


def change_type(field, before, after) -> str:
    if field in ("primary_model", "fallback_model"):
        if after is None:
            return "fallback_removed" if field == "fallback_model" else "model_family"
        if before is None:
            return "model_family"
        same = (
            before.get("provider") == after.get("provider")
            and before.get("family") and before.get("family") == after.get("family")
        )
        return "model_version" if same else "model_family"
    if field == "provider":
        return "model_family"
    if field == "autonomy_level":
        return "autonomy_increase" if (after or 0) > (before or 0) else "autonomy_decrease"
    if field == "requires_human_approval":
        return "oversight_removed" if before and not after else "oversight_added"
    if field == "allowed_tools":
        return "tool_added" if set(after or []) - set(before or []) else "tool_removed"
    if field == "restricted_tools":
        return "tool_added" if set(before or []) - set(after or []) else "tool_removed"
    if field == "mcp_grants":
        return "tool_added" if _grants_widened(before, after) else "tool_removed"
    if field == "a2a_contracts":
        return "peer_added" if _contracts_widened(before, after) else "peer_removed"
    if field == "knowledge_sources":
        return "grounding_added" if set(after or []) - set(before or []) else "grounding_removed"
    if field == "data_classification":
        raised = _SENSITIVITY.get(after, 0) > _SENSITIVITY.get(before, 0)
        return "grounding_added" if raised else "grounding_removed"
    if field == "environment":
        return "population"
    if field == "system_prompt_reference":
        return "prompt"
    if field == "action_limits":
        return "limits_raised" if _limits_widened(before, after) else "limits_lowered"
    if field in ("kill_switch_conditions", "kill_switch_triggers"):
        return "kill_switch"
    if field == "identities":
        return "identity"
    if field == "risk_tier":
        return "tier_increase" if (after or 0) > (before or 0) else "tier_decrease"
    # An application and the agents it composes.
    if field == "models":
        known = {(ref.get("provider"), ref.get("family")) for ref in before or []}
        added = [ref for ref in after or [] if ref.get("id") not in {old.get("id") for old in before or []}]
        if not added:
            return "model_removed"
        same = all(ref.get("family") and (ref.get("provider"), ref.get("family")) in known for ref in added)
        return "model_version" if same else "model_family"
    if field == "providers":
        widened = {ref.get("id") for ref in after or []} - {ref.get("id") for ref in before or []}
        return "model_family" if widened else "model_removed"
    if field == "agents":
        widened = {ref.get("id") for ref in after or []} - {ref.get("id") for ref in before or []}
        return "agent_added" if widened else "agent_removed"
    if field == "delegations":
        return "delegation_added" if comparable(field, after) and set(comparable(field, after)) - set(comparable(field, before)) else "delegation_removed"
    if field == "composite":
        before, after = before or {}, after or {}
        if (after.get("autonomy") or 0) > (before.get("autonomy") or 0):
            return "autonomy_increase"
        if set(after.get("unsupervised") or []) - set(before.get("unsupervised") or []):
            return "oversight_removed"
        return "tool_added" if set(after.get("tools") or []) - set(before.get("tools") or []) else "tool_removed"
    if field == "tools":
        return "tool_added" if set(after or []) - set(before or []) else "tool_removed"
    return "prompt"


class ChangeService:
    @classmethod
    def detect(cls, subject) -> list[ChangeRecord]:
        """Compare the system with its authorized configuration and record what moved."""
        authorization = AuthorizationService.baseline(subject)
        if authorization is None:
            return []
        current = snapshot(subject)
        baseline = authorization.configuration or {}
        open_records = {
            record.field: record
            for record in authorization.changes.filter(status__in=OPEN_CHANGE_STATUSES)
        }
        created = []
        with transaction.atomic():
            for field in fields_of(current):
                if not recorded(baseline, field):
                    continue
                before, after = baseline.get(field), current.get(field)
                differs = comparable(field, before) != comparable(field, after)
                record = open_records.get(field)
                if differs and record is None:
                    created.append(cls._record(authorization, subject, field, before, after))
                elif differs and comparable(field, record.after) != comparable(field, after):
                    cls._update(record, before, after)
                elif not differs and record is not None:
                    cls._revert(record)
            cls._settle(authorization)
        return created

    @staticmethod
    def _classify(record, before, after):
        record.change_type = change_type(record.field, before, after)
        verdict = irgf.classify(record.change_type, record.tier_at_change)
        record.classification = verdict.classification
        record.classification_reason = verdict.reason
        record.requires_rescore = verdict.requires_rescore
        record.requires_reauthorization = verdict.requires_reauthorization
        record.autonomy_change = record.change_type == "autonomy_increase"
        record.description = (
            f"{FIELD_LABELS[record.field]}: {display(record.field, before)} → {display(record.field, after)}"
        )
        record.before, record.after = before, after
        return verdict

    @classmethod
    def _record(cls, authorization, subject, field, before, after) -> ChangeRecord:
        context = get_audit_context()
        record = ChangeRecord(
            organization=subject.organization,
            **subject_filter(subject),
            authorization=authorization,
            field=field,
            tier_at_change=max(authorization.tier, subject.risk_tier or 1),
            changed_by=_person(context.actor),
            changed_by_label=context.actor_label or (str(context.actor) if context.actor else "system"),
        )
        cls._classify(record, before, after)
        if record.is_gated:
            record.status = ChangeStatus.AWAITING_G4
        else:
            record.status = ChangeStatus.ACCEPTED
            record.resolved_at = timezone.now()
        record.save()
        if not record.is_gated:
            cls._rebaseline(authorization, field, after)

        _audit(AuditAction.CHANGE_DETECTED, record, actor=context.actor,
               old_value={field: before}, new_value={field: after},
               metadata={"stage": "S9", "classification": record.classification})
        if record.is_gated:
            # Autonomy increases already flag a re-score through the S2 triggers.
            if record.requires_rescore and record.change_type != "autonomy_increase":
                ClassificationService.flag_rescore(
                    subject, f"{record.change_label}, a {record.classification} change at S9."
                )
            _notify(record, title=f"Gate G4: {record.classification} change to {subject.name}",
                    body=f"{record.description}\n\n{record.classification_reason}",
                    url=reverse("lifecycle:change_detail", args=[record.pk]),
                    dedupe_key=f"g4_change:{record.pk}")
        return record

    @classmethod
    def _update(cls, record, before, after) -> None:
        """The field moved again before the change was decided: judge the new value."""
        cls._classify(record, before, after)
        record.signatures = []
        record.status = ChangeStatus.AWAITING_G4 if record.is_gated else ChangeStatus.ACCEPTED
        if record.status == ChangeStatus.ACCEPTED:
            record.resolved_at = timezone.now()
            cls._rebaseline(record.authorization, record.field, after)
        record.save()

    @staticmethod
    def _revert(record) -> None:
        record.status = ChangeStatus.REVERTED
        record.resolved_at = timezone.now()
        record.save(update_fields=["status", "resolved_at", "updated_at"])
        _audit(AuditAction.UPDATE, record, new_value={"status": record.status},
               metadata={"reason": "The system is back on its authorized configuration."})

    @staticmethod
    def _rebaseline(authorization, field, value) -> None:
        configuration = dict(authorization.configuration or {})
        configuration[field] = value
        authorization.configuration = configuration
        authorization.save(update_fields=["configuration", "updated_at"])

    @staticmethod
    def blocking(authorization):
        return authorization.changes.filter(
            Q(status=ChangeStatus.REJECTED)
            | Q(status__in=OPEN_CHANGE_STATUSES, classification=irgf.MAJOR)
        )

    @classmethod
    def _settle(cls, authorization) -> None:
        """Suspend while a major or rejected change stands; restore when none does."""
        authorization.refresh_from_db()
        blocking = list(cls.blocking(authorization))
        if blocking and authorization.status == AuthorizationStatus.AUTHORIZED:
            AuthorizationService.suspend(
                authorization,
                reason="Awaiting Gate G4: " + "; ".join(record.description for record in blocking),
            )
        elif not blocking:
            AuthorizationService.restore(authorization)

    # -- Decisions ----------------------------------------------------------

    @classmethod
    @transaction.atomic
    def decide(cls, record, *, actor, approve: bool, note="", delta_assurance_scope="") -> ChangeRecord:
        if record.status != ChangeStatus.AWAITING_G4:
            raise LifecycleError("Only a change awaiting Gate G4 can be decided.")
        if approve:
            if record.requires_reauthorization:
                raise LifecycleError(
                    f"This change needs the {record.subject_kind} authorized again at Gate G3; G4 cannot approve it."
                )
            if not (delta_assurance_scope or "").strip():
                raise LifecycleError(
                    "State the delta assurance scope: what was re-run, what was not, and why."
                )
            if record.requires_rescore:
                current = ClassificationService.current(record.subject)
                if current is None or not current.is_confirmed or current.scored_at < record.detected_at:
                    raise LifecycleError(
                        f"Re-score the {record.subject_kind} at S2 and have the tier countersigned (G2) before G4."
                    )
        elif not (note or "").strip():
            raise LifecycleError("Say why the change is rejected.")

        verdict = decisions.check(record, actor, approving=approve)
        if not verdict.ok:
            raise LifecycleError(verdict.reason)

        record.signatures = [*record.signatures, decisions.signature(actor, verdict.seat, note)]
        if approve:
            record.delta_assurance_scope = delta_assurance_scope.strip()
            if decisions.is_complete(record):
                record.status = ChangeStatus.APPROVED
                record.decided_at = record.resolved_at = timezone.now()
                record.decision_note = note
                cls._rebaseline(record.authorization, record.field, snapshot(record.subject)[record.field])
            action = AuditAction.GATE_SIGNATURE
        else:
            record.status = ChangeStatus.REJECTED
            record.decided_at = timezone.now()
            record.decision_note = note.strip()
            action = AuditAction.GATE_REJECTION
        record.save()

        _audit(action, record, actor=actor,
               new_value={"status": record.status, "seat": verdict.seat.label})
        cls._settle(record.authorization)
        return record

    @classmethod
    @transaction.atomic
    def reclassify(cls, record, *, actor, classification: str, reason: str) -> ChangeRecord:
        if record.status != ChangeStatus.AWAITING_G4:
            raise LifecycleError("Only a change awaiting Gate G4 can be reclassified.")
        if classification not in irgf.CLASSES:
            raise LifecycleError("Choose cosmetic, minor, material or major.")
        if not (reason or "").strip():
            raise LifecycleError("Give the reasoning; it is part of the change record.")
        lowering = irgf.CLASSES.index(classification) < irgf.CLASSES.index(record.classification)
        if lowering and record.change_type not in irgf.RECLASSIFIABLE:
            raise LifecycleError(
                f"{record.change_label}: IRGF fixes this class, so it cannot be lowered."
            )

        record.classification = classification
        record.auto_classified = False
        record.reclassified_by = _person(actor)
        record.reclassification_reason = reason.strip()
        record.signatures = []
        if classification in irgf.GATED:
            record.status = ChangeStatus.AWAITING_G4
        elif record.tier >= irgf.INDEPENDENT_FROM_TIER:
            record.status = ChangeStatus.AWAITING_CONFIRMATION
        else:
            record.status = ChangeStatus.ACCEPTED
            record.resolved_at = timezone.now()
            cls._rebaseline(record.authorization, record.field, record.after)
        record.save()
        _audit(AuditAction.UPDATE, record, actor=actor,
               new_value={"classification": classification, "status": record.status},
               metadata={"reason": reason})
        cls._settle(record.authorization)
        return record

    @classmethod
    @transaction.atomic
    def confirm(cls, record, *, actor) -> ChangeRecord:
        """IRGF: a second person confirms a lower class claimed at Tier 3-4."""
        if record.status != ChangeStatus.AWAITING_CONFIRMATION:
            raise LifecycleError("Nothing here is waiting for confirmation.")
        person = _person(actor)
        if person is None or person.pk in {record.reclassified_by_id, record.changed_by_id}:
            raise LifecycleError(
                "A second person must confirm: not whoever made the change or proposed the lower class."
            )
        record.status = ChangeStatus.ACCEPTED
        record.confirmed_by = person
        record.confirmed_at = record.resolved_at = timezone.now()
        record.save()
        cls._rebaseline(record.authorization, record.field, record.after)
        _audit(AuditAction.UPDATE, record, actor=actor, new_value={"status": record.status},
               metadata={"reason": "second-person confirmation"})
        cls._settle(record.authorization)
        return record


# -- M17 · Exceptions --------------------------------------------------------------


class ExceptionService:
    @staticmethod
    def active_policy_ids(organization, agent) -> set:
        if agent is None or getattr(agent, "pk", None) is None:
            return set()
        return set(
            GovernanceException.objects.filter(
                agent=agent, status=ExceptionStatus.ACTIVE, expires_at__gt=timezone.now(),
                policy__isnull=False,
            ).values_list("policy_id", flat=True)
        )

    @classmethod
    @transaction.atomic
    def request(cls, agent, policy, *, actor, justification, compensating_control="",
                duration_days: int, renewal_of=None, divergence="") -> GovernanceException:
        """Request an exception from a policy -- or, with no policy, for a divergence
        the Control Plane found that must persist for now (M13's "Except")."""
        divergence = (divergence or "").strip()
        if policy is None and not divergence:
            raise LifecycleError("Say what is set aside: a policy, or a divergence the Control Plane found.")
        if policy is not None and policy.organization_id != agent.organization_id:
            raise LifecycleError("The policy and the agent belong to different organizations.")
        if not 1 <= int(duration_days) <= irgf.MAX_EXCEPTION_DAYS:
            raise LifecycleError(f"An exception lasts at most {irgf.MAX_EXCEPTION_DAYS} days (IRGF M17).")
        if not (justification or "").strip():
            raise LifecycleError("State why the requirement cannot be met, or is met another way.")
        tier = agent.risk_tier or 1
        if tier >= irgf.INDEPENDENT_FROM_TIER and not (compensating_control or "").strip():
            raise LifecycleError("At Tier 3-4 an exception needs a compensating control.")
        if renewal_of is not None:
            if max(tier, renewal_of.tier) >= irgf.NO_RENEWAL_FROM_TIER:
                raise LifecycleError(
                    "Tier 4 exceptions cannot be renewed: the agent goes back through Gate G3."
                )
            if renewal_of.agent_id != agent.pk or renewal_of.policy_id != (policy.pk if policy else None):
                raise LifecycleError("A renewal must be for the same agent and policy.")
        if policy is not None and GovernanceException.objects.filter(
            agent=agent, policy=policy, status=ExceptionStatus.PENDING
        ).exists():
            raise LifecycleError("An exception for this agent and policy is already awaiting a decision.")

        person = _person(actor)
        exception = GovernanceException.objects.create(
            organization=agent.organization, agent=agent, policy=policy,
            divergence="" if policy is not None else divergence,
            justification=justification.strip(),
            compensating_control=(compensating_control or "").strip(),
            duration_days=int(duration_days), tier=tier, requested_by=person,
            renewal_of=renewal_of, created_by=person,
        )
        _audit(AuditAction.GATE_REQUEST, exception, actor=actor,
               new_value={"policy": policy.name if policy else None, "divergence": exception.divergence,
                          "days": exception.duration_days})
        _notify(exception, title=f"M17: exception for {agent.name} from {exception.subject_label}"[:200],
                body=f"{exception.justification}\n\nDecision needed from {decisions.describe(exception)}.",
                url=reverse("lifecycle:exception_detail", args=[exception.pk]),
                dedupe_key=f"m17_request:{exception.pk}")
        return exception

    @classmethod
    @transaction.atomic
    def sign(cls, exception, *, actor, note="") -> GovernanceException:
        if exception.status != ExceptionStatus.PENDING:
            raise LifecycleError("Only an exception awaiting a decision can be signed.")
        verdict = decisions.check(exception, actor)
        if not verdict.ok:
            raise LifecycleError(verdict.reason)
        exception.signatures = [*exception.signatures, decisions.signature(actor, verdict.seat, note)]
        complete = decisions.is_complete(exception)
        if complete:
            now = timezone.now()
            exception.status = ExceptionStatus.ACTIVE
            exception.starts_at = exception.decided_at = now
            exception.expires_at = now + timedelta(days=exception.duration_days)
            exception.decision_note = note
            previous = exception.renewal_of
            if previous is not None and previous.status == ExceptionStatus.ACTIVE:
                previous.status = ExceptionStatus.EXPIRED
                previous.save(update_fields=["status", "updated_at"])
        exception.save()
        _audit(AuditAction.GATE_SIGNATURE, exception, actor=actor,
               new_value={"seat": verdict.seat.label, "complete": complete})
        if complete and exception.requested_by_id:
            _notify(exception, title=f"Exception granted until {exception.expires_at:%d %b %Y}",
                    body=f"Set aside for {exception.agent.name}: {exception.subject_label}.",
                    url=reverse("lifecycle:exception_detail", args=[exception.pk]),
                    recipients=[exception.requested_by])
        return exception

    @classmethod
    @transaction.atomic
    def reject(cls, exception, *, actor, note) -> GovernanceException:
        if exception.status != ExceptionStatus.PENDING:
            raise LifecycleError("Only an exception awaiting a decision can be rejected.")
        verdict = decisions.check(exception, actor, approving=False)
        if not verdict.ok:
            raise LifecycleError(verdict.reason)
        if not (note or "").strip():
            raise LifecycleError("Say why the exception is refused.")
        exception.status = ExceptionStatus.REJECTED
        exception.decided_at = timezone.now()
        exception.decision_note = note.strip()
        exception.save()
        _audit(AuditAction.GATE_REJECTION, exception, actor=actor, new_value={"note": note})
        return exception

    @classmethod
    @transaction.atomic
    def revoke(cls, exception, *, actor, reason) -> GovernanceException:
        if exception.status not in (ExceptionStatus.ACTIVE, ExceptionStatus.PENDING):
            raise LifecycleError("This exception is not in effect.")
        person = _person(actor)
        if person is None or not (
            person.has_org_permission(exception.organization, Perm.GATE_DECIDE)
            or person.has_org_permission(exception.organization, Perm.RISK_MANAGE)
        ):
            raise LifecycleError("Revoking an exception needs the risk function or the governance body.")
        if not (reason or "").strip():
            raise LifecycleError("Say why the exception is revoked.")
        exception.status = ExceptionStatus.REVOKED
        exception.revoked_at = timezone.now()
        exception.revoked_by = person
        exception.revoke_reason = reason.strip()
        exception.save()
        _audit(AuditAction.UPDATE, exception, actor=actor, new_value={"status": exception.status},
               metadata={"reason": reason})
        return exception

    @staticmethod
    def _people(exception) -> list:
        from django.contrib.auth import get_user_model

        ids = {entry["user"] for entry in exception.signatures or []}
        if exception.requested_by_id:
            ids.add(str(exception.requested_by_id))
        return list(get_user_model().objects.filter(pk__in=ids))

    @classmethod
    def sweep(cls, now=None) -> dict:
        """Expire lapsed exceptions and warn before others lapse (the approvers too)."""
        now = now or timezone.now()
        expired = 0
        for exception in GovernanceException.objects.filter(
            status=ExceptionStatus.ACTIVE, expires_at__lte=now
        ).select_related("agent", "policy"):
            exception.status = ExceptionStatus.EXPIRED
            exception.save(update_fields=["status", "updated_at"])
            _audit(AuditAction.EXCEPTION_EXPIRE, exception, new_value={"status": exception.status})
            _notify(exception, title=f"Exception expired: {exception.agent.name}",
                    body=f"No longer set aside for {exception.agent.name}: {exception.subject_label}.",
                    url=reverse("lifecycle:exception_detail", args=[exception.pk]),
                    recipients=cls._people(exception) or None,
                    dedupe_key=f"m17_expired:{exception.pk}")
            expired += 1

        warned = 0
        soon = now + timedelta(days=irgf.EXCEPTION_WARNING_DAYS)
        for exception in GovernanceException.objects.filter(
            status=ExceptionStatus.ACTIVE, expires_at__gt=now, expires_at__lte=soon,
            expiry_warned_at__isnull=True,
        ).select_related("agent", "policy"):
            _notify(exception, title=f"Exception for {exception.agent.name} ends {exception.expires_at:%d %b}",
                    body="Renew it, or make sure the requirement can be met by then."
                    + (" Tier 4 exceptions cannot be renewed." if exception.tier >= irgf.NO_RENEWAL_FROM_TIER else ""),
                    url=reverse("lifecycle:exception_detail", args=[exception.pk]),
                    recipients=cls._people(exception) or None,
                    dedupe_key=f"m17_expiring:{exception.pk}")
            exception.expiry_warned_at = now
            exception.save(update_fields=["expiry_warned_at", "updated_at"])
            warned += 1
        return {"expired": expired, "warned": warned}


# -- G3 conditions ------------------------------------------------------------------


class ConditionService:
    @staticmethod
    @transaction.atomic
    def mark_met(condition, *, actor, note="") -> AuthorizationCondition:
        person = _person(actor)
        if person is None:
            raise LifecycleError("A person marks a condition met.")
        signers = {entry["user"] for entry in condition.authorization.signatures or []}
        allowed = (
            person.pk == condition.owner_id
            or str(person.pk) in signers
            or person.has_org_permission(condition.organization, Perm.GATE_DECIDE)
        )
        if not allowed:
            raise LifecycleError("The condition's owner, a signer or the governance body marks it met.")
        if condition.is_met:
            raise LifecycleError("This condition is already met.")
        condition.met_at = timezone.now()
        condition.met_by = person
        condition.met_note = (note or "").strip()
        condition.save()
        AuditService.log(
            action=AuditAction.UPDATE, resource_type=condition._meta.label, resource_id=condition.pk,
            resource_label=str(condition), organization=condition.organization, actor=person,
            new_value={"met": True, "note": condition.met_note}, metadata={"irgf": "G3 condition"},
        )
        return condition

    @staticmethod
    def escalate_overdue(now=None) -> int:
        """Past its deadline, an unmet condition goes back to whoever authorized the system."""
        from django.contrib.auth import get_user_model

        now = now or timezone.now()
        escalated = 0
        conditions = AuthorizationCondition.objects.filter(
            met_at__isnull=True, due_date__lt=now.date(),
            authorization__status__in=BASELINE_STATUSES,
        ).filter(Q(escalated_at__isnull=True) | Q(escalated_at__lt=now - timedelta(days=1)))
        for condition in conditions.select_related("authorization__agent", "authorization__application", "owner"):
            authorization = condition.authorization
            ids = {entry["user"] for entry in authorization.signatures or []}
            people = list(get_user_model().objects.filter(pk__in=ids))
            if condition.owner_id and condition.owner not in people:
                people.append(condition.owner)
            _notify(
                authorization,
                title=f"Overdue G3 condition: {authorization.subject.name}",
                body=f"{condition.description}\n\nWas due {condition.due_date:%d %b %Y}.",
                url=reverse("lifecycle:authorization_detail", args=[authorization.pk]),
                recipients=people or None,
                dedupe_key=f"g3_condition:{condition.pk}:{now:%Y%m%d}",
            )
            condition.escalated_at = now
            condition.save(update_fields=["escalated_at", "updated_at"])
            escalated += 1
        return escalated
