"""Approval rule engine and decision service (sections 19 and 20)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.approvals.models import ApprovalRequest, ApprovalRule, ApprovalStatus
from apps.audit.models import AuditAction
from apps.audit.services import AuditService
from apps.core.constants import RiskLevel
from apps.executions.models import ApprovalState, ExecutionStatusChoices
from integrations.base.events import EventType

SEVERITY_ORDER = [RiskLevel.LOW, RiskLevel.MODERATE, RiskLevel.HIGH, RiskLevel.CRITICAL]
DEFAULT_EXPIRY_HOURS = 24


@dataclass
class ApprovalContext:
    """What the caller knows about the action being attempted."""

    agent: Any = None
    application: Any = None
    model: Any = None
    provider: Any = None
    execution: Any = None
    action: str = ""
    tool: str = ""
    data_classification: str = ""
    environment: str = ""
    estimated_cost: Decimal = Decimal("0")

    @property
    def effective_environment(self) -> str:
        return self.environment or getattr(self.agent, "environment", "") or ""

    @property
    def effective_data_classification(self) -> str:
        return (
            self.data_classification
            or getattr(self.agent, "data_classification", "")
            or getattr(self.application, "data_classification", "")
            or ""
        )

    @property
    def effective_risk_level(self) -> str:
        return (
            getattr(self.agent, "risk_level", "")
            or getattr(self.application, "risk_level", "")
            or ""
        )

    @property
    def effective_tier(self) -> int | None:
        """The higher IRGF tier of the agent and application, if either is classified."""
        tiers = [
            tier
            for tier in (
                getattr(self.agent, "risk_tier", None),
                getattr(self.application, "risk_tier", None),
            )
            if tier
        ]
        return max(tiers) if tiers else None


@dataclass
class RuleDecision:
    requires_approval: bool = False
    matched: list[ApprovalRule] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    severity: str = RiskLevel.LOW

    def as_metadata(self) -> list[dict]:
        return [
            {"id": str(rule.pk), "name": rule.name, "severity": rule.severity}
            for rule in self.matched
        ]


class ApprovalRuleEngine:
    """Evaluates the configured rules against one attempted action.

    A rule matches only when every condition it specifies is satisfied. A rule
    with no conditions never matches -- an empty rule silently gating every
    action would be far worse than one that does nothing.
    """

    @staticmethod
    def _matches(rule: ApprovalRule, context: ApprovalContext) -> bool:
        checks: list[bool] = []

        if rule.min_autonomy_level is not None:
            autonomy = getattr(context.agent, "autonomy_level", None)
            checks.append(autonomy is not None and autonomy >= rule.min_autonomy_level)

        if rule.risk_levels:
            checks.append(context.effective_risk_level in rule.risk_levels)

        if rule.environments:
            checks.append(context.effective_environment in rule.environments)

        if rule.data_classifications:
            checks.append(context.effective_data_classification in rule.data_classifications)

        if rule.tools:
            checks.append(bool(context.tool) and context.tool in rule.tools)

        if rule.actions:
            checks.append(bool(context.action) and context.action in rule.actions)

        if rule.cost_threshold is not None:
            checks.append(Decimal(context.estimated_cost or 0) >= rule.cost_threshold)

        if rule.match_restricted_tool:
            restricted = set(getattr(context.agent, "restricted_tools", []) or [])
            checks.append(bool(context.tool) and context.tool in restricted)

        if rule.match_unapproved_provider:
            provider = context.provider or getattr(context.agent, "provider", None)
            checks.append(provider is not None and not provider.is_approved)

        if rule.match_unapproved_model:
            model = context.model or getattr(context.agent, "primary_model", None)
            checks.append(model is not None and not model.approved_for_use)

        if rule.tiers:
            wanted = {int(tier) for tier in rule.tiers if str(tier).strip().isdigit()}
            checks.append(context.effective_tier in wanted)

        return bool(checks) and all(checks)

    @classmethod
    def evaluate(cls, organization, context: ApprovalContext) -> RuleDecision:
        rules = ApprovalRule.objects.for_organization(organization).filter(status="active")

        decision = RuleDecision()
        for rule in rules:
            if cls._matches(rule, context):
                decision.matched.append(rule)
                decision.reasons.append(f"{rule.name}: {', '.join(rule.conditions)}")

        if decision.matched:
            decision.requires_approval = True
            decision.severity = max(
                (rule.severity for rule in decision.matched),
                key=lambda level: SEVERITY_ORDER.index(level)
                if level in SEVERITY_ORDER
                else 0,
            )
        return decision


class ApprovalService:
    """Creates approval requests and records human decisions."""

    @classmethod
    @transaction.atomic
    def request(
        cls,
        *,
        organization,
        requested_action: str,
        context: ApprovalContext | None = None,
        reason: str = "",
        risk_level: str | None = None,
        requested_by=None,
        requested_by_system: str = "",
        assigned_approver=None,
        expires_in_hours: int = DEFAULT_EXPIRY_HOURS,
        matched_rules: list[dict] | None = None,
    ) -> ApprovalRequest:
        context = context or ApprovalContext()
        execution = context.execution

        application = context.application
        if application is None and context.agent is not None:
            application = context.agent.applications.first()

        request = ApprovalRequest.objects.create(
            organization=organization,
            execution=execution,
            agent=context.agent,
            application=application,
            requested_action=requested_action,
            reason=reason,
            risk_level=risk_level or context.effective_risk_level or RiskLevel.MODERATE,
            requested_by=requested_by,
            requested_by_system=requested_by_system,
            assigned_approver=assigned_approver,
            expires_at=timezone.now() + timedelta(hours=expires_in_hours)
            if expires_in_hours
            else None,
            matched_rules=matched_rules or [],
            context={
                "action": context.action,
                "tool": context.tool,
                "environment": context.effective_environment,
                "data_classification": context.effective_data_classification,
                "estimated_cost": str(context.estimated_cost or 0),
            },
        )

        # Pause the run and put an APPROVAL_REQUESTED marker on its timeline.
        if execution is not None:
            cls._mark_execution(
                execution,
                status=ExecutionStatusChoices.WAITING_FOR_APPROVAL,
                approval_status=ApprovalState.PENDING,
                event_type=EventType.APPROVAL_REQUESTED,
                metadata={"approval_request": request.request_id},
            )

        AuditService.log(
            action=AuditAction.CREATE,
            resource_type=ApprovalRequest._meta.label,
            resource_id=request.pk,
            resource_label=request.request_id,
            organization=organization,
            actor=requested_by,
            new_value={"requested_action": requested_action, "risk_level": request.risk_level},
            metadata={"rules": [rule["name"] for rule in (matched_rules or [])]},
        )

        from apps.notifications import emitters

        emitters.approval_requested(request)
        return request

    @classmethod
    def evaluate_and_request(
        cls, *, organization, requested_action: str, context: ApprovalContext, **kwargs
    ) -> tuple[RuleDecision, ApprovalRequest | None]:
        """Run the rules, and raise a request only if one is actually needed."""
        decision = ApprovalRuleEngine.evaluate(organization, context)
        if not decision.requires_approval:
            return decision, None

        request = cls.request(
            organization=organization,
            requested_action=requested_action,
            context=context,
            reason="; ".join(decision.reasons),
            risk_level=decision.severity,
            matched_rules=decision.as_metadata(),
            **kwargs,
        )
        return decision, request

    # -- Decisions ----------------------------------------------------------

    @classmethod
    def approve(cls, request: ApprovalRequest, *, user, notes: str = "") -> ApprovalRequest:
        return cls._decide(request, ApprovalStatus.APPROVED, user=user, notes=notes)

    @classmethod
    def reject(cls, request: ApprovalRequest, *, user, notes: str = "") -> ApprovalRequest:
        return cls._decide(request, ApprovalStatus.REJECTED, user=user, notes=notes)

    @classmethod
    def cancel(cls, request: ApprovalRequest, *, user=None, notes: str = "") -> ApprovalRequest:
        return cls._decide(request, ApprovalStatus.CANCELLED, user=user, notes=notes)

    @classmethod
    @transaction.atomic
    def _decide(
        cls, request: ApprovalRequest, status: str, *, user=None, notes: str = ""
    ) -> ApprovalRequest:
        if not request.is_pending:
            raise ValueError(
                f"{request.request_id} is already {request.get_status_display().lower()}."
            )

        # IRGF moves Tier 3-4 decisions out of the delivery line: whoever raised
        # the request and the system's own owners cannot decide it.
        if status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED) and user is not None:
            tier = max(
                (getattr(asset, "risk_tier", None) or 0 for asset in (request.agent, request.application)),
                default=0,
            )
            owners = {
                pk
                for pk in (
                    getattr(request.agent, "owner_id", None),
                    getattr(request.application, "business_owner_id", None),
                    getattr(request.application, "technical_owner_id", None),
                )
                if pk
            }
            if tier >= 3 and (user.pk == request.requested_by_id or user.pk in owners):
                raise ValueError(
                    f"{request.request_id} concerns a Tier {tier} system. IRGF takes Tier 3-4 "
                    "decisions out of the delivery line, so the person who raised it and the "
                    "system's owners cannot decide it."
                )

        request.status = status
        request.decision_at = timezone.now()
        request.decision_by = user
        request.decision_notes = notes
        request.save(
            update_fields=["status", "decision_at", "decision_by", "decision_notes", "updated_at"]
        )

        if request.execution_id:
            approved = status == ApprovalStatus.APPROVED
            cls._mark_execution(
                request.execution,
                # The Control Tower does not resume the run -- the engine does,
                # after seeing this decision. It only records the outcome and
                # releases the pause.
                status=ExecutionStatusChoices.RUNNING
                if approved
                else ExecutionStatusChoices.CANCELLED,
                approval_status=(
                    ApprovalState.APPROVED if approved else ApprovalState.REJECTED
                ),
                event_type=(
                    EventType.APPROVAL_GRANTED if approved else EventType.APPROVAL_REJECTED
                ),
                metadata={"approval_request": request.request_id, "decided_by": str(user or "")},
            )

        AuditService.log(
            action=AuditAction.APPROVE if status == ApprovalStatus.APPROVED else AuditAction.REJECT,
            resource_type=ApprovalRequest._meta.label,
            resource_id=request.pk,
            resource_label=request.request_id,
            organization=request.organization,
            actor=user,
            new_value={"status": status, "notes": notes},
        )

        from apps.notifications import emitters

        emitters.approval_decided(request)
        return request

    @staticmethod
    def _mark_execution(execution, *, status, approval_status, event_type, metadata) -> None:
        from apps.executions.services import ExecutionService

        execution.status = status
        execution.approval_required = True
        execution.approval_status = approval_status
        execution.save(
            update_fields=["status", "approval_required", "approval_status", "updated_at"]
        )

        # An engine may already have emitted APPROVAL_REQUESTED itself before
        # calling the API. Recording a second identical marker would make the
        # timeline read as two separate requests, so only append when the run
        # is not already sitting on one.
        if event_type == EventType.APPROVAL_REQUESTED:
            latest = (
                execution.events.filter(
                    event_type__in=[
                        str(EventType.APPROVAL_REQUESTED),
                        str(EventType.APPROVAL_GRANTED),
                        str(EventType.APPROVAL_REJECTED),
                    ]
                )
                .order_by("-sequence")
                .first()
            )
            if latest and latest.event_type == str(EventType.APPROVAL_REQUESTED):
                return

        ExecutionService.append_events(
            execution, [{"event_type": str(event_type), "metadata": metadata}]
        )

    @classmethod
    def expire_stale(cls, organization=None) -> int:
        """Expire pending requests past their deadline (section 24)."""
        queryset = ApprovalRequest.objects.filter(
            status=ApprovalStatus.PENDING, expires_at__lte=timezone.now()
        )
        if organization is not None:
            queryset = queryset.for_organization(organization)

        expired = 0
        for request in queryset.select_related("execution", "organization"):
            request.status = ApprovalStatus.EXPIRED
            request.decision_at = timezone.now()
            request.save(update_fields=["status", "decision_at", "updated_at"])

            if request.execution_id:
                execution = request.execution
                execution.approval_status = ApprovalState.EXPIRED
                execution.status = ExecutionStatusChoices.TIMED_OUT
                execution.save(update_fields=["approval_status", "status", "updated_at"])

            AuditService.log(
                action=AuditAction.UPDATE,
                resource_type=ApprovalRequest._meta.label,
                resource_id=request.pk,
                resource_label=request.request_id,
                organization=request.organization,
                new_value={"status": ApprovalStatus.EXPIRED},
                metadata={"reason": "expired without a decision"},
            )
            expired += 1
        return expired
