"""Gate G1: qualification and provisional risk.

Decision rights follow IRGF's proportionality:
- **Tier 1**, internal data at most and no sector constraints, is approved by
  rule on submission: self-service, recorded as such.
- **Tiers 1-2** otherwise: the Risk and Compliance Lead (`risk.manage`) or the
  AI Governance Body (`gate.decide`).
- **Tiers 3-4**: the AI Governance Body, independent of the use case: not its
  sponsor and not the person who proposed it.
Nobody decides a use case they proposed, at any tier.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from django.contrib.auth import get_user_model
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Perm
from apps.audit.models import AuditAction
from apps.audit.services import AuditService
from apps.core.constants import DataClassification, LifecycleStatus
from apps.intake.models import (
    APPROVED_STATUSES,
    EDITABLE_STATUSES,
    OPEN_STATUSES,
    OUTCOME_STATUS,
    G1Outcome,
    UseCase,
    UseCaseStage,
    UseCaseStatus,
)

AUTO_APPROVE_TIER = 1
AUTO_APPROVE_DATA = (DataClassification.PUBLIC, DataClassification.INTERNAL)
INDEPENDENT_FROM_TIER = 3
#: Who may propose a use case: anyone who builds, runs, sponsors or governs AI.
PROPOSE_PERMISSIONS = (Perm.INVENTORY_MANAGE, Perm.APPLICATION_MANAGE, Perm.APPROVAL_DECIDE,
                       Perm.RISK_MANAGE, Perm.GATE_DECIDE)


class IntakeError(Exception):
    pass


@dataclass(frozen=True)
class Criterion:
    key: str
    label: str
    met: bool
    detail: str = ""


def criteria(use_case: UseCase) -> list[Criterion]:
    """IRGF's G1 entry criteria, as the record stands."""
    uc = use_case
    return [
        Criterion("need", "A named need, with how it is measured today and the target",
                  bool(uc.need.strip() and uc.current_measure.strip() and uc.target_measure.strip())),
        Criterion("alternative", "The non-AI alternative considered", bool(uc.non_ai_alternative.strip())),
        Criterion("decision", "The decision the output supports, in one sentence", bool(uc.decision_supported.strip())),
        Criterion("affected", "The people affected", bool(uc.affected_parties.strip())),
        Criterion("autonomy", "Intended launch autonomy, in D2 terms", uc.intended_autonomy is not None),
        Criterion("scores", "Provisional D1–D5 scores", uc.scored,
                  f"Provisional Tier {uc.provisional_tier}" if uc.provisional_tier else ""),
        Criterion("data", "A data path, with its sensitivity", bool(uc.data_path.strip() and uc.data_classification)),
        Criterion("commitment", "No architecture or vendor commitment yet", uc.no_commitment),
    ]


def missing(use_case: UseCase) -> list[str]:
    return [criterion.label for criterion in criteria(use_case) if not criterion.met]


def has(user, organization, permission) -> bool:
    return bool(user and user.is_authenticated and user.has_org_permission(organization, permission))


def can_propose(user, organization) -> bool:
    return any(has(user, organization, permission) for permission in PROPOSE_PERMISSIONS)


def deciding_permissions(tier: int | None) -> tuple[str, ...]:
    if tier and tier >= INDEPENDENT_FROM_TIER:
        return (Perm.GATE_DECIDE,)
    return (Perm.RISK_MANAGE, Perm.GATE_DECIDE)


def who_decides(tier: int | None) -> str:
    if tier and tier >= INDEPENDENT_FROM_TIER:
        return "The AI Governance Body, independent of the use case"
    if tier == AUTO_APPROVE_TIER:
        return ("Approved by rule when the data is internal at most and no sector constraints apply; "
                "otherwise the Risk and Compliance Lead or the AI Governance Body")
    return "The Risk and Compliance Lead or the AI Governance Body"


def can_decide(use_case: UseCase, user) -> tuple[bool, str]:
    if use_case.status not in OPEN_STATUSES:
        return False, "This use case is not awaiting a G1 decision."
    if not any(has(user, use_case.organization, p) for p in deciding_permissions(use_case.provisional_tier)):
        return False, f"G1 for this use case is decided by: {who_decides(use_case.provisional_tier).lower()}."
    if use_case.created_by_id == user.pk:
        return False, "Nobody decides a use case they proposed."
    if (use_case.provisional_tier or 0) >= INDEPENDENT_FROM_TIER and use_case.sponsor_id == user.pk:
        return False, f"From Tier {INDEPENDENT_FROM_TIER}, the sponsor may not decide their own use case."
    return True, ""


def deciders(use_case: UseCase) -> list:
    User = get_user_model()
    members = User.objects.filter(organization_memberships__organization=use_case.organization, is_active=True)
    return [user for user in members.distinct() if can_decide(use_case, user)[0]]


def _audit(use_case: UseCase, user, action: str, old: dict, new: dict) -> None:
    AuditService.log(
        action=action, resource_type=UseCase._meta.label, resource_id=use_case.pk,
        resource_label=str(use_case), organization=use_case.organization, actor=user,
        old_value=old, new_value=new,
    )


def _notify(use_case: UseCase, *, title: str, body: str, recipients, severity="info") -> None:
    from apps.notifications import emitters
    from apps.notifications.models import NotificationCategory

    recipients = [r for r in recipients if r is not None]
    if recipients:
        emitters.lifecycle_notice(
            use_case, category=NotificationCategory.GATE_DECISION, title=title, body=body,
            url=reverse("intake:detail", args=[use_case.pk]), recipients=recipients, severity=severity,
            dedupe_key=f"g1:{use_case.pk}:{use_case.status}:{timezone.now():%Y%m%d%H%M}",
        )


class IntakeService:
    @staticmethod
    @transaction.atomic
    def submit(use_case: UseCase, user) -> UseCase:
        if use_case.status not in EDITABLE_STATUSES:
            raise IntakeError("Only a draft, or one returned to remediate, can be submitted.")
        gaps = missing(use_case)
        if gaps:
            raise IntakeError("Not ready for G1. Still needed: " + "; ".join(gaps) + ".")
        old = {"status": use_case.status}
        use_case.status = UseCaseStatus.SUBMITTED
        use_case.stage = UseCaseStage.S1
        use_case.submitted_at = timezone.now()
        use_case.outcome, use_case.decided_at, use_case.decided_by, use_case.decided_by_rule = "", None, None, ""
        use_case.save()
        _audit(use_case, user, AuditAction.GATE_REQUEST, old, {"status": use_case.status, "tier": use_case.provisional_tier})

        if (use_case.provisional_tier == AUTO_APPROVE_TIER and use_case.data_classification in AUTO_APPROVE_DATA
                and not use_case.sector_constraints.strip()):
            IntakeService._apply(use_case, None, G1Outcome.APPROVE,
                                 "Tier 1 with internal data at most and no sector constraints: self-service.",
                                 [], rule="G1 auto-approval by rule (Tier 1)")
            _notify(use_case, title=f"{use_case.reference} approved at G1 by rule",
                    body=f"{use_case.title} is Tier 1 and was approved by rule. Register it when you are ready.",
                    recipients=[use_case.created_by, use_case.sponsor])
        else:
            _notify(use_case, title=f"G1 decision needed: {use_case.title}",
                    body=f"{use_case.reference} is provisionally Tier {use_case.provisional_tier}. "
                         f"{who_decides(use_case.provisional_tier)} decides.",
                    recipients=deciders(use_case), severity="warning")
        return use_case

    @staticmethod
    def _apply(use_case, user, outcome, note, conditions, rule=""):
        old = {"status": use_case.status}
        use_case.status = OUTCOME_STATUS[outcome]
        use_case.outcome = outcome
        use_case.decided_at = timezone.now()
        use_case.decided_by = user
        use_case.decided_by_rule = rule
        use_case.decision_note = note
        use_case.conditions = conditions
        use_case.save()
        _audit(use_case, user, AuditAction.GATE_SIGNATURE if outcome != G1Outcome.REJECT else AuditAction.GATE_REJECTION,
               old, {"status": use_case.status, "outcome": outcome, "note": note, "rule": rule,
                     "conditions": conditions})

    @staticmethod
    @transaction.atomic
    def decide(use_case: UseCase, user, outcome: str, note: str = "", conditions: list[dict] | None = None) -> UseCase:
        allowed, reason = can_decide(use_case, user)
        if not allowed:
            raise IntakeError(reason)
        if outcome not in G1Outcome.values:
            raise IntakeError("Unknown outcome.")
        note = (note or "").strip()
        conditions = conditions or []
        if outcome == G1Outcome.APPROVE_WITH_CONDITIONS:
            if not conditions:
                raise IntakeError("Approving with conditions needs at least one condition.")
            for condition in conditions:
                if not (condition.get("text") and condition.get("owner") and condition.get("due")):
                    raise IntakeError("Each condition needs what, who owns it, and when it is due.")
        if outcome == G1Outcome.REMEDIATE and not note:
            raise IntakeError("Say what would satisfy the gate, so the use case can come back ready.")
        if outcome == G1Outcome.ESCALATE and not note:
            raise IntakeError("Say where it is escalated and what that body must answer.")
        if outcome == G1Outcome.REJECT and not note:
            raise IntakeError("A rejection is recorded with its reason.")
        IntakeService._apply(use_case, user, outcome, note,
                             conditions if outcome == G1Outcome.APPROVE_WITH_CONDITIONS else [])
        _notify(use_case, title=f"{use_case.reference}: {G1Outcome(outcome).label} at G1",
                body=note or f"{use_case.title} was approved at Gate G1.",
                recipients=[use_case.created_by, use_case.sponsor])
        return use_case

    @staticmethod
    @transaction.atomic
    def complete_condition(use_case: UseCase, index: int, user) -> None:
        conditions = list(use_case.conditions or [])
        if not (0 <= index < len(conditions)):
            raise IntakeError("No such condition.")
        if not (user.pk in (conditions[index].get("owner_id"), use_case.sponsor_id, use_case.created_by_id)
                or has(user, use_case.organization, Perm.GATE_DECIDE)):
            raise IntakeError("Only the condition's owner, the sponsor or the AI Governance Body can close it.")
        conditions[index] = {**conditions[index], "done": True, "done_at": timezone.now().isoformat(),
                             "done_by": user.get_full_name() or user.email}
        use_case.conditions = conditions
        use_case.save(update_fields=["conditions", "updated_at"])
        _audit(use_case, user, AuditAction.UPDATE, {}, {"condition_met": conditions[index]["text"]})

    @staticmethod
    @transaction.atomic
    def register(use_case: UseCase, user):
        """Approved at G1: create the draft application the rest of the lifecycle governs."""
        from apps.applications_registry.models import AIApplication

        if use_case.status not in APPROVED_STATUSES:
            raise IntakeError("Only a use case approved at G1 can be registered.")
        name, number = use_case.title[:190], 2
        while AIApplication.objects.for_organization(use_case.organization).filter(name=name).exists():
            name, number = f"{use_case.title[:180]} ({number})", number + 1
        scores = ", ".join(f"{label} {value}" for label, value in [
            ("D1", use_case.d1_consequence), ("D2", use_case.d2_autonomy), ("D3", use_case.d3_reversibility),
            ("D4", use_case.d4_exposure), ("D5 data", use_case.d5_data_sensitivity),
            ("D5 model", use_case.d5_model_uncertainty)])
        application = AIApplication.objects.create(
            organization=use_case.organization,
            name=name,
            description=use_case.summary or use_case.need[:500],
            business_purpose=(f"{use_case.need}\n\nDecision supported: {use_case.decision_supported}\n"
                              f"Registered from {use_case.reference}, approved at Gate G1 with provisional "
                              f"Tier {use_case.provisional_tier} ({scores}). Confirm the tier at S2."),
            business_owner=use_case.sponsor,
            team=use_case.team,
            department=use_case.department or (use_case.team.department if use_case.team_id else ""),
            data_classification=use_case.data_classification or DataClassification.INTERNAL,
            status=LifecycleStatus.DRAFT,
            created_by=user,
        )
        use_case.application = application
        use_case.status = UseCaseStatus.REGISTERED
        use_case.registered_at = timezone.now()
        use_case.save()
        _audit(use_case, user, AuditAction.CREATE, {}, {"registered_as": str(application.pk), "name": application.name})
        return application

    @staticmethod
    def withdraw(use_case: UseCase, user) -> None:
        if use_case.status in (UseCaseStatus.REGISTERED, UseCaseStatus.WITHDRAWN, UseCaseStatus.REJECTED):
            raise IntakeError("This use case is already closed.")
        old = {"status": use_case.status}
        use_case.status = UseCaseStatus.WITHDRAWN
        use_case.save(update_fields=["status", "updated_at"])
        _audit(use_case, user, AuditAction.UPDATE, old, {"status": use_case.status})


def overdue_conditions(use_case: UseCase) -> list[dict]:
    today = date.today().isoformat()
    return [c for c in use_case.open_conditions if c.get("due") and c["due"] < today]
