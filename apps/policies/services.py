"""The policy engine (section 18).

One evaluator per policy type. Each reads only the typed settings its type
declares, so evaluation stays explicit and auditable -- there is no generic
expression evaluation anywhere in here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
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


EVALUATORS: dict[str, Callable[[Policy, PolicyContext], "str | None"]] = {
    PolicyType.MODEL_APPROVAL: _check_model_approval,
    PolicyType.PROVIDER: _check_provider,
    PolicyType.DATA_CLASSIFICATION: _check_data_classification,
    PolicyType.AGENT_AUTONOMY: _check_agent_autonomy,
    PolicyType.COST_LIMIT: _check_cost_limit,
    PolicyType.TOOL_ACCESS: _check_tool_access,
    PolicyType.HUMAN_APPROVAL: _check_human_approval,
}


class PolicyEngine:
    """Evaluates every active policy against one attempted action."""

    @staticmethod
    def policies_for(organization):
        return Policy.objects.for_organization(organization).filter(status="active")

    @classmethod
    def evaluate(cls, organization, context: PolicyContext) -> PolicyDecision:
        decision = PolicyDecision()
        if organization is None:
            return decision

        for policy in cls.policies_for(organization):
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
