"""The policy engine (section 18).

One evaluator per policy type. Each reads only the typed settings its type
declares, so evaluation stays explicit and auditable -- there is no generic
expression evaluation anywhere in here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from django.utils import timezone

from apps.core.constants import RiskLevel
from apps.policies.models import Policy, PolicyEffect, PolicyType, PolicyViolation
from integrations.base.events import EventType

logger = logging.getLogger(__name__)

SEVERITY_ORDER = [RiskLevel.LOW, RiskLevel.MODERATE, RiskLevel.HIGH, RiskLevel.CRITICAL]


def _rank(severity: str) -> int:
    return SEVERITY_ORDER.index(severity) if severity in SEVERITY_ORDER else 0


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        return Decimal("0")


@dataclass
class PolicyContext:
    """What the caller is asking permission to do."""

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
    def effective_provider(self):
        return self.provider or getattr(self.agent, "provider", None)

    @property
    def effective_model(self):
        return self.model or getattr(self.agent, "primary_model", None)

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
    def effective_tier(self) -> int | None:
        """The IRGF risk tier in play: the higher of the agent's and application's.

        None when neither is classified. Read from the tier cached on each asset,
        so scoping a policy by tier costs no query on the gateway's path.
        """
        tiers = [
            tier
            for tier in (
                getattr(self.agent, "risk_tier", None),
                getattr(self.application, "risk_tier", None),
            )
            if tier
        ]
        return max(tiers) if tiers else None

    def to_approval_context(self):
        from apps.approvals.services import ApprovalContext

        return ApprovalContext(
            agent=self.agent,
            application=self.application,
            model=self.model,
            provider=self.provider,
            execution=self.execution,
            action=self.action,
            tool=self.tool,
            data_classification=self.data_classification,
            environment=self.environment,
            estimated_cost=self.estimated_cost,
        )

    def as_metadata(self) -> dict:
        return {
            "action": self.action,
            "tool": self.tool,
            "environment": self.effective_environment,
            "data_classification": self.effective_data_classification,
            "estimated_cost": str(self.estimated_cost or 0),
            "agent": str(self.agent) if self.agent else "",
            "provider": str(self.effective_provider) if self.effective_provider else "",
            "model": str(self.effective_model) if self.effective_model else "",
        }


@dataclass
class PolicyMatch:
    policy: Policy
    reason: str


@dataclass
class PolicyDecision:
    allowed: bool = True
    requires_approval: bool = False
    matches: list[PolicyMatch] = field(default_factory=list)
    severity: str = RiskLevel.LOW
    #: Policies an active IRGF M17 exception set aside for this agent.
    excepted: list[Policy] = field(default_factory=list)

    @property
    def denials(self) -> list[PolicyMatch]:
        return [m for m in self.matches if m.policy.effect == PolicyEffect.DENY]

    @property
    def warnings(self) -> list[PolicyMatch]:
        return [m for m in self.matches if m.policy.effect == PolicyEffect.WARN]

    @property
    def policy_ids(self) -> list[str]:
        return [str(m.policy.pk) for m in self.matches]

    @property
    def policy_names(self) -> list[str]:
        return [m.policy.name for m in self.matches]

    @property
    def reason(self) -> str:
        return "; ".join(m.reason for m in self.matches)

    def as_payload(self) -> dict:
        """The stable contract engines depend on (section 18)."""
        return {
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "policy_ids": self.policy_ids,
            "policy_names": self.policy_names,
            "reason": self.reason,
            "severity": self.severity,
            "denied_by": [m.policy.name for m in self.denials],
            "warnings": [m.reason for m in self.warnings],
            # Set aside for this agent by an active IRGF M17 exception.
            "excepted": [policy.name for policy in self.excepted],
        }


# -- Evaluators -------------------------------------------------------------
#
# Each returns a reason string when the policy is triggered, or None. They are
# intentionally small and independent: a new control is a new function.


def _check_model_approval(policy: Policy, context: PolicyContext) -> str | None:
    model = context.effective_model
    if model is None:
        return None

    if not model.approved_for_use:
        return f"Model '{model.model_name}' is not approved for use."

    blocked = policy.option("blocked_risk_classifications", []) or []
    if blocked and model.risk_classification in blocked:
        return (
            f"Model '{model.model_name}' carries a "
            f"{model.get_risk_classification_display()} risk classification."
        )

    return _check_evaluation_evidence(policy, model)


def _check_evaluation_evidence(policy: Policy, model) -> str | None:
    """Require the approval to be backed by a passing evaluation.

    The gate reads the model's **latest completed run**, never its best one.
    Letting a policy pick the best result would turn the control into a search
    for one good day, which is exactly the failure mode evaluation exists to
    prevent -- if the most recent evidence says the model regressed, that is
    the evidence that counts.

    Off unless `require_evaluation` is configured, so adding this to the schema
    does not silently start blocking traffic for existing policies.
    """
    if not policy.option("require_evaluation", False):
        return None

    # Imported here rather than at module scope: evaluations run through the
    # gateway, which consults this engine, so a top-level import would close a
    # cycle at startup.
    from apps.evaluations.models import EvaluationRun, RunStatus

    latest = (
        EvaluationRun.objects.filter(model=model, status=RunStatus.COMPLETED)
        .select_related("suite")
        .order_by("-finished_at")
        .first()
    )

    if latest is None:
        return (
            f"Model '{model.model_name}' is approved but has never been evaluated, "
            "and this policy requires evaluation evidence."
        )

    if not latest.passed:
        # "Blocked" is not a low score: say which is which, because the two
        # need different responses from whoever reads the violation.
        if latest.blocked_by_category:
            return (
                f"Model '{model.model_name}' last failed a "
                f"{latest.blocked_by_category} case in '{latest.suite.name}', "
                f"which that suite treats as disqualifying (scored {latest.score}%)."
            )
        return (
            f"Model '{model.model_name}' last scored {latest.score}% on "
            f"'{latest.suite.name}', below the suite's {latest.suite.pass_threshold}% bar."
        )

    max_age = policy.option("evaluation_max_age_days")
    if max_age:
        # A pass from six months ago is weak evidence about a model the
        # provider has since changed underneath you.
        cutoff = timezone.now() - timedelta(days=int(max_age))
        if latest.finished_at and latest.finished_at < cutoff:
            age = (timezone.now() - latest.finished_at).days
            return (
                f"Model '{model.model_name}' last passed evaluation {age} days ago, "
                f"beyond this policy's {max_age}-day limit."
            )

    required = policy.option("required_suites", []) or []
    if required:
        passed_suites = set(
            EvaluationRun.objects.filter(
                model=model, status=RunStatus.COMPLETED, passed=True
            ).values_list("suite__name", flat=True)
        )
        missing = [name for name in required if name not in passed_suites]
        if missing:
            return (
                f"Model '{model.model_name}' has not passed required "
                f"{'suite' if len(missing) == 1 else 'suites'}: {', '.join(missing)}."
            )

    return None


def _check_provider(policy: Policy, context: PolicyContext) -> str | None:
    provider = context.effective_provider
    if provider is None:
        return None

    identifiers = {provider.slug, provider.name, provider.adapter_key}

    blocked = set(policy.option("blocked_providers", []) or [])
    if blocked & identifiers:
        return f"Provider '{provider.name}' is blocked."

    allowed = set(policy.option("allowed_providers", []) or [])
    if allowed and not (allowed & identifiers):
        return f"Provider '{provider.name}' is not on the permitted list."

    regions = policy.option("allowed_regions", []) or []
    if regions and provider.region and provider.region not in regions:
        return f"Provider '{provider.name}' operates in an unapproved region ({provider.region})."

    if policy.option("require_approved", False) and not provider.is_approved:
        return f"Provider '{provider.name}' has not been approved by governance."
    return None


def _check_data_classification(policy: Policy, context: PolicyContext) -> str | None:
    classifications = policy.option("classifications", []) or []
    classification = context.effective_data_classification
    if classifications and classification not in classifications:
        return None
    if not classification:
        return None

    environments = policy.option("environments", []) or []
    if environments and context.effective_environment not in environments:
        return None

    provider = context.effective_provider
    if provider is None:
        return None

    blocked = set(policy.option("blocked_providers", []) or [])
    if blocked & {provider.slug, provider.name, provider.adapter_key}:
        return f"{classification.title()} data may not be sent to '{provider.name}'."

    if policy.option("require_approved_provider", False) and not provider.is_approved:
        return (
            f"{classification.title()} data would reach '{provider.name}', "
            "which is not an approved provider."
        )
    return None


def _check_agent_autonomy(policy: Policy, context: PolicyContext) -> str | None:
    agent = context.agent
    if agent is None:
        return None

    environments = policy.option("environments", []) or []
    if environments and context.effective_environment not in environments:
        return None

    risk_levels = policy.option("risk_levels", []) or []
    if risk_levels and agent.risk_level not in risk_levels:
        return None

    ceiling = policy.option("max_autonomy_level")
    if ceiling is None:
        return None

    if agent.autonomy_level > int(ceiling):
        return (
            f"Agent '{agent.name}' runs at autonomy level {agent.autonomy_level}, "
            f"above the permitted maximum of {ceiling}."
        )
    return None


def _check_cost_limit(policy: Policy, context: PolicyContext) -> str | None:
    ceiling = policy.option("max_cost_per_execution")
    if ceiling is None:
        return None

    limit = _decimal(ceiling)
    cost = _decimal(context.estimated_cost)
    if cost >= limit:
        return f"Estimated cost {cost} reaches the per-run ceiling of {limit}."
    return None


def _check_tool_access(policy: Policy, context: PolicyContext) -> str | None:
    if not context.tool:
        return None

    environments = policy.option("environments", []) or []
    if environments and context.effective_environment not in environments:
        return None

    blocked = set(policy.option("blocked_tools", []) or [])
    if context.tool in blocked:
        return f"Tool '{context.tool}' is prohibited."
    return None


def _check_human_approval(policy: Policy, context: PolicyContext) -> str | None:
    """Delegates to the approval rules, so both surfaces agree."""
    from apps.approvals.services import ApprovalRuleEngine

    decision = ApprovalRuleEngine.evaluate(
        policy.organization, context.to_approval_context()
    )
    if decision.requires_approval:
        return "; ".join(decision.reasons)
    return None


def _subject_label(subject) -> str:
    kind = "Agent" if hasattr(subject, "agent_id") else "Application"
    return f"{kind} '{subject.name}'"


def _check_risk_classification(policy: Policy, context: PolicyContext) -> str | None:
    """IRGF S2: whatever is acting must carry a current risk classification.

    Gate G1 wants provisional D1-D5 and Gate G2 a confirmed tier. A system
    serving traffic with neither skipped the stage that decides how much
    governance it gets, so every tier-scoped control is silently off for it.
    """
    subjects = [subject for subject in (context.agent, context.application) if subject is not None]
    if not subjects:
        return None

    environments = policy.option("environments", []) or []
    if environments and context.effective_environment not in environments:
        return None

    from apps.classification.services import ClassificationService

    for subject in subjects:
        record = ClassificationService.current(subject)
        label = _subject_label(subject)
        if record is None:
            return (
                f"{label} has no IRGF risk classification. Score it on D1-D5 (stage S2) "
                "before it serves traffic."
            )
        if policy.option("require_countersignature", True) and not record.is_confirmed:
            return (
                f"{label} is provisionally Tier {record.tier}; its classification awaits an "
                "independent countersignature (IRGF M6), which Gate G2 requires."
            )
        if policy.option("block_when_rescore_due", False) and record.rescore_due:
            reasons = "; ".join(record.rescore_reasons) or "the annual Tier 3-4 review is due"
            return f"{label} needs a re-score: {reasons}"
    return None


def _check_deployment_authorization(policy: Policy, context: PolicyContext) -> str | None:
    """IRGF S7 · Gate G3: the agent must hold an authorization in force here.

    A suspended authorization -- a major change awaiting Gate G4, or a change
    G4 rejected -- counts as none. Material changes still awaiting G4 pass
    unless the policy says otherwise.
    """
    # An agent answers for itself, and an application for the system it makes up.
    for subject in (context.agent, context.application):
        if subject is None:
            continue
        environment = context.environment or getattr(subject, "environment", "") or ""
        environments = policy.option("environments", []) or []
        if environments and environment not in environments:
            continue
        reason = _authorization_denial(policy, subject, environment)
        if reason:
            return reason
    return None


def _authorization_denial(policy: Policy, subject, environment: str) -> str | None:
    from apps.lifecycle import decisions
    from apps.lifecycle.models import OPEN_CHANGE_STATUSES, AuthorizationStatus
    from apps.lifecycle.services import AuthorizationService

    label = _subject_label(subject)
    authorization = AuthorizationService.baseline(subject)
    if authorization is None:
        pending = AuthorizationService.pending(subject)
        if pending is not None:
            return (
                f"{label} awaits a decision at Gate G3 "
                f"({decisions.remaining(pending)} signature(s) to go)."
            )
        return f"{label} has no deployment authorization (IRGF S7 · Gate G3)."
    if environment and authorization.environment != environment:
        return (
            f"{label} is authorized for {authorization.get_environment_display().lower()}, "
            f"not {environment} (IRGF S7 · Gate G3)."
        )
    if authorization.status == AuthorizationStatus.SUSPENDED:
        return f"{label}'s deployment authorization is suspended. {authorization.suspension_reason}"
    if policy.option("block_pending_changes", False) and authorization.changes.filter(
        status__in=OPEN_CHANGE_STATUSES
    ).exists():
        return f"{label} has changes awaiting Gate G4."
    return None


def excepted_policy_ids(organization, agent) -> set:
    """Policies an active IRGF M17 exception sets aside for this agent."""
    if agent is None or getattr(agent, "pk", None) is None:
        return set()
    from apps.lifecycle.services import ExceptionService

    return ExceptionService.active_policy_ids(organization, agent)


EVALUATORS: dict[str, Callable[[Policy, PolicyContext], "str | None"]] = {
    PolicyType.DEPLOYMENT_AUTHORIZATION: _check_deployment_authorization,
    PolicyType.MODEL_APPROVAL: _check_model_approval,
    PolicyType.PROVIDER: _check_provider,
    PolicyType.DATA_CLASSIFICATION: _check_data_classification,
    PolicyType.AGENT_AUTONOMY: _check_agent_autonomy,
    PolicyType.COST_LIMIT: _check_cost_limit,
    PolicyType.TOOL_ACCESS: _check_tool_access,
    PolicyType.HUMAN_APPROVAL: _check_human_approval,
    PolicyType.RISK_CLASSIFICATION: _check_risk_classification,
}


def policy_tiers(policy: Policy) -> set[int]:
    """The IRGF tiers a policy is scoped to. Empty means it is not tier-scoped."""
    tiers = set()
    for value in policy.option("tiers", []) or []:
        try:
            tiers.add(int(value))
        except (TypeError, ValueError):
            continue
    return tiers


def in_tier_scope(policy: Policy, tier: int | None) -> bool:
    """Whether a policy applies at this tier. Unclassified meets no tier scope."""
    scope = policy_tiers(policy)
    return not scope or tier in scope


class PolicyEngine:
    """Evaluates every active policy against one attempted action."""

    @staticmethod
    def policies_for(organization):
        return Policy.objects.for_organization(organization).filter(status="active")

    @classmethod
    def evaluate(cls, organization, context: PolicyContext, *, types=None) -> PolicyDecision:
        """Evaluate every active policy, or only the given types.

        The type filter exists for callers that evaluate in two stages -- the
        gateway checks agent-level policies before routing and model-level ones
        against the model routing actually chose.
        """
        decision = PolicyDecision()
        if organization is None:
            return decision

        excepted = excepted_policy_ids(organization, context.agent)
        for policy in cls.policies_for(organization):
            if types is not None and policy.policy_type not in types:
                continue
            if not in_tier_scope(policy, context.effective_tier):
                # Scoped to IRGF tiers this request is not at.
                continue
            if policy.pk in excepted:
                # Set aside for this agent by an active IRGF M17 exception.
                decision.excepted.append(policy)
                continue
            evaluator = EVALUATORS.get(policy.policy_type)
            if evaluator is None:
                logger.warning("No evaluator for policy type %s", policy.policy_type)
                continue

            try:
                reason = evaluator(policy, context)
            except Exception:
                # A broken policy must not take down the whole check; failing
                # open here is safe because the caller still sees every other
                # policy's verdict.
                logger.exception("Policy %s raised during evaluation", policy.pk)
                continue

            if not reason:
                continue

            decision.matches.append(PolicyMatch(policy=policy, reason=reason))
            if policy.effect == PolicyEffect.DENY:
                decision.allowed = False
            elif policy.effect == PolicyEffect.REQUIRE_APPROVAL:
                decision.requires_approval = True

        if decision.matches:
            decision.severity = max(
                (m.policy.severity for m in decision.matches), key=_rank
            )
        return decision

    @classmethod
    def record(
        cls,
        organization,
        context: PolicyContext,
        decision: PolicyDecision,
        requested_action: str = "",
    ) -> list[PolicyViolation]:
        """Persist denials and warnings, and mark the execution timeline.

        Approval routing is not a violation -- it is the system working as
        designed -- so only DENY and WARN are recorded here.
        """
        recorded: list[PolicyViolation] = []
        notable = [
            m for m in decision.matches
            if m.policy.effect in {PolicyEffect.DENY, PolicyEffect.WARN}
        ]
        if not notable:
            return recorded

        for match in notable:
            recorded.append(
                PolicyViolation.objects.create(
                    organization=organization,
                    policy=match.policy,
                    execution=context.execution,
                    agent=context.agent,
                    application=context.application,
                    model=context.effective_model,
                    provider=context.effective_provider,
                    effect=match.policy.effect,
                    severity=match.policy.severity,
                    reason=match.reason,
                    requested_action=requested_action or context.action,
                    context=context.as_metadata(),
                )
            )

        if context.execution is not None:
            from apps.executions.services import ExecutionService

            ExecutionService.append_events(
                context.execution,
                [
                    {
                        "event_type": str(EventType.POLICY_VIOLATION),
                        "error_message": match.reason,
                        "metadata": {
                            "policy": match.policy.name,
                            "effect": match.policy.effect,
                            "severity": match.policy.severity,
                        },
                    }
                    for match in notable
                ],
            )

        from apps.notifications import emitters

        for violation in recorded:
            emitters.policy_violation(violation)

        return recorded

    @classmethod
    def check(
        cls, organization, context: PolicyContext, requested_action: str = "", record: bool = True
    ) -> PolicyDecision:
        """Evaluate and, by default, record what was blocked or flagged."""
        decision = cls.evaluate(organization, context)
        if record:
            cls.record(organization, context, decision, requested_action)
        return decision

    #: Policy types whose verdict depends on *which model* serves, rather than
    #: on the agent asking. A caller that routes has to check these against the
    #: model it chose, because the model an agent declares is not always the
    #: one that runs -- and checking the declared one produces both false
    #: denials and, worse, false permissions.
    MODEL_DEPENDENT_TYPES = frozenset({PolicyType.MODEL_APPROVAL, PolicyType.PROVIDER})

    #: Everything else: decided by the agent and the request, not the model.
    AGENT_LEVEL_TYPES = frozenset(PolicyType.values) - MODEL_DEPENDENT_TYPES

    @classmethod
    def check_agent(
        cls, organization, context: PolicyContext, requested_action: str = "", record: bool = True
    ) -> PolicyDecision:
        """Everything decidable before a model has been chosen."""
        decision = cls.evaluate(organization, context, types=cls.AGENT_LEVEL_TYPES)
        if record and decision.matches:
            cls.record(organization, context, decision, requested_action)
        return decision

    @classmethod
    def check_model(
        cls, organization, model, context: PolicyContext, *, requested_action: str = "",
        record: bool = True,
    ) -> PolicyDecision:
        """Evaluate only the model-dependent policies, against one model.

        The gateway calls this per routing candidate. Re-running the whole
        engine for each would repeat agent-level checks that cannot change
        between candidates, and would multiply the work on the request path.
        """
        if organization is None or model is None:
            return PolicyDecision()

        scoped = replace(context, model=model, provider=model.provider)
        decision = cls.evaluate(organization, scoped, types=cls.MODEL_DEPENDENT_TYPES)
        if record and decision.matches:
            cls.record(organization, scoped, decision, requested_action)
        return decision
