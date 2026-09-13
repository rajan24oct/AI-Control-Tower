"""Running an evaluation suite.

Cases are executed through the AI Gateway rather than by calling providers
here. That is the whole reason the gateway exists: evaluation traffic then
inherits routing, cost attribution, the execution record and the live monitor,
and an eval is measuring the same path production uses rather than a private
one that only resembles it.

Two consequences of that choice are worth stating, because they surprise people:

  * A case can fail because a **policy** refused it. That is a real result, not
    a broken test -- if production would refuse this prompt, the evaluation
    should say so rather than route around the control to get a score.
  * An evaluation costs real money against real providers. The run records what
    it spent, and a suite can put a ceiling on it per case.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Sum
from django.db import transaction
from django.utils import timezone

from apps.evaluations import scorers
from apps.evaluations.models import (
    EvaluationCase,
    EvaluationResult,
    EvaluationRun,
    EvaluationSuite,
    RunStatus,
    TargetKind,
)
from apps.gateway.services import GatewayError, GatewayRequest, GatewayService

logger = logging.getLogger(__name__)


class EvaluationError(Exception):
    """The run could not be set up. Distinct from cases failing."""


@dataclass
class CaseOutcome:
    passed: bool
    detail: str
    output: str = ""
    latency_ms: int | None = None
    cost: Decimal = Decimal("0")
    execution: object = None
    error: str = ""


class EvaluationService:
    # -- Starting -----------------------------------------------------------

    @staticmethod
    def create_run(suite: EvaluationSuite, *, model=None, agent=None,
                   triggered_by=None, triggered_by_system="") -> EvaluationRun:
        """Register a run. Does not execute it."""
        if model is None and agent is None:
            raise EvaluationError("An evaluation needs a model or an agent to run against.")
        if suite.target_kind == TargetKind.MODEL and model is None:
            raise EvaluationError(f"'{suite.name}' evaluates models; no model was given.")
        if suite.target_kind == TargetKind.AGENT and agent is None:
            raise EvaluationError(f"'{suite.name}' evaluates agents; no agent was given.")

        target = model or agent
        if target.organization_id != suite.organization_id:
            # Checked explicitly rather than trusted from the queryset that
            # fetched it: an eval crossing tenants would put one customer's
            # prompts through another's provider account.
            raise EvaluationError("The suite and its target belong to different organizations.")

        case_count = suite.cases.count()
        if not case_count:
            raise EvaluationError(f"'{suite.name}' has no cases.")

        return EvaluationRun.objects.create(
            organization=suite.organization,
            suite=suite,
            model=model,
            agent=agent,
            triggered_by=triggered_by,
            triggered_by_system=triggered_by_system,
            total_cases=case_count,
            status=RunStatus.QUEUED,
        )

    # -- Executing ----------------------------------------------------------

    @classmethod
    def execute(cls, run: EvaluationRun) -> EvaluationRun:
        """Work through every case, recording each as it completes.

        Progress is written case by case rather than at the end, so a suite that
        takes minutes is legible while it runs and a crash halfway through
        leaves evidence rather than nothing.
        """
        if run.is_finished:
            return run

        run.status = RunStatus.RUNNING
        run.started_at = timezone.now()
        run.save(update_fields=["status", "started_at", "updated_at"])

        cases = list(run.suite.cases.all())
        try:
            for case in cases:
                outcome = cls._run_case(run, case)
                cls._record_result(run, case, outcome)
        except Exception as exc:
            logger.exception("Evaluation run %s failed", run.pk)
            run.status = RunStatus.FAILED
            run.error_message = f"{type(exc).__name__}: {exc}"[:2000]
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "error_message", "finished_at", "updated_at"])
            return run

        return cls._finalise(run)

    @classmethod
    def _run_case(cls, run: EvaluationRun, case: EvaluationCase) -> CaseOutcome:
        messages = []
        if case.system_prompt:
            messages.append({"role": "system", "content": case.system_prompt})
        messages.append({"role": "user", "content": case.prompt})

        request = GatewayRequest(
            agent=run.agent,
            messages=messages,
            # A model-targeted suite pins the model, so the suite measures what
            # it says it measures rather than whatever routing preferred today.
            model=run.model.model_id if run.model_id else "",
            environment=run.suite.environment,
            metadata={"evaluation_run": str(run.pk), "evaluation_case": case.name},
        )

        try:
            response = GatewayService.complete(
                run.organization, request, service_account=None
            )
        except GatewayError as exc:
            # A refusal is a result. Production would have refused too.
            return CaseOutcome(
                passed=False,
                detail=f"The gateway refused this case: {exc.message}",
                error=exc.code,
            )
        except Exception as exc:
            return CaseOutcome(
                passed=False,
                detail=f"The call failed: {type(exc).__name__}: {exc}",
                error=type(exc).__name__,
            )

        outcome = scorers.apply(
            case,
            response.output_text,
            {"latency_ms": response.latency_ms, "cost": response.cost},
        )
        return CaseOutcome(
            passed=outcome.passed,
            detail=outcome.detail,
            output=response.output_text,
            latency_ms=response.latency_ms,
            cost=response.cost,
            execution=response.execution,
        )

    @staticmethod
    def _record_result(run: EvaluationRun, case: EvaluationCase, outcome: CaseOutcome) -> None:
        with transaction.atomic():
            EvaluationResult.objects.create(
                organization=run.organization,
                run=run,
                case=case,
                case_name=case.name,
                category=case.category,
                passed=outcome.passed,
                detail=outcome.detail,
                output=outcome.output,
                execution=outcome.execution,
                latency_ms=outcome.latency_ms,
                cost=outcome.cost,
                weight=case.weight,
                error_message=outcome.error,
            )
            run.completed_cases += 1
            if outcome.passed:
                run.passed_cases += 1
            run.total_cost += outcome.cost
            run.save(
                update_fields=["completed_cases", "passed_cases", "total_cost", "updated_at"]
            )

    @classmethod
    def _finalise(cls, run: EvaluationRun) -> EvaluationRun:
        results = list(run.results.all())
        run.score = cls.weighted_score(results)

        blocking = run.suite.blocking
        blocked = next(
            (r.category for r in results if not r.passed and r.category in blocking), ""
        )

        run.blocked_by_category = blocked
        # Both bars have to be cleared. A suite that scores 95% while failing a
        # red-team case has not passed, and saying otherwise would be the most
        # dangerous kind of green tick.
        run.passed = bool(run.score >= run.suite.pass_threshold) and not blocked
        run.status = RunStatus.COMPLETED
        run.finished_at = timezone.now()
        run.save(
            update_fields=[
                "score", "passed", "blocked_by_category", "status", "finished_at", "updated_at",
            ]
        )
        return run

    @staticmethod
    def weighted_score(results) -> Decimal:
        """Percentage of weight passed, to two places."""
        total = sum(r.weight for r in results)
        if not total:
            return Decimal("0")
        earned = sum(r.weight for r in results if r.passed)
        return (Decimal(earned) / Decimal(total) * 100).quantize(Decimal("0.01"))

    # -- Reading ------------------------------------------------------------

    @staticmethod
    def latest_for_model(model) -> EvaluationRun | None:
        return (
            EvaluationRun.objects.filter(model=model, status=RunStatus.COMPLETED)
            .select_related("suite")
            .order_by("-finished_at")
            .first()
        )

    @staticmethod
    def category_breakdown(run: EvaluationRun) -> list[dict]:
        """Per-category pass counts, so a failure is readable at a glance."""
        buckets: dict[str, dict] = {}
        for result in run.results.all():
            bucket = buckets.setdefault(
                result.category, {"category": result.category, "total": 0, "passed": 0}
            )
            bucket["total"] += 1
            if result.passed:
                bucket["passed"] += 1

        blocking = run.suite.blocking
        for bucket in buckets.values():
            bucket["blocking"] = bucket["category"] in blocking
            bucket["failed"] = bucket["total"] - bucket["passed"]
        return sorted(buckets.values(), key=lambda b: b["category"])


class EvaluationStats:
    """Aggregates for the Evaluation Centre landing page."""

    def __init__(self, organization):
        self.organization = organization

    def build(self) -> dict:
        runs = EvaluationRun.objects.for_organization(self.organization)
        completed = runs.filter(status=RunStatus.COMPLETED)

        return {
            "suites": EvaluationSuite.objects.for_organization(self.organization).count(),
            "runs": runs.count(),
            "in_flight": runs.filter(
                status__in=[RunStatus.QUEUED, RunStatus.RUNNING]
            ).count(),
            "passed": completed.filter(passed=True).count(),
            "failed": completed.filter(passed=False).count(),
            "blocked": completed.exclude(blocked_by_category="").count(),
            "spend": completed.aggregate(total=Sum("total_cost"))["total"] or Decimal("0"),
            "unevaluated_approved_models": self.unevaluated_approved_count(),
        }

    def unevaluated_approved_count(self) -> int:
        """Approved models with no completed evaluation behind them.

        The number a governance manager actually wants: how much of what we have
        blessed has never been tested.
        """
        from apps.models_registry.models import AIModel

        return (
            AIModel.objects.for_organization(self.organization)
            .filter(approved_for_use=True)
            .exclude(evaluation_runs__status=RunStatus.COMPLETED)
            .distinct()
            .count()
        )
