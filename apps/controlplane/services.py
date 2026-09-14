"""The Control Plane (IRGF M12): observe, compare, record drift (M13), verify (link 07).

Observe: MCP tool calls as the gateway records them, executions as they are
reported, evaluation runs, budget alerts, policy edits and tool definitions.
Compare: each against the configuration Gate G3 authorized for that agent.
Record: every divergence becomes one drift record, kept as one while it stays
unresolved, routed and timed by category and tier. Enforce: only the
kill-switch triggers the agent pre-approved -- the one correction IRGF lets a
machine make at every tier. Everything else is for a person to decide: revert,
ratify (Gate G4), except (M17) or escalate (S2, possibly G3).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from statistics import mean

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Perm
from apps.audit.models import AuditAction
from apps.audit.services import AuditService
from apps.controlplane import irgf, limits
from apps.controlplane.models import (
    UNRESOLVED_STATUSES,
    DriftRecord,
    DriftStatus,
    Resolution,
    Verification,
    VerificationOutcome,
)
from apps.core.constants import Environment

logger = logging.getLogger(__name__)

#: How many pieces of evidence a drift record keeps; the count keeps going.
EVIDENCE_KEPT = 20
TERMINAL_EXECUTION_STATUSES = ("completed", "failed", "cancelled", "timed_out")
REACHED_CALL_STATUSES = ("succeeded", "tool_error")
#: A quiet-route record waits this long for the digest.
DIGEST_AFTER = timedelta(hours=24)


class ControlPlaneError(ValueError):
    """A resolution the IRGF rules do not allow."""


def _person(actor):
    if actor is None or getattr(actor, "is_service_account", False) or not getattr(actor, "pk", None):
        return None
    return actor


def _jsonable(value):
    return json.loads(json.dumps(value, default=str)) if value is not None else None


def _merge(existing, new):
    """Observed values accumulate: every tool or model seen, not only the last."""
    if isinstance(existing, list) and isinstance(new, list):
        return existing + [item for item in new if item not in existing]
    return new if new is not None else existing


def _audit(action, record, *, actor=None, new_value=None, metadata=None):
    AuditService.log(
        action=action,
        resource_type=record._meta.label,
        resource_id=record.pk,
        resource_label=str(record)[:255],
        organization=record.organization,
        actor=_person(actor),
        new_value=_jsonable(new_value),
        metadata={"irgf": f"{irgf.DRIFT} · {record.category_info.label}", **(metadata or {})},
    )


# -- Comparing running state with the authorized state -----------------------------


@dataclass
class Finding:
    signal: str
    key: str
    title: str
    detail: str = ""
    expected: object = None
    observed: object = None

    def as_entry(self) -> dict:
        return {"signal": self.signal, "title": self.title}


@dataclass
class Baseline:
    """A system -- an agent, or an application -- and the authorization it is measured against."""

    subject: object
    authorization: object | None
    configuration: dict

    @property
    def agent(self):
        """The subject, by the name the agent checks use."""
        return self.subject

    @property
    def triggers(self) -> set[str]:
        # The stricter of the two: a trigger added since G3 already protects,
        # and one removed since G3 keeps protecting until G4 approves the removal.
        return set(getattr(self.subject, "kill_switch_triggers", None) or []) | set(
            self.configuration.get("kill_switch_triggers") or []
        )


class Baselines(dict):
    """Each system's authorization, looked up once per run."""

    def for_subject(self, subject) -> Baseline:
        if subject.pk not in self:
            from apps.lifecycle.services import AuthorizationService

            authorization = AuthorizationService.baseline(subject)
            configuration = (authorization.configuration if authorization else None) or {}
            self[subject.pk] = Baseline(subject, authorization, configuration)
        return self[subject.pk]

    for_agent = for_subject


def _qualified(server_slug: str, tool_name: str) -> str:
    return f"{server_slug}__{tool_name}" if server_slug else tool_name


def within_boundary(configuration: dict, server_slug: str, tool_name: str) -> bool:
    """Link 04: the tool is on the allow-list, or inside one of the MCP grants."""
    allowed = set(configuration.get("allowed_tools") or [])
    if tool_name in allowed or _qualified(server_slug, tool_name) in allowed:
        return True
    return any(
        grant.get("server") == server_slug and (not grant.get("tools") or tool_name in grant["tools"])
        for grant in configuration.get("mcp_grants") or []
    )


def is_restricted(configuration: dict, server_slug: str, tool_name: str) -> bool:
    restricted = set(configuration.get("restricted_tools") or [])
    return tool_name in restricted or _qualified(server_slug, tool_name) in restricted


def deployment_findings(baseline: Baseline, environment: str) -> list[Finding]:
    """Structural drift: running where no authorization covers it."""
    agent, authorization = baseline.agent, baseline.authorization
    if environment != Environment.PRODUCTION:
        return []
    if authorization is None:
        return [Finding(
            "unauthorized_deployment", Environment.PRODUCTION,
            f"{agent.name} is running in production with no deployment authorization.",
            "Gate G3 authorizes a configuration for an environment; nothing was authorized for production.",
            expected="An authorization in force for production (IRGF S7 · Gate G3)",
            observed="Production traffic",
        )]
    findings = []
    if authorization.environment != Environment.PRODUCTION:
        findings.append(Finding(
            "environment_mismatch", Environment.PRODUCTION,
            f"{agent.name} is authorized for {authorization.get_environment_display().lower()} "
            "but is running in production.",
            expected=authorization.environment, observed=Environment.PRODUCTION,
        ))
    if authorization.status == "suspended":
        findings.append(Finding(
            "acted_while_suspended", "suspended",
            f"{agent.name} kept acting while its deployment authorization was suspended.",
            authorization.suspension_reason,
            expected="No traffic until the suspension is lifted", observed="Traffic",
        ))
    return findings


def model_findings(baseline: Baseline, model) -> list[Finding]:
    if model is None or baseline.authorization is None:
        return []
    refs = [ref for ref in (baseline.configuration.get("primary_model"),
                            baseline.configuration.get("fallback_model")) if ref]
    if not refs or str(model.pk) in {ref.get("id") for ref in refs}:
        return []
    return [Finding(
        "unauthorized_model", "models",
        f"{baseline.agent.name} was served by {model.model_name}, which its authorization does not include.",
        expected=[ref.get("name") or ref.get("model_id") for ref in refs], observed=[model.model_name],
    )]


def identity_findings(baseline: Baseline, account) -> list[Finding]:
    if account is None or baseline.authorization is None:
        return []
    identities = baseline.configuration.get("identities") or []
    if not identities or account.name in identities:
        return []
    return [Finding(
        "unbound_identity", "identities",
        f"{baseline.agent.name} acted under '{account.name}', which was not bound to it at authorization.",
        expected=identities, observed=[account.name],
    )]


def tool_findings(baseline: Baseline, server_slug: str, tool_name: str, *, server=None, tool=None,
                  arguments=None) -> list[Finding]:
    """Links 04 and 06: the boundary, the data behind it, and the limits on it."""
    if baseline.authorization is None:
        return []
    configuration = baseline.configuration
    qualified = _qualified(server_slug, tool_name)
    findings = []
    if is_restricted(configuration, server_slug, tool_name):
        findings.append(Finding(
            "restricted_tool", "restricted",
            f"{baseline.agent.name} reached tools its authorization restricts.",
            expected=configuration.get("restricted_tools") or [], observed=[qualified],
        ))
    elif not within_boundary(configuration, server_slug, tool_name):
        findings.append(Finding(
            "boundary_divergence", "tools",
            f"{baseline.agent.name} reached tools outside its authorized boundary.",
            "Neither the tool allow-list (link 04) nor an MCP access grant covers them.",
            expected={
                "allowed_tools": configuration.get("allowed_tools") or [],
                "mcp_grants": [grant.get("server") for grant in configuration.get("mcp_grants") or []],
            },
            observed=[qualified],
        ))
    if server is not None:
        authorized = irgf.SENSITIVITY.get(configuration.get("data_classification") or "")
        reached = irgf.SENSITIVITY.get(server.data_classification or "")
        if authorized is not None and reached is not None and reached > authorized:
            findings.append(Finding(
                "data_above_classification", server.slug,
                f"{baseline.agent.name} reached {server.name} ({server.data_classification} data); it is "
                f"authorized for {configuration.get('data_classification')} data.",
                expected=configuration.get("data_classification"), observed=server.data_classification,
            ))
    if tool is not None:
        breach = limits.exceeded(baseline.agent, tool, arguments)
        if breach is not None:
            findings.append(Finding(
                "action_limit_exceeded", breach.limit, f"{qualified}: {breach.reason}",
                expected={breach.limit: (baseline.agent.action_limits or {}).get(breach.limit)},
                observed=[qualified],
            ))
    return findings


def system_findings(baseline: Baseline, execution) -> list[Finding]:
    """An application's run against the application's authorized configuration."""
    application, authorization = baseline.subject, baseline.authorization
    if authorization is None:
        return []
    configuration = baseline.configuration
    findings = []
    members = configuration.get("agents") or []
    if execution.agent_id and str(execution.agent_id) not in {ref.get("id") for ref in members}:
        findings.append(Finding(
            "agent_outside_system", "agents",
            f"{application.name} ran {execution.agent.name}, which its authorization does not include.",
            "Its composite authority was scored and authorized without this agent (IRGF chapter 16).",
            expected=[ref.get("name") for ref in members], observed=[execution.agent.name],
        ))
    models = configuration.get("models") or []
    if execution.model_id and models and str(execution.model_id) not in {ref.get("id") for ref in models}:
        findings.append(Finding(
            "unauthorized_model", "models",
            f"{application.name} was served by {execution.model.model_name}, which its authorization does not include.",
            expected=[ref.get("name") for ref in models], observed=[execution.model.model_name],
        ))
    return findings


def delegation_findings(message, authorizations) -> tuple[list, bool]:
    """IRGF chapter 16: one agent handing work to another is a delegation.

    Returns (authorization, finding) pairs for every authorized application whose
    declared graph the message falls outside, and whether any graph covers it.
    """
    edge = (message.sender.agent_id, message.recipient.agent_id)
    if any(
        edge in {(item["from"], item["to"]) for item in authorization.configuration.get("delegations") or []}
        for authorization in authorizations
    ):
        return [], True
    findings = []
    for authorization in authorizations:
        members = {ref.get("agent_id") for ref in authorization.configuration.get("agents") or []}
        if not members & set(edge):
            continue
        declared = [f"{item['from']} → {item['to']}" for item in authorization.configuration.get("delegations") or []]
        findings.append((authorization, Finding(
            "undeclared_delegation", f"{edge[0]}->{edge[1]}",
            f"{message.sender.name} delegated work to {message.recipient.name}; "
            f"{authorization.application.name}'s authorization declares no such delegation.",
            "Which agents may invoke which is declared at Gate G3, not left to emerge from traffic.",
            expected=declared or "No delegations declared",
            observed=[f"{edge[0]} → {edge[1]}"],
        )))
    return findings, False


# -- Drift records (M13) ------------------------------------------------------------


def _owner(record):
    if record.agent_id:
        return record.agent.owner
    application = record.application
    return application.technical_owner or application.business_owner


def _owner_ids(record) -> set:
    if record.agent_id:
        return {record.agent.owner_id} - {None}
    return {record.application.business_owner_id, record.application.technical_owner_id} - {None}


def _drift_recipients(record, *, governance: bool = False) -> list:
    from apps.notifications.models import NotificationCategory
    from apps.notifications.services import NotificationService

    people = [person for person in (_owner(record),) if person]
    people += NotificationService.recipients_for(record.organization, NotificationCategory.DRIFT)
    if governance:
        people += NotificationService.recipients_for(record.organization, NotificationCategory.GATE_DECISION)
    unique = {}
    for person in people:
        unique.setdefault(person.pk, person)
    return list(unique.values())


class DriftService:
    @classmethod
    def record(cls, subject, finding: Finding, *, tier=None, authorization=None, evidence=None,
               at=None, once: bool = False, triggers=()) -> tuple[DriftRecord | None, bool]:
        """Record a divergence, or add it to the unresolved record for the same one.

        `once` is for observations that can be seen again on every sweep -- an
        evaluation run, a policy edit, a budget alert: each is recorded once and
        never again, even after its record is resolved.
        """
        from apps.agents.models import AIAgent

        signal = irgf.SIGNALS[finding.signal]
        is_agent = isinstance(subject, AIAgent)
        fingerprint = f"{finding.signal}:{subject._meta.model_name}:{subject.pk}:{finding.key}"[:255]
        at = at or timezone.now()
        evidence = [_jsonable(item) for item in evidence or []]

        with transaction.atomic():
            records = DriftRecord.objects.select_for_update().filter(fingerprint=fingerprint)
            if once and records.exists():
                return None, False
            existing = records.filter(status__in=UNRESOLVED_STATUSES).first()
            if existing is not None:
                known = {item.get("id") for item in existing.evidence}
                fresh = [item for item in evidence if item.get("id") not in known]
                if evidence and not fresh:
                    return existing, False
                existing.occurrences += 1
                existing.last_seen_at = max(existing.last_seen_at, at)
                existing.observed = _merge(existing.observed, _jsonable(finding.observed))
                existing.evidence = (fresh + existing.evidence)[:EVIDENCE_KEPT]
                existing.save(update_fields=["occurrences", "last_seen_at", "observed", "evidence", "updated_at"])
                record, created = existing, False
            else:
                previous = records.filter(status=DriftStatus.RESOLVED).order_by("-resolved_at").first()
                response = irgf.response_for(signal.category, tier, incident=signal.incident)
                record = DriftRecord.objects.create(
                    organization=subject.organization,
                    agent=subject if is_agent else None,
                    application=None if is_agent else subject,
                    authorization=authorization,
                    signal=signal.key,
                    category=signal.category,
                    is_incident=signal.incident,
                    tier=tier,
                    title=finding.title[:300],
                    detail=finding.detail,
                    expected=_jsonable(finding.expected),
                    observed=_jsonable(finding.observed),
                    evidence=evidence[:EVIDENCE_KEPT],
                    fingerprint=fingerprint,
                    first_seen_at=at,
                    last_seen_at=at,
                    route=response.route,
                    response_due=irgf.due_at(response, timezone.now()),
                    recurrence_of=previous,
                )
                created = True

        if created:
            _audit(AuditAction.DRIFT_DETECTED, record, new_value={
                "signal": record.signal, "tier": record.tier, "incident": record.is_incident,
                "route": record.route, "due": record.response_due,
            })
            cls._respond(record)
        cls._enforce(record, triggers)
        return record, created

    # -- What happens on its own ----------------------------------------------------

    @classmethod
    def _respond(cls, record: DriftRecord) -> None:
        response = record.response
        if response.action in (irgf.RESCORE, irgf.RESCORE_G4):
            from apps.classification.services import ClassificationService

            if ClassificationService.flag_rescore(record.subject, f"Behavioral drift: {record.title}"):
                record.automatic_action = irgf.RESCORE
                record.save(update_fields=["automatic_action", "updated_at"])
        if not response.is_quiet:
            cls._notify_new(record)

    @classmethod
    def _enforce(cls, record: DriftRecord, triggers) -> None:
        """Pull the kill-switch for a trigger the agent pre-approved at Gate G3 (link 06)."""
        signal = record.signal_info
        agent = record.agent
        if agent is None or not signal or not signal.trigger or signal.trigger not in set(triggers):
            return
        agent.refresh_from_db(fields=["suspended_at"])
        if agent.is_suspended:
            return
        from apps.agents.models import KILL_SWITCH_TRIGGERS
        from apps.agents.services import KillSwitch

        KillSwitch.suspend(
            agent, actor=None,
            reason=f"Automatic kill-switch: {KILL_SWITCH_TRIGGERS[signal.trigger].lower()}. {record.title}",
        )
        record.auto_suspended = True
        record.automatic_action = "auto_suspend"
        record.save(update_fields=["auto_suspended", "automatic_action", "updated_at"])

    @staticmethod
    def _notify_new(record: DriftRecord) -> None:
        from apps.notifications import emitters
        from apps.notifications.models import NotificationSeverity

        response = record.response
        urgent = record.is_incident or response.route == irgf.IMMEDIATE
        due = f" Respond by {timezone.localtime(record.response_due):%d %b %H:%M}." if record.response_due else ""
        emitters.drift_notice(
            record,
            title=(f"Incident: {record.title}" if record.is_incident else f"Drift: {record.title}")[:200],
            body=(f"{record.category_info.label} drift at Tier {record.triage_tier}: "
                  f"{response.label}.{due}\n\n{record.detail}").strip(),
            recipients=_drift_recipients(record, governance=urgent),
            severity=NotificationSeverity.CRITICAL if urgent else NotificationSeverity.WARNING,
            dedupe_key=f"drift:{record.pk}:{record.first_seen_at:%Y%m%d%H%M%S}",
        )
        record.notified_at = timezone.now()
        record.save(update_fields=["notified_at", "updated_at"])

    @classmethod
    def escalate_overdue(cls, organization=None, *, now=None) -> int:
        """A missed response window goes to the AI Governance Body."""
        from apps.notifications import emitters
        from apps.notifications.models import NotificationCategory, NotificationSeverity
        from apps.notifications.services import NotificationService

        now = now or timezone.now()
        records = DriftRecord.objects.filter(
            status=DriftStatus.OPEN, response_due__lt=now, escalated_at__isnull=True
        ).select_related("agent", "application", "organization")
        if organization is not None:
            records = records.filter(organization=organization)
        count = 0
        for record in records:
            record.escalated_at = now
            record.save(update_fields=["escalated_at", "updated_at"])
            _audit(AuditAction.DRIFT_ESCALATED, record, new_value={"due": record.response_due})
            recipients = NotificationService.recipients_for(record.organization, NotificationCategory.GATE_DECISION)
            owner = _owner(record)
            if owner and owner.pk not in {person.pk for person in recipients}:
                recipients.append(owner)
            emitters.drift_notice(
                record, title=f"Overdue drift: {record.title}"[:200],
                body=(f"The response window closed {timezone.localtime(record.response_due):%d %b %H:%M} "
                      f"({record.response.label}). Revert, ratify, except or escalate it."),
                recipients=recipients, severity=NotificationSeverity.CRITICAL,
                category=NotificationCategory.GATE_DECISION, dedupe_key=f"drift_overdue:{record.pk}",
            )
            count += 1
        return count

    @classmethod
    def send_digests(cls, organization=None, *, now=None) -> int:
        """Quiet routes: one digest per owner, and one for the risk function."""
        from apps.notifications import emitters
        from apps.notifications.models import NotificationCategory, NotificationSeverity
        from apps.notifications.services import NotificationService

        now = now or timezone.now()
        waiting = DriftRecord.objects.filter(
            status=DriftStatus.OPEN, route__in=irgf.QUIET_ROUTES, notified_at__isnull=True
        ).select_related("agent__owner", "application", "organization")
        if organization is not None:
            waiting = waiting.filter(organization=organization)
        if not waiting.filter(first_seen_at__lte=now - DIGEST_AFTER).exists():
            return 0

        batches: dict = {}
        for record in waiting:
            if record.route == irgf.DIGEST:
                owner = _owner(record)
                recipients = [owner] if owner else []
            else:
                recipients = NotificationService.recipients_for(record.organization, NotificationCategory.DRIFT)
            for person in recipients:
                batches.setdefault((record.organization_id, person.pk), (record.organization, person, []))[2].append(record)

        sent = 0
        for organization_, person, records in batches.values():
            lines = "\n".join(f"· {record.subject}: {record.title}" for record in records[:15])
            emitters.digest_notice(
                organization_, recipient=person,
                title=f"Drift digest: {len(records)} finding{'s' if len(records) != 1 else ''} to review",
                body=f"{lines}\n\nFrom the Control Plane (IRGF M12); no response window applies.",
                category=NotificationCategory.DRIFT, severity=NotificationSeverity.INFO,
                url=reverse("controlplane:drift") + "?route=quiet",
                dedupe_key=f"drift_digest:{person.pk}:{now:%Y%m%d}",
            )
            sent += 1
        waiting.update(notified_at=now)
        return sent

    # -- Resolution: revert, ratify, except, escalate --------------------------------

    @staticmethod
    def can_triage(user, record) -> bool:
        """Revert and ratify: the system owner, the risk function or the governance body."""
        person = _person(user)
        if person is None:
            return False
        organization = record.organization
        return (
            person.pk in _owner_ids(record)
            or person.has_org_permission(organization, Perm.RISK_MANAGE)
            or person.has_org_permission(organization, Perm.GATE_DECIDE)
        )

    @staticmethod
    def can_escalate(user, record) -> bool:
        """Except and escalate: the risk function or the governance body."""
        person = _person(user)
        return person is not None and (
            person.has_org_permission(record.organization, Perm.RISK_MANAGE)
            or person.has_org_permission(record.organization, Perm.GATE_DECIDE)
        )

    @staticmethod
    def _require_open(record):
        if record.status != DriftStatus.OPEN:
            raise ControlPlaneError("Only an open drift record can be resolved.")

    @classmethod
    def _resolve(cls, record, resolution, *, actor=None, note=""):
        record.status = DriftStatus.RESOLVED
        record.resolution = resolution
        record.resolution_note = note
        record.resolved_by = _person(actor)
        record.resolved_at = timezone.now()
        record.save()
        _audit(AuditAction.DRIFT_RESOLVED, record, actor=actor,
               new_value={"resolution": resolution, "note": note})
        return record

    @classmethod
    @transaction.atomic
    def revert(cls, record, *, actor, note) -> DriftRecord:
        """The divergence was not meant, and the approved state is still right."""
        cls._require_open(record)
        if not cls.can_triage(actor, record):
            raise ControlPlaneError("Reverting needs the system owner, the risk function or the governance body.")
        if not (note or "").strip():
            raise ControlPlaneError("Say what was put back, so a recurrence can be told apart.")
        return cls._resolve(record, Resolution.REVERT, actor=actor, note=note.strip())

    @classmethod
    @transaction.atomic
    def ratify(cls, record, *, actor, note="", change=None) -> DriftRecord:
        """The divergence is an improvement: make it the approved state."""
        from apps.lifecycle.models import OPEN_CHANGE_STATUSES, ChangeStatus

        cls._require_open(record)
        if not cls.can_triage(actor, record):
            raise ControlPlaneError("Ratifying needs the system owner, the risk function or the governance body.")
        how = record.signal_info.ratify if record.signal_info else irgf.RATIFY_BY_NOTE
        if how == irgf.RATIFY_BY_AUTHORIZATION:
            raise ControlPlaneError(
                "Ratify this by authorizing the agent at Gate G3: the record closes by itself once an "
                "authorization for production is in force."
            )
        if how == irgf.RATIFY_BY_CHANGE:
            if change is None:
                raise ControlPlaneError(
                    "Record the new state on the agent first -- the edit becomes a change record that Gate G4 "
                    "classifies and decides -- then choose that change here."
                )
            if (change.agent_id, change.application_id) != (record.agent_id, record.application_id):
                raise ControlPlaneError("The change must be to the same system.")
            record.change_record = change
            record.resolution_note = (note or "").strip()
            if change.status in (ChangeStatus.APPROVED, ChangeStatus.ACCEPTED):
                return cls._resolve(record, Resolution.RATIFY, actor=actor,
                                    note=record.resolution_note or f"Ratified by {change.change_label}.")
            if change.status not in OPEN_CHANGE_STATUSES:
                raise ControlPlaneError("That change was rejected, reverted or superseded.")
            record.status = DriftStatus.RATIFYING
            record.save()
            _audit(AuditAction.UPDATE, record, actor=actor,
                   new_value={"status": record.status, "change": str(change.pk)})
            return record
        if not (note or "").strip():
            raise ControlPlaneError("Say why the new state is right; the note is the ratification.")
        return cls._resolve(record, Resolution.RATIFY, actor=actor, note=note.strip())

    @classmethod
    @transaction.atomic
    def except_(cls, record, *, actor, justification, compensating_control="", duration_days) -> DriftRecord:
        """The divergence must stay for now: a time-boxed exception under M17."""
        from apps.lifecycle.services import ExceptionService, LifecycleError

        cls._require_open(record)
        if not cls.can_escalate(actor, record):
            raise ControlPlaneError("Excepting needs the risk function or the governance body.")
        if record.agent_id is None:
            raise ControlPlaneError("Exceptions are granted to agents; resolve this one another way.")
        try:
            exception = ExceptionService.request(
                record.agent, None, actor=actor, justification=justification,
                compensating_control=compensating_control, duration_days=duration_days,
                divergence=f"{record.category_info.label} drift: {record.title}",
            )
        except LifecycleError as exc:
            raise ControlPlaneError(str(exc)) from exc
        record.exception = exception
        record.status = DriftStatus.EXCEPTING
        record.save()
        _audit(AuditAction.UPDATE, record, actor=actor,
               new_value={"status": record.status, "exception": str(exception.pk)})
        return record

    @classmethod
    @transaction.atomic
    def escalate(cls, record, *, actor, reason, suspend_authorization: bool = False) -> DriftRecord:
        """The tier or the approval was wrong: back to S2, and possibly Gate G3."""
        from apps.classification.services import ClassificationService
        from apps.lifecycle.services import AuthorizationService

        cls._require_open(record)
        if not cls.can_escalate(actor, record):
            raise ControlPlaneError("Escalating needs the risk function or the governance body.")
        if not (reason or "").strip():
            raise ControlPlaneError("Say why the tier or the approval looks wrong.")
        ClassificationService.flag_rescore(record.subject, f"Drift escalated: {record.title}", actor=actor)
        note = reason.strip()
        if suspend_authorization:
            authorization = AuthorizationService.in_force(record.subject)
            if authorization is not None:
                AuthorizationService.suspend(
                    authorization, reason=f"Escalated from drift ({record.title}): {note}", for_changes=False
                )
                note += " The deployment authorization is suspended until the agent is authorized again (G3)."
        return cls._resolve(record, Resolution.ESCALATE, actor=actor, note=note)

    # -- Keeping linked records in step --------------------------------------------------

    @classmethod
    def _reopen(cls, record, why: str) -> None:
        record.status = DriftStatus.OPEN
        record.response_due = irgf.due_at(record.response, timezone.now())
        record.escalated_at = None
        record.notified_at = None
        record.resolution_note = why
        record.save()
        _audit(AuditAction.UPDATE, record, new_value={"status": record.status, "why": why})
        if not record.response.is_quiet:
            cls._notify_new(record)

    @classmethod
    def reconcile(cls, organization=None, *, records=None) -> int:
        """Ratifying and excepting records follow the change or exception they wait on."""
        from apps.lifecycle.models import ChangeStatus, ExceptionStatus

        if records is None:
            records = DriftRecord.objects.filter(
                status__in=(DriftStatus.RATIFYING, DriftStatus.EXCEPTING, DriftStatus.EXCEPTED)
            )
            if organization is not None:
                records = records.filter(organization=organization)
        moved = 0
        for record in records.select_related("change_record", "exception", "agent", "application"):
            change, exception = record.change_record, record.exception
            if record.status == DriftStatus.RATIFYING and change is not None:
                if change.status in (ChangeStatus.APPROVED, ChangeStatus.ACCEPTED):
                    cls._resolve(record, Resolution.RATIFY,
                                 note=record.resolution_note or f"Ratified: Gate G4 approved {change.change_label}.")
                    moved += 1
                elif change.status in (ChangeStatus.REJECTED, ChangeStatus.REVERTED, ChangeStatus.SUPERSEDED):
                    cls._reopen(record, f"The change it waited on was {change.get_status_display().lower()}.")
                    moved += 1
            elif record.status == DriftStatus.EXCEPTING and exception is not None:
                if exception.status == ExceptionStatus.ACTIVE:
                    record.status = DriftStatus.EXCEPTED
                    record.save(update_fields=["status", "updated_at"])
                    moved += 1
                elif exception.status != ExceptionStatus.PENDING:
                    cls._reopen(record, f"The exception was {exception.get_status_display().lower()}.")
                    moved += 1
            elif record.status == DriftStatus.EXCEPTED and exception is not None and not exception.is_active:
                cls._reopen(record, "Its exception ended; the divergence needs a decision again.")
                moved += 1
        return moved

    @classmethod
    def settle_structural(cls, organization=None) -> int:
        """Running without authorization closes once Gate G3 authorizes production."""
        from apps.lifecycle.services import AuthorizationService

        records = DriftRecord.objects.filter(
            status=DriftStatus.OPEN, signal__in=("unauthorized_deployment", "environment_mismatch"),
        ).select_related("agent", "application")
        if organization is not None:
            records = records.filter(organization=organization)
        settled = 0
        for record in records:
            authorization = AuthorizationService.in_force(record.subject, Environment.PRODUCTION)
            if authorization is not None:
                cls._resolve(record, Resolution.RATIFY,
                             note=f"Authorized for production at Gate G3 on {authorization.decided_at:%d %b %Y}.")
                settled += 1
        return settled


# -- Verification (link 07) and observation (M12) ------------------------------------


class ControlPlane:
    @staticmethod
    def _save_verification(**fields) -> Verification | None:
        try:
            with transaction.atomic():
                return Verification.objects.create(**fields)
        except IntegrityError:
            return None  # verified concurrently

    @staticmethod
    def _timing(agent, recorded_at, now) -> dict:
        tier = agent.risk_tier if agent is not None else None
        ceiling = irgf.verification_ceiling(tier)
        latency_ms = max(0, int((now - recorded_at).total_seconds() * 1000))
        return {
            "tier": tier, "recorded_at": recorded_at, "verified_at": now, "latency_ms": latency_ms,
            "ceiling_seconds": ceiling, "late": latency_ms > ceiling * 1000,
        }

    @classmethod
    def _record_findings(cls, baseline, findings, *, evidence, at) -> None:
        for finding in findings:
            DriftService.record(
                baseline.agent, finding, tier=baseline.agent.risk_tier,
                authorization=baseline.authorization, evidence=[evidence], at=at,
                triggers=baseline.triggers,
            )

    @classmethod
    def verify_tool_call(cls, call, *, baselines=None, now=None) -> Verification | None:
        """Check one MCP tool call against the calling agent's authorized state."""
        if Verification.objects.filter(tool_call=call).exists():
            return None
        now = now or timezone.now()
        agent, findings, baseline = call.agent, [], None
        if agent is not None:
            baseline = (baselines or Baselines()).for_agent(agent)
            environment = (call.execution.environment if call.execution_id else "") or agent.environment
            if call.limit_breached:
                findings.append(Finding(
                    "action_limit_breach", call.limit_breached,
                    f"{agent.name} attempted an action beyond its {limits.LIMIT_LABELS.get(call.limit_breached, call.limit_breached).lower()} limit.",
                    call.reason,
                    expected={call.limit_breached: (agent.action_limits or {}).get(call.limit_breached)},
                    observed=[{"tool": call.tool_name, "value": call.action_value, "counterparty": call.counterparty}],
                ))
            elif call.status in REACHED_CALL_STATUSES:
                tool = call.tool
                findings += deployment_findings(baseline, environment)
                findings += tool_findings(baseline, call.server.slug, tool.name if tool else call.tool_name,
                                          server=call.server, tool=tool, arguments=call.arguments)
                findings += identity_findings(baseline, call.service_account)
        outcome = (
            VerificationOutcome.DIVERGES if findings
            else VerificationOutcome.CONFORMS if baseline is not None and baseline.authorization is not None
            else VerificationOutcome.UNGOVERNED
        )
        verification = cls._save_verification(
            organization=call.organization, tool_call=call, agent=agent, acted_at=call.created_at,
            outcome=outcome, findings=[finding.as_entry() for finding in findings],
            **cls._timing(agent, call.created_at, now),
        )
        if verification is not None and findings:
            cls._record_findings(baseline, findings, at=call.created_at, evidence={
                "kind": "tool_call", "id": str(call.pk), "at": call.created_at, "label": call.tool_name,
            })
        return verification

    @classmethod
    def verify_tool_call_by_id(cls, pk) -> None:
        """For the post-save hook: never let verification break the caller."""
        from apps.mcp_gateway.models import MCPToolCall

        try:
            call = MCPToolCall.objects.select_related(
                "agent", "server", "tool", "service_account", "execution"
            ).filter(pk=pk).first()
            if call is not None:
                cls.verify_tool_call(call)
        except Exception:
            logger.exception("Control Plane could not verify MCP tool call %s", pk)

    @classmethod
    def verify_message(cls, message, *, now=None) -> Verification | None:
        """Check one agent-to-agent message against the declared delegation graphs."""
        from apps.gateway.models import GatewayMessage
        from apps.lifecycle.models import BASELINE_STATUSES, DeploymentAuthorization

        if Verification.objects.filter(message=message).exists():
            return None
        now = now or timezone.now()
        findings, governed = [], False
        if message.status != GatewayMessage.Status.BLOCKED:
            authorizations = list(
                DeploymentAuthorization.objects.filter(
                    organization=message.organization, application__isnull=False,
                    status__in=BASELINE_STATUSES,
                ).select_related("application")
            )
            findings, covered = delegation_findings(message, authorizations)
            governed = covered or bool(findings)
        outcome = (
            VerificationOutcome.DIVERGES if findings
            else VerificationOutcome.CONFORMS if governed
            else VerificationOutcome.UNGOVERNED
        )
        verification = cls._save_verification(
            organization=message.organization, message=message, agent=message.sender,
            acted_at=message.created_at, outcome=outcome,
            findings=[finding.as_entry() for _, finding in findings],
            **cls._timing(message.sender, message.created_at, now),
        )
        if verification is not None:
            evidence = {
                "kind": "message", "id": str(message.pk), "at": message.created_at,
                "label": f"{message.sender.agent_id} → {message.recipient.agent_id}",
            }
            for authorization, finding in findings:
                application = authorization.application
                DriftService.record(
                    application, finding, tier=application.risk_tier, authorization=authorization,
                    evidence=[evidence], at=message.created_at,
                )
        return verification

    @classmethod
    def verify_message_by_id(cls, pk) -> None:
        from apps.gateway.models import GatewayMessage

        try:
            message = GatewayMessage.objects.select_related("sender", "recipient", "organization").filter(pk=pk).first()
            if message is not None:
                cls.verify_message(message)
        except Exception:
            logger.exception("Control Plane could not verify agent message %s", pk)

    @classmethod
    def verify_execution(cls, execution, *, baselines=None, now=None) -> Verification | None:
        """Check one reported or served run: where it ran, the model, the identity, its tools."""
        if Verification.objects.filter(execution=execution).exists():
            return None
        now = now or timezone.now()
        baselines = baselines if baselines is not None else Baselines()
        agent, findings, baseline = execution.agent, [], None
        if agent is not None:
            baseline = baselines.for_subject(agent)
            findings += deployment_findings(baseline, execution.environment)
            findings += model_findings(baseline, execution.model)
            findings += identity_findings(baseline, execution.service_account)
            for event in execution.events.filter(event_type="TOOL_CALL_COMPLETED").only("metadata"):
                metadata = event.metadata or {}
                name = str(metadata.get("tool") or "")
                # Calls brokered by the MCP Gateway are verified as tool calls.
                if not name or metadata.get("mcp_gateway"):
                    continue
                server_slug, _, tool_name = name.rpartition("__")
                findings += tool_findings(baseline, server_slug, tool_name)
        # The run also belongs to the application the agent serves: the system it composes.
        application, system, system_baseline = execution.application, [], None
        if application is not None:
            system_baseline = baselines.for_subject(application)
            system += deployment_findings(system_baseline, execution.environment)
            system += system_findings(system_baseline, execution)
        acted_at = execution.ended_at or execution.started_at or execution.created_at
        governed = any(item is not None and item.authorization is not None for item in (baseline, system_baseline))
        outcome = (
            VerificationOutcome.DIVERGES if findings or system
            else VerificationOutcome.CONFORMS if governed
            else VerificationOutcome.UNGOVERNED
        )
        verification = cls._save_verification(
            organization=execution.organization, execution=execution, agent=agent, acted_at=acted_at,
            outcome=outcome, findings=[finding.as_entry() for finding in (*findings, *system)],
            **cls._timing(agent, execution.created_at, now),
        )
        evidence = {
            "kind": "execution", "id": str(execution.pk), "at": acted_at,
            "label": execution.external_id or str(execution.pk),
        }
        if verification is not None and findings:
            cls._record_findings(baseline, findings, at=acted_at, evidence=evidence)
        if verification is not None and system:
            cls._record_findings(system_baseline, system, at=acted_at, evidence=evidence)
        return verification

    @classmethod
    def verify_pending(cls, organization=None, *, now=None, settle=irgf.SETTLE) -> dict:
        """Everything recorded in the look-back window that nobody has verified yet."""
        from apps.executions.models import Execution
        from apps.gateway.models import GatewayMessage
        from apps.mcp_gateway.models import MCPToolCall

        now = now or timezone.now()
        since = now - irgf.LOOKBACK
        baselines = Baselines()
        calls = MCPToolCall.objects.filter(verification__isnull=True, created_at__gte=since).select_related(
            "agent", "server", "tool", "service_account", "execution", "organization"
        )
        executions = Execution.objects.filter(
            verification__isnull=True, created_at__gte=since, status__in=TERMINAL_EXECUTION_STATUSES,
            updated_at__lte=now - settle,
        ).select_related("agent", "application", "model", "service_account", "organization")
        messages = GatewayMessage.objects.filter(verification__isnull=True, created_at__gte=since).select_related(
            "sender", "recipient", "organization"
        )
        if organization is not None:
            calls = calls.filter(organization=organization)
            executions = executions.filter(organization=organization)
            messages = messages.filter(organization=organization)

        counts = {"tool_calls": 0, "executions": 0, "messages": 0}
        for call in calls.order_by("created_at"):
            counts["tool_calls"] += bool(cls.verify_tool_call(call, baselines=baselines, now=now))
        for execution in executions.order_by("created_at"):
            counts["executions"] += bool(cls.verify_execution(execution, baselines=baselines, now=now))
        for message in messages.order_by("created_at"):
            counts["messages"] += bool(cls.verify_message(message, now=now))
        return counts

    # -- Observers: evaluations, policies, budgets, tool definitions ------------------

    @staticmethod
    def _in_force(organization=None):
        from apps.lifecycle.models import AuthorizationStatus, DeploymentAuthorization

        authorizations = DeploymentAuthorization.objects.filter(
            status=AuthorizationStatus.AUTHORIZED
        ).select_related("agent", "agent__primary_model", "application", "organization")
        if organization is not None:
            authorizations = authorizations.filter(organization=organization)
        return authorizations

    @classmethod
    def observe_evaluations(cls, organization=None) -> int:
        """Behavioral drift: an evaluation after authorization that did not pass."""
        from apps.evaluations.models import EvaluationRun, RunStatus

        found = 0
        for authorization in cls._in_force(organization):
            if authorization.agent_id is None:
                continue  # an application's behaviour is evaluated through its agents
            agent = authorization.agent
            target = Q(agent=agent)
            if agent.primary_model_id:
                target |= Q(model_id=agent.primary_model_id)
            runs = EvaluationRun.objects.filter(
                target, status=RunStatus.COMPLETED, passed=False, finished_at__gte=authorization.decided_at
            ).select_related("suite")
            for run in runs:
                finding = Finding(
                    "evaluation_regression", str(run.pk),
                    f"'{run.suite.name}' did not pass for {agent.name} ({run.score}%) after it was authorized.",
                    f"Blocked by {run.blocked_by_category} cases." if run.blocked_by_category else "",
                    expected=f"A pass at {run.suite.pass_threshold}% or above, as relied on at Gate G3",
                    observed=f"{run.score}% on {run.finished_at:%d %b %Y}",
                )
                _, created = DriftService.record(
                    agent, finding, tier=agent.risk_tier, authorization=authorization, once=True,
                    at=run.finished_at,
                    evidence=[{"kind": "evaluation", "id": str(run.pk), "at": run.finished_at,
                               "label": run.suite.name}],
                )
                found += created
        return found

    @classmethod
    def observe_policies(cls, organization=None) -> int:
        """Policy drift: a policy that applies to an agent changed after its authorization."""
        from apps.core.constants import LifecycleStatus
        from apps.policies.models import Policy
        from apps.policies.services import in_tier_scope

        found = 0
        for authorization in cls._in_force(organization):
            agent = authorization.subject  # a policy governs applications as well as agents
            # What a policy does, not its wording: a reworded description is no drift.
            policies = Policy.objects.filter(
                # On or after: a change in the same clock tick as the decision counts.
                organization=authorization.organization, status=LifecycleStatus.ACTIVE,
                content_changed_at__gte=authorization.decided_at,
            )
            for policy in policies:
                environments = policy.option("environments", []) or []
                if environments and authorization.environment not in environments:
                    continue
                if not in_tier_scope(policy, agent.risk_tier):
                    continue
                changed = policy.content_changed_at
                finding = Finding(
                    "policy_changed", f"{policy.pk}:{changed:%Y%m%d%H%M%S}",
                    f"'{policy.name}' changed on {changed:%d %b %Y}, after {agent.name} was authorized.",
                    "The authorization's compliance evidence predates the policy as it now stands.",
                    expected=f"Assessed against the policies in force on {authorization.decided_at:%d %b %Y}",
                    observed=f"'{policy.name}' now: {policy.get_effect_display().lower()}, {policy.summary}",
                )
                _, created = DriftService.record(
                    agent, finding, tier=agent.risk_tier, authorization=authorization, once=True,
                    at=changed,
                    evidence=[{"kind": "policy", "id": str(policy.pk), "at": policy.updated_at,
                               "label": policy.name}],
                )
                found += created
        return found

    @classmethod
    def observe_budgets(cls, organization=None) -> int:
        """Cost drift: an agent's or application's spend past its approved budget."""
        from apps.costs.models import BudgetAlert

        alerts = BudgetAlert.objects.filter(percentage__gte=100).select_related(
            "budget", "budget__agent", "budget__application"
        )
        if organization is not None:
            alerts = alerts.filter(organization=organization)
        found = 0
        for alert in alerts.order_by("created_at"):
            budget = alert.budget
            subject = budget.agent or budget.application
            if subject is None:
                continue
            finding = Finding(
                "budget_exceeded", f"{budget.pk}:{alert.period_start}",
                f"{subject.name} spent {alert.spend:.2f} {alert.currency}, {alert.percentage:.0f}% of "
                f"the '{budget.name}' budget.",
                expected=f"{alert.budget_amount} {alert.currency} for {alert.period_start:%d %b}–{alert.period_end:%d %b}",
                observed=f"{alert.spend:.2f} {alert.currency}",
            )
            _, created = DriftService.record(
                subject, finding, tier=subject.risk_tier, once=True, at=alert.created_at,
                evidence=[{"kind": "budget_alert", "id": str(alert.pk), "at": alert.created_at,
                           "label": budget.name}],
            )
            found += created
        return found

    @classmethod
    def observe_tool_definitions(cls, organization=None) -> int:
        """Integration drift: a tool an authorized agent depends on changed after review."""
        from apps.mcp_gateway.models import MCPTool, MCPToolCall

        tools = MCPTool.objects.filter(changed_since_review=True).select_related("server")
        if organization is not None:
            tools = tools.filter(organization=organization)
        found = 0
        authorizations = list(cls._in_force(organization))
        for tool in tools:
            recent = set(MCPToolCall.objects.filter(
                tool=tool, created_at__gte=timezone.now() - timedelta(days=30)
            ).values_list("agent_id", flat=True))
            for authorization in authorizations:
                if authorization.agent_id is None:
                    continue
                agent = authorization.agent
                if agent.organization_id != tool.organization_id:
                    continue
                granted = any(grant.get("server") == tool.server.slug
                              for grant in authorization.configuration.get("mcp_grants") or [])
                if not (granted or agent.pk in recent
                        or within_boundary(authorization.configuration, tool.server.slug, tool.name)):
                    continue
                finding = Finding(
                    "tool_definition_changed", f"{tool.pk}:{tool.definition_hash}",
                    f"{tool.qualified_name}, which {agent.name} depends on, changed after it was reviewed.",
                    "The MCP Gateway holds a changed tool until someone reviews it again.",
                    expected="The definition approved at review", observed=f"Definition {tool.definition_hash[:12]}",
                )
                _, created = DriftService.record(
                    agent, finding, tier=agent.risk_tier, authorization=authorization, once=True,
                    evidence=[{"kind": "tool", "id": str(tool.pk), "at": tool.last_seen_at,
                               "label": tool.qualified_name}],
                )
                found += created
        return found

    @classmethod
    def observe(cls, organization=None) -> dict:
        return {
            "evaluations": cls.observe_evaluations(organization),
            "policies": cls.observe_policies(organization),
            "budgets": cls.observe_budgets(organization),
            "tool_definitions": cls.observe_tool_definitions(organization),
        }

    @classmethod
    def sweep(cls, organization=None, *, now=None, settle=irgf.SETTLE) -> dict:
        """One pass of the loop: verify, observe, settle, escalate, digest."""
        now = now or timezone.now()
        result = {"verified": cls.verify_pending(organization, now=now, settle=settle)}
        result["observed"] = cls.observe(organization)
        result["settled"] = DriftService.settle_structural(organization)
        result["reconciled"] = DriftService.reconcile(organization)
        result["escalated"] = DriftService.escalate_overdue(organization, now=now)
        result["digests"] = DriftService.send_digests(organization, now=now)
        return result

    # -- Read models for the pages ------------------------------------------------------

    @staticmethod
    def verification_stats(organization, *, hours: int = 24) -> list[dict]:
        since = timezone.now() - timedelta(hours=hours)
        rows = Verification.objects.for_organization(organization).filter(verified_at__gte=since).values_list(
            "tier", "latency_ms", "late", "outcome"
        )
        by_tier: dict = {}
        for tier, latency, late, outcome in rows:
            by_tier.setdefault(irgf.effective_tier(tier), []).append((latency, late, outcome))
        stats = []
        for tier in (1, 2, 3, 4):
            items = by_tier.get(tier, [])
            latencies = sorted(latency for latency, _, _ in items)
            stats.append({
                "tier": tier,
                "ceiling_seconds": irgf.verification_ceiling(tier),
                "count": len(items),
                "diverged": sum(1 for _, _, outcome in items if outcome == VerificationOutcome.DIVERGES),
                "late": sum(1 for _, late, _ in items if late),
                "mean_ms": int(mean(latencies)) if latencies else None,
                "p95_ms": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None,
                "max_ms": latencies[-1] if latencies else None,
            })
        return stats

    @staticmethod
    def agent_summary(agent) -> dict:
        records = agent.drift_records.filter(status__in=UNRESOLVED_STATUSES)
        return {
            "drift": records.order_by("-is_incident", "-last_seen_at")[:4],
            "drift_count": records.count(),
            "incidents": records.filter(is_incident=True).count(),
            "usage": limits.usage(agent) if agent.action_limits else None,
            "last_verification": agent.verifications.order_by("-verified_at").first(),
        }
