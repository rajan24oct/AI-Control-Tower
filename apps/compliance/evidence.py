"""Evidence checks: what the platform already holds that answers a requirement.

Every check reads live records for one system -- an agent, or an application
with its member agents -- and returns met, partial or missing, with the items
that support the finding and links to them. Nothing is cached: an evidence pack
reflects the platform at the moment it is produced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

MET, PARTIAL, MISSING, NOT_APPLICABLE = "met", "partial", "missing", "not_applicable"
STATUS_LABELS = {MET: "Met", PARTIAL: "Partly met", MISSING: "Missing", NOT_APPLICABLE: "Not applicable"}
STATUS_STYLES = {MET: "success", PARTIAL: "warning", MISSING: "danger", NOT_APPLICABLE: "secondary"}
EVALUATION_MAX_AGE_DAYS = 180
ACTIVITY_DAYS = 30


@dataclass
class Item:
    label: str
    url: str = ""
    when: object = None


@dataclass
class CheckResult:
    key: str
    title: str
    status: str
    finding: str
    items: list[Item] = field(default_factory=list)

    @property
    def label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def style(self) -> str:
        return STATUS_STYLES[self.status]


class Subject:
    """One governed system: an agent, or an application and its member agents."""

    def __init__(self, obj):
        from apps.agents.models import AIAgent

        self.obj = obj
        self.is_agent = isinstance(obj, AIAgent)
        self.kind = "agent" if self.is_agent else "application"
        self.organization = obj.organization

    def __str__(self) -> str:
        return self.obj.name

    @property
    def name(self) -> str:
        return self.obj.name

    @property
    def url(self) -> str:
        return reverse("inventory:agent_detail" if self.is_agent else "inventory:application_detail", args=[self.obj.pk])

    @property
    def filter(self) -> dict:
        return {"agent": self.obj} if self.is_agent else {"application": self.obj}

    @property
    def agents(self) -> list:
        return [self.obj] if self.is_agent else list(self.obj.agents.select_related("primary_model", "provider"))

    @property
    def owners(self) -> list:
        if self.is_agent:
            return [self.obj.owner] if self.obj.owner_id else []
        return [person for person in (self.obj.business_owner, self.obj.technical_owner) if person]

    @property
    def tier(self):
        return self.obj.risk_tier

    @property
    def data_classification(self) -> str:
        return self.obj.data_classification

    def classification(self):
        from apps.classification.models import CURRENT_STATUSES, RiskClassification

        return (RiskClassification.objects.filter(**self.filter, status__in=CURRENT_STATUSES)
                .order_by("-created_at").first())

    def authorization(self):
        from apps.lifecycle.models import AuthorizationStatus, DeploymentAuthorization

        authorizations = DeploymentAuthorization.objects.filter(**self.filter)
        return (authorizations.filter(status=AuthorizationStatus.AUTHORIZED).order_by("-decided_at").first()
                or authorizations.order_by("-requested_at").first())


def _url(name, *args) -> str:
    try:
        return reverse(name, args=args)
    except Exception:
        return ""


# -- The checks --------------------------------------------------------------------------


def check_classification(subject: Subject) -> CheckResult:
    from apps.classification.models import ClassificationStatus

    record = subject.classification()
    title = "Risk classification (IRGF S2)"
    if record is None:
        return CheckResult("classification", title, MISSING, "Not classified: no D1–D5 scoring or tier.")
    item = Item(f"Tier {record.tier}, {record.get_status_display().lower()}", _url("classification:detail", record.pk),
                record.countersigned_at or record.created_at)
    if record.status == ClassificationStatus.CONFIRMED and not record.rescore_due:
        return CheckResult("classification", title, MET, f"Tier {record.tier}, countersigned independently.", [item])
    reason = "due for re-scoring" if record.rescore_due else "awaiting countersignature"
    return CheckResult("classification", title, PARTIAL, f"Tier {record.tier}, {reason}.", [item])


def check_authorization(subject: Subject) -> CheckResult:
    authorization = subject.authorization()
    title = "Deployment authorization (IRGF Gate G3)"
    if authorization is None:
        return CheckResult("authorization", title, MISSING, "Never authorized at Gate G3.")
    item = Item(f"{authorization.get_environment_display()} · {authorization.get_status_display()}",
                _url("lifecycle:authorization_detail", authorization.pk), authorization.decided_at or authorization.requested_at)
    if authorization.is_in_force:
        return CheckResult("authorization", title, MET,
                           f"Authorized at Tier {authorization.tier} with its configuration recorded.", [item])
    return CheckResult("authorization", title, PARTIAL, f"Authorization {authorization.get_status_display().lower()}.", [item])


def check_risks(subject: Subject) -> CheckResult:
    from apps.risks.models import Risk

    risks = list(Risk.objects.filter(**subject.filter).order_by("-updated_at")[:10])
    title = "Risk register"
    if not risks:
        return CheckResult("risks", title, MISSING, "No risks recorded against this system.")
    items = [Item(f"{risk.title} · {risk.get_status_display()}", _url("risks:detail", risk.pk), risk.updated_at)
             for risk in risks]
    unmitigated = [risk for risk in risks if risk.is_open and not (risk.mitigation or "").strip()]
    if unmitigated:
        return CheckResult("risks", title, PARTIAL,
                           f"{len(risks)} risks recorded; {len(unmitigated)} open without a mitigation.", items)
    return CheckResult("risks", title, MET, f"{len(risks)} risks recorded, each open one with a mitigation.", items)


def check_evaluation(subject: Subject) -> CheckResult:
    from django.db.models import Q

    from apps.evaluations.models import EvaluationRun

    agents = subject.agents
    models = [agent.primary_model_id for agent in agents if agent.primary_model_id]
    if not subject.is_agent:
        models += list(subject.obj.ai_models.values_list("pk", flat=True))
    runs = (EvaluationRun.objects.filter(Q(agent__in=agents) | Q(model_id__in=models))
            .exclude(finished_at=None).order_by("-finished_at"))
    title = "Evaluation and testing"
    latest = runs.first()
    if latest is None:
        return CheckResult("evaluation", title, MISSING, "No evaluation run of the system or its models.")
    recent_pass = runs.filter(passed=True, finished_at__gte=timezone.now() - timedelta(days=EVALUATION_MAX_AGE_DAYS)).first()
    items = [Item(f"{run.suite.name} · {'passed' if run.passed else 'failed'} · {run.score or 0:.0f}%",
                  _url("evaluations:run", run.pk), run.finished_at) for run in runs.select_related("suite")[:5]]
    if recent_pass:
        return CheckResult("evaluation", title, MET,
                           f"A passing evaluation within {EVALUATION_MAX_AGE_DAYS} days.", items)
    return CheckResult("evaluation", title, PARTIAL,
                       f"Evaluated, but no pass within the last {EVALUATION_MAX_AGE_DAYS} days.", items)


def check_human_oversight(subject: Subject) -> CheckResult:
    authorization = subject.authorization()
    title = "Human oversight"
    if authorization and authorization.is_in_force and (authorization.oversight_point or "").strip():
        return CheckResult("human_oversight", title, MET, authorization.oversight_point[:300],
                           [Item("Oversight point in the G3 authorization",
                                 _url("lifecycle:authorization_detail", authorization.pk), authorization.decided_at)])
    gated = [agent for agent in subject.agents if agent.requires_human_approval]
    if gated:
        return CheckResult("human_oversight", title, PARTIAL,
                           "Human approval is required for agent actions, but no oversight point is recorded at G3.",
                           [Item(f"{agent.name}: human approval required", _url("inventory:agent_detail", agent.pk))
                            for agent in gated])
    return CheckResult("human_oversight", title, MISSING, "No oversight point and no human approval step.")


def check_kill_switch(subject: Subject) -> CheckResult:
    agents = subject.agents
    title = "Ability to stop the system (kill-switch)"
    if not agents:
        return CheckResult("kill_switch", title, MISSING, "The application has no member agents to stop.")
    with_conditions = [agent for agent in agents if (agent.kill_switch_conditions or "").strip()]
    items = [Item(f"{agent.name}: {'conditions set' if agent in with_conditions else 'no conditions'}"
                  f"{', automatic triggers' if agent.kill_switch_triggers else ''}",
                  _url("inventory:agent_detail", agent.pk)) for agent in agents]
    if len(with_conditions) == len(agents):
        return CheckResult("kill_switch", title, MET,
                           "Every agent can be suspended, with pre-approved kill-switch conditions.", items)
    return CheckResult("kill_switch", title, PARTIAL,
                       "Every agent can be suspended; not all have pre-approved kill-switch conditions.", items)


def check_documentation(subject: Subject) -> CheckResult:
    title = "Technical documentation (Agent Card)"
    gaps = {agent.name: agent.agent_card_gaps() for agent in subject.agents}
    items = [Item(f"{name}: {'complete' if not missing else f'{len(missing)} gaps'}") for name, missing in gaps.items()]
    if not subject.is_agent and not (subject.obj.business_purpose or "").strip():
        return CheckResult("documentation", title, MISSING, "The application's purpose is not documented.", items)
    if not gaps:
        return CheckResult("documentation", title, PARTIAL, "Documented, but the application has no member agents.", items)
    if all(not missing for missing in gaps.values()):
        return CheckResult("documentation", title, MET, "The Agent Card is complete for every agent.", items)
    open_gaps = sorted({gap for missing in gaps.values() for gap in missing})
    return CheckResult("documentation", title, PARTIAL, "Agent Card gaps: " + "; ".join(open_gaps[:4]) + ".", items)


def check_record_keeping(subject: Subject) -> CheckResult:
    from apps.audit.models import AuditLog
    from apps.executions.models import Execution

    since = timezone.now() - timedelta(days=ACTIVITY_DAYS)
    runs = Execution.objects.filter(**subject.filter, started_at__gte=since).count()
    audit = AuditLog.objects.filter(resource_id=str(subject.obj.pk)).count()
    title = "Record-keeping (logs)"
    items = [Item(f"{runs:,} runs logged with events in the last {ACTIVITY_DAYS} days", _url("executions:list")),
             Item(f"{audit:,} audit entries for this system", _url("audit:list"))]
    if runs:
        return CheckResult("record_keeping", title, MET,
                           "Every run is recorded with its events, model, tokens and cost; changes are audited.", items)
    return CheckResult("record_keeping", title, PARTIAL,
                       "Logging is in place, but no runs were recorded in the period.", items)


def check_transparency(subject: Subject) -> CheckResult:
    title = "Purpose and limits documented"
    if subject.is_agent:
        purpose, limits = subject.obj.business_purpose, subject.obj.out_of_scope
    else:
        purpose, limits = subject.obj.business_purpose, subject.obj.description
    if (purpose or "").strip() and (limits or "").strip():
        return CheckResult("transparency", title, MET, "Intended purpose and limits are documented.",
                           [Item("Registry record", subject.url)])
    if (purpose or "").strip():
        return CheckResult("transparency", title, PARTIAL, "Purpose documented; its limits are not.",
                           [Item("Registry record", subject.url)])
    return CheckResult("transparency", title, MISSING, "Intended purpose is not documented.")


def check_accountability(subject: Subject) -> CheckResult:
    owners = subject.owners
    title = "Named accountability"
    if owners:
        return CheckResult("accountability", title, MET,
                           "Accountable: " + ", ".join(person.get_full_name() or person.email for person in owners) + ".",
                           [Item("Registry record", subject.url)])
    return CheckResult("accountability", title, MISSING, "No named owner.")


def check_monitoring(subject: Subject) -> CheckResult:
    from apps.controlplane.models import UNRESOLVED_STATUSES, DriftRecord, Verification

    since = timezone.now() - timedelta(days=ACTIVITY_DAYS)
    agents = subject.agents
    verified = Verification.objects.filter(agent__in=agents, verified_at__gte=since).count()
    drift = DriftRecord.objects.filter(**subject.filter, status__in=UNRESOLVED_STATUSES)
    overdue = [record for record in drift if record.is_overdue]
    authorization = subject.authorization()
    commitments = bool(authorization and (authorization.monitoring_commitments or "").strip())
    title = "Monitoring in operation (Control Plane)"
    items = [Item(f"{verified:,} actions verified in the last {ACTIVITY_DAYS} days", _url("controlplane:verifications")),
             Item(f"{drift.count()} unresolved drift records, {len(overdue)} past their response window",
                  _url("controlplane:drift"))]
    if overdue:
        return CheckResult("monitoring", title, PARTIAL, "Monitored, but drift is past its response window.", items)
    if verified or commitments:
        return CheckResult("monitoring", title, MET,
                           "The Control Plane compares operation with what was authorized"
                           + (", against recorded monitoring commitments." if commitments else "."), items)
    return CheckResult("monitoring", title, MISSING, "No verified activity and no monitoring commitments.", items)


def check_change_control(subject: Subject) -> CheckResult:
    from apps.lifecycle.models import ChangeRecord, ChangeStatus

    authorization = subject.authorization()
    changes = ChangeRecord.objects.filter(**subject.filter).order_by("-created_at")
    rejected = changes.filter(status=ChangeStatus.REJECTED).count()
    awaiting = changes.filter(status=ChangeStatus.AWAITING_G4).count()
    title = "Change control (IRGF Gate G4)"
    items = [Item(f"{changes.count()} change records · {awaiting} awaiting G4 · {rejected} rejected, not reverted",
                  _url("lifecycle:changes"))]
    if not (authorization and authorization.is_in_force):
        return CheckResult("change_control", title, MISSING,
                           "No authorized configuration to compare changes against.", items)
    if rejected:
        return CheckResult("change_control", title, PARTIAL, "A change rejected at G4 has not been reverted.", items)
    return CheckResult("change_control", title, MET, "Every change is compared with the authorized configuration "
                       "and classified; material changes go through G4.", items)


def check_incidents(subject: Subject) -> CheckResult:
    from apps.controlplane.models import UNRESOLVED_STATUSES, DriftRecord

    incidents = DriftRecord.objects.filter(**subject.filter, is_incident=True)
    open_incidents = incidents.filter(status__in=UNRESOLVED_STATUSES)
    title = "Incident handling"
    items = [Item(record.title,
                  _url("controlplane:drift_detail", record.pk), record.first_seen_at) for record in incidents[:5]]
    if open_incidents.exists():
        return CheckResult("incidents", title, MISSING, f"{open_incidents.count()} incident(s) unresolved.", items)
    if incidents.exists():
        return CheckResult("incidents", title, MET, "Every incident has been resolved.", items)
    return CheckResult("incidents", title, MET, "No incidents recorded; boundary breaches are raised automatically.", items)


def check_data_governance(subject: Subject) -> CheckResult:
    title = "Data governance"
    classification = subject.data_classification
    sources = [source for agent in subject.agents for source in (agent.knowledge_sources or [])]
    items = [Item(f"Data classification: {classification or 'not set'}")] + [Item(f"Source: {s}") for s in sources[:5]]
    if classification and sources:
        return CheckResult("data_governance", title, MET, "Data classified, with its grounding sources named.", items)
    if classification:
        return CheckResult("data_governance", title, PARTIAL, "Data classified; grounding sources are not named.", items)
    return CheckResult("data_governance", title, MISSING, "Data is not classified.", items)


def check_policies(subject: Subject) -> CheckResult:
    from apps.core.constants import LifecycleStatus
    from apps.policies.models import Policy

    active = Policy.objects.for_organization(subject.organization).filter(status=LifecycleStatus.ACTIVE)
    title = "Policies enforced"
    if active.exists():
        return CheckResult("policies", title, MET,
                           f"{active.count()} active control policies, enforced in the AI, MCP and A2A gateways.",
                           [Item(policy.name, _url("policies:detail", policy.pk)) for policy in active[:6]])
    return CheckResult("policies", title, MISSING, "No active control policies.")


def check_third_party(subject: Subject) -> CheckResult:
    title = "Third-party models and providers"
    concerns, items = [], []
    for agent in subject.agents:
        if agent.provider_id:
            items.append(Item(f"Provider {agent.provider.name}: {'approved' if agent.provider.is_approved else 'not approved'}"))
            if not agent.provider.is_approved:
                concerns.append(agent.provider.name)
        if agent.primary_model_id:
            items.append(Item(f"Model {agent.primary_model.model_name}: "
                              f"{'approved' if agent.primary_model.approved_for_use else 'not approved'}"))
            if not agent.primary_model.approved_for_use:
                concerns.append(agent.primary_model.model_name)
    if not items:
        return CheckResult("third_party", title, MISSING, "No provider or model recorded.")
    if concerns:
        return CheckResult("third_party", title, PARTIAL, "Not approved: " + ", ".join(sorted(set(concerns))) + ".", items)
    return CheckResult("third_party", title, MET, "Every provider and model in use is approved.", items)


CHECKS = {
    "classification": check_classification,
    "authorization": check_authorization,
    "risks": check_risks,
    "evaluation": check_evaluation,
    "human_oversight": check_human_oversight,
    "kill_switch": check_kill_switch,
    "documentation": check_documentation,
    "record_keeping": check_record_keeping,
    "transparency": check_transparency,
    "accountability": check_accountability,
    "monitoring": check_monitoring,
    "change_control": check_change_control,
    "incidents": check_incidents,
    "data_governance": check_data_governance,
    "policies": check_policies,
    "third_party": check_third_party,
}
