"""Building a routing plan.

Selection is two passes, kept separate on purpose:

  1. **Admissibility** -- hard rules. A candidate either may serve this work or
     may not: unapproved, blocked, unhealthy, too small a context window, over
     the cost ceiling, missing a capability. There is no score that rescues a
     candidate that fails here, and every exclusion is recorded with its reason,
     because "why is there no route?" is the question operators actually ask.

  2. **Ranking** -- soft signals. Cost from the registry, latency and failure
     rate observed from this organization's own recent executions. Each is
     normalised against the candidate field so the weights mean the same thing
     whatever the estate looks like.

Observed signals come from history, not from a vendor's claims, so a model that
has been failing here ranks below one that has not -- regardless of what its
datasheet says.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Iterable

from django.db.models import Count
from django.utils import timezone

from apps.core.constants import Environment, HealthStatus, LifecycleStatus
from apps.models_registry.models import AIModel, ModelType
from apps.routing.models import (
    RouteOutcome,
    RoutingDecision,
    RoutingPolicy,
    RoutingStrategy,
)

#: How far back observed reliability and latency are measured. Long enough to
#: be stable, short enough that a provider that recovered last week is not
#: still being punished for it.
OBSERVATION_DAYS = 7

#: Below this many observations a model's own history is not trusted on its
#: own; it is blended toward the neutral 0.5 so a single unlucky run cannot
#: bury a model, and one lucky run cannot promote one.
CONFIDENCE_FLOOR = 20

HEALTH_SCORES = {
    HealthStatus.HEALTHY: 1.0,
    HealthStatus.DEGRADED: 0.55,
    HealthStatus.UNKNOWN: 0.75,
}


@dataclass
class RoutingRequest:
    """What the caller needs a model for."""

    model_type: str = ModelType.CHAT
    environment: str = Environment.PRODUCTION
    agent: object = None
    execution: object = None
    required_capabilities: tuple[str, ...] = ()
    min_context_window: int | None = None
    max_input_cost_per_1k: Decimal | None = None
    #: Models already tried and failed in this attempt, so a retry gets a plan
    #: that does not begin with the thing that just broke.
    exclude_model_ids: tuple[str, ...] = ()


@dataclass
class Signals:
    cost: float = 0.0
    latency: float = 0.0
    reliability: float = 0.0
    health: float = 1.0
    observations: int = 0
    failure_rate: float | None = None
    median_latency_ms: int | None = None


@dataclass
class Candidate:
    model: AIModel
    score: float = 0.0
    signals: Signals = field(default_factory=Signals)
    reason: str = ""

    def as_entry(self, position: int) -> dict:
        return {
            "position": position,
            "model_id": str(self.model.pk),
            "model_key": self.model.model_id,
            "model_name": self.model.model_name,
            "provider": self.model.provider.name,
            "provider_id": str(self.model.provider_id),
            "score": round(self.score, 4),
            "reason": self.reason,
            "signals": {
                "cost": round(self.signals.cost, 3),
                "latency": round(self.signals.latency, 3),
                "reliability": round(self.signals.reliability, 3),
                "health": round(self.signals.health, 3),
                "observations": self.signals.observations,
                "failure_rate": self.signals.failure_rate,
                "median_latency_ms": self.signals.median_latency_ms,
            },
        }


@dataclass
class Exclusion:
    model: AIModel
    reason: str

    def as_entry(self) -> dict:
        return {
            "model_id": str(self.model.pk),
            "model_name": self.model.model_name,
            "provider": self.model.provider.name,
            "reason": self.reason,
        }


@dataclass
class RoutePlan:
    policy: RoutingPolicy | None
    strategy: str
    candidates: list[Candidate]
    exclusions: list[Exclusion]
    reason: str = ""

    @property
    def primary(self) -> AIModel | None:
        return self.candidates[0].model if self.candidates else None

    @property
    def has_route(self) -> bool:
        return bool(self.candidates)

    def as_payload(self, decision: RoutingDecision | None = None) -> dict:
        """The stable contract an engine consumes."""
        payload = {
            "routed": self.has_route,
            "strategy": self.strategy,
            "policy": self.policy.name if self.policy else None,
            "policy_id": str(self.policy.pk) if self.policy else None,
            "reason": self.reason,
            "primary": self.candidates[0].as_entry(1) if self.candidates else None,
            "fallbacks": [
                candidate.as_entry(index)
                for index, candidate in enumerate(self.candidates[1:], start=2)
            ],
            "excluded": [exclusion.as_entry() for exclusion in self.exclusions],
        }
        if decision is not None:
            payload["decision_id"] = str(decision.pk)
        return payload


class RoutingService:
    """Chooses models. Never calls them."""

    # -- Entry points -------------------------------------------------------

    @classmethod
    def decide(
        cls, organization, request: RoutingRequest, *, service_account=None
    ) -> tuple[RoutePlan, RoutingDecision]:
        """Build a plan and record it. The recorded plan is what was sent."""
        plan = cls.plan(organization, request)
        decision = cls._record(organization, request, plan, service_account)
        return plan, decision

    @classmethod
    def plan(cls, organization, request: RoutingRequest) -> RoutePlan:
        policy = cls.policy_for(organization, request)
        candidates, exclusions = cls._admissible(organization, request, policy)

        if not candidates:
            return RoutePlan(
                policy=policy,
                strategy=policy.strategy if policy else RoutingStrategy.BALANCED,
                candidates=[],
                exclusions=exclusions,
                reason=cls._no_route_reason(exclusions),
            )

        weights = policy.weights if policy else RoutingPolicy().weights
        cls._score(organization, candidates, weights)
        candidates.sort(key=lambda candidate: candidate.score, reverse=True)

        depth = policy.chain_length if policy else 3
        chosen = candidates[:depth]
        cls._explain(chosen, weights)

        return RoutePlan(
            policy=policy,
            strategy=policy.strategy if policy else RoutingStrategy.BALANCED,
            candidates=chosen,
            exclusions=exclusions,
            reason=f"{len(chosen)} of {len(candidates)} eligible models offered.",
        )

    @staticmethod
    def policy_for(organization, request: RoutingRequest) -> RoutingPolicy | None:
        """The most specific active policy for this work, or None.

        Agent-scoped policies win over estate-wide ones at the same priority --
        a rule written for one agent is a deliberate exception, and a general
        rule should not quietly outrank it.
        """
        policies = (
            RoutingPolicy.objects.for_organization(organization)
            .filter(status=LifecycleStatus.ACTIVE, model_type=request.model_type)
            .prefetch_related("agents", "allowed_providers", "blocked_providers")
            .order_by("priority", "name")
        )

        general: RoutingPolicy | None = None
        for policy in policies:
            if not policy.applies_to_environment(request.environment):
                continue
            agent_ids = {agent.pk for agent in policy.agents.all()}
            if agent_ids:
                if request.agent is not None and request.agent.pk in agent_ids:
                    return policy
                continue
            if general is None:
                general = policy
        return general

    # -- Pass one: admissibility -------------------------------------------

    @classmethod
    def _admissible(
        cls, organization, request: RoutingRequest, policy: RoutingPolicy | None
    ) -> tuple[list[Candidate], list[Exclusion]]:
        queryset = (
            AIModel.objects.for_organization(organization)
            .filter(model_type=request.model_type, status=LifecycleStatus.ACTIVE)
            .select_related("provider")
        )

        required = set(request.required_capabilities) | set(
            policy.required_capabilities if policy else []
        )
        min_context = max(
            filter(None, [request.min_context_window, policy.min_context_window if policy else None]),
            default=None,
        )
        cost_ceiling = cls._tightest(
            request.max_input_cost_per_1k,
            policy.max_input_cost_per_1k if policy else None,
        )
        allowed_ids = {p.pk for p in policy.allowed_providers.all()} if policy else set()
        blocked_ids = {p.pk for p in policy.blocked_providers.all()} if policy else set()
        excluded_models = {str(value) for value in request.exclude_model_ids}

        candidates: list[Candidate] = []
        exclusions: list[Exclusion] = []

        for model in queryset:
            reason = cls._inadmissible(
                model,
                policy=policy,
                required=required,
                min_context=min_context,
                cost_ceiling=cost_ceiling,
                allowed_ids=allowed_ids,
                blocked_ids=blocked_ids,
                excluded_models=excluded_models,
            )
            if reason:
                exclusions.append(Exclusion(model, reason))
            else:
                candidates.append(Candidate(model))

        return candidates, exclusions

    @staticmethod
    def _inadmissible(model, *, policy, required, min_context, cost_ceiling,
                      allowed_ids, blocked_ids, excluded_models) -> str:
        """The reason this model may not serve, or an empty string."""
        if str(model.pk) in excluded_models or model.model_id in excluded_models:
            return "Already attempted in this request."

        if policy is not None and policy.require_approved_models and not model.approved_for_use:
            return "Not approved for use."

        if blocked_ids and model.provider_id in blocked_ids:
            return f"Provider {model.provider.name} is blocked by the policy."
        if allowed_ids and model.provider_id not in allowed_ids:
            return f"Provider {model.provider.name} is not on the policy's allowed list."

        if model.provider.status != LifecycleStatus.ACTIVE:
            return f"Provider {model.provider.name} is not active."

        health = model.provider.health_status
        if health == HealthStatus.UNHEALTHY:
            # Never a fallback either: offering a chain entry known to be down
            # would make the fallback itself a source of latency.
            return f"Provider {model.provider.name} is unhealthy."
        if health == HealthStatus.DEGRADED and policy is not None and not policy.allow_degraded_providers:
            return f"Provider {model.provider.name} is degraded."

        missing = required - {str(capability) for capability in (model.capabilities or [])}
        if missing:
            return f"Missing capability: {', '.join(sorted(missing))}."

        if min_context and (model.context_window or 0) < min_context:
            return f"Context window {model.context_window or 0} is below {min_context}."

        if cost_ceiling is not None and model.input_cost_per_1k > cost_ceiling:
            return f"Input rate {model.input_cost_per_1k} exceeds the ceiling {cost_ceiling}."

        return ""

    @staticmethod
    def _tightest(*values):
        present = [value for value in values if value is not None]
        return min(present) if present else None

    @staticmethod
    def _no_route_reason(exclusions: list[Exclusion]) -> str:
        if not exclusions:
            return "No models of this type are registered."
        return f"All {len(exclusions)} candidate models were excluded."

    # -- Pass two: ranking --------------------------------------------------

    @classmethod
    def _score(cls, organization, candidates: list[Candidate], weights: dict) -> None:
        observed = cls._observed(organization, [c.model.pk for c in candidates])

        costs = [float(c.model.input_cost_per_1k or 0) for c in candidates]
        latencies = [
            observed.get(c.model.pk, {}).get("median_latency_ms")
            for c in candidates
        ]
        known_latencies = [value for value in latencies if value]

        for candidate in candidates:
            stats = observed.get(candidate.model.pk, {})
            signals = candidate.signals

            signals.observations = stats.get("count", 0)
            signals.failure_rate = stats.get("failure_rate")
            signals.median_latency_ms = stats.get("median_latency_ms")

            signals.cost = _inverse_normal(float(candidate.model.input_cost_per_1k or 0), costs)
            signals.latency = (
                _inverse_normal(signals.median_latency_ms, known_latencies)
                if signals.median_latency_ms
                else 0.5  # unmeasured: neither rewarded nor punished
            )
            signals.reliability = _confidence_weighted(
                1.0 - signals.failure_rate if signals.failure_rate is not None else 0.5,
                signals.observations,
            )
            signals.health = HEALTH_SCORES.get(candidate.model.provider.health_status, 0.75)

            weighted = (
                weights["cost"] * signals.cost
                + weights["latency"] * signals.latency
                + weights["reliability"] * signals.reliability
            )
            # Health scales the whole score rather than adding to it: a degraded
            # provider should slip behind a healthy one on every strategy, not
            # just the ones that happen to weight reliability highly.
            candidate.score = weighted * signals.health

    @staticmethod
    def _observed(organization, model_ids: Iterable) -> dict:
        """Failure rate and median latency per model, from recent executions."""
        from apps.executions.models import Execution, ExecutionStatusChoices

        model_ids = list(model_ids)
        if not model_ids:
            return {}

        since = timezone.now() - timedelta(days=OBSERVATION_DAYS)
        rows = (
            Execution.objects.for_organization(organization)
            .filter(model_id__in=model_ids, started_at__gte=since)
            .exclude(status__in=[ExecutionStatusChoices.QUEUED, ExecutionStatusChoices.RUNNING])
            .values_list("model_id", "status", "latency_ms")
        )

        buckets: dict = {}
        for model_id, status, latency in rows:
            bucket = buckets.setdefault(model_id, {"total": 0, "failed": 0, "latencies": []})
            bucket["total"] += 1
            if status in {ExecutionStatusChoices.FAILED, ExecutionStatusChoices.TIMED_OUT}:
                bucket["failed"] += 1
            if latency:
                bucket["latencies"].append(latency)

        return {
            model_id: {
                "count": bucket["total"],
                "failure_rate": bucket["failed"] / bucket["total"] if bucket["total"] else None,
                "median_latency_ms": (
                    int(statistics.median(bucket["latencies"])) if bucket["latencies"] else None
                ),
            }
            for model_id, bucket in buckets.items()
        }

    @staticmethod
    def _explain(candidates: list[Candidate], weights: dict) -> None:
        """Say why each candidate placed where it did, in the operator's terms."""
        for position, candidate in enumerate(candidates, start=1):
            signals = candidate.signals
            parts = []

            leading = max(("cost", "latency", "reliability"), key=lambda key: weights[key])
            if leading == "cost":
                parts.append(f"${candidate.model.input_cost_per_1k}/1k input")
            elif leading == "latency" and signals.median_latency_ms:
                parts.append(f"{signals.median_latency_ms} ms median")
            elif signals.failure_rate is not None:
                parts.append(f"{signals.failure_rate:.0%} recent failure rate")

            if signals.observations < CONFIDENCE_FLOOR:
                parts.append(f"only {signals.observations} recent runs")
            if signals.health < 1.0:
                parts.append(f"provider {candidate.model.provider.get_health_status_display().lower()}")

            role = "Primary" if position == 1 else f"Fallback {position - 1}"
            candidate.reason = f"{role}: " + (", ".join(parts) if parts else "no signal against it")

    # -- Recording ----------------------------------------------------------

    @staticmethod
    def _record(organization, request, plan: RoutePlan, service_account) -> RoutingDecision:
        return RoutingDecision.objects.create(
            organization=organization,
            policy=plan.policy,
            agent=request.agent,
            execution=request.execution,
            service_account=service_account,
            model_type=request.model_type,
            environment=request.environment,
            strategy=plan.strategy,
            selected_model=plan.primary,
            chain=[
                candidate.as_entry(index)
                for index, candidate in enumerate(plan.candidates, start=1)
            ],
            excluded=[exclusion.as_entry() for exclusion in plan.exclusions],
            reason=plan.reason[:500],
            outcome=RouteOutcome.PENDING if plan.has_route else RouteOutcome.EXHAUSTED,
        )

    @staticmethod
    def report_outcome(decision: RoutingDecision, *, model=None, attempt=None,
                       outcome=RouteOutcome.SERVED, detail="") -> RoutingDecision:
        """Close the loop: which entry in the plan actually served."""
        decision.served_model = model
        decision.attempt = attempt
        decision.outcome = outcome
        decision.outcome_detail = detail[:500]
        decision.outcome_reported_at = timezone.now()
        decision.save(
            update_fields=[
                "served_model", "attempt", "outcome", "outcome_detail",
                "outcome_reported_at", "updated_at",
            ]
        )
        return decision


def _inverse_normal(value, population) -> float:
    """1.0 for the smallest value in the field, 0.0 for the largest.

    Normalising against the candidates actually on offer, rather than an
    absolute scale, is what lets one set of weights work for an estate of
    cheap small models and an estate of frontier ones alike.
    """
    if value is None or not population:
        return 0.5
    low, high = min(population), max(population)
    if high == low:
        return 1.0
    return 1.0 - ((value - low) / (high - low))


def _confidence_weighted(score: float, observations: int) -> float:
    """Blend a measured score toward neutral when there is little to go on."""
    if observations >= CONFIDENCE_FLOOR:
        return score
    weight = observations / CONFIDENCE_FLOOR
    return (score * weight) + (0.5 * (1 - weight))


class RoutingStats:
    """Aggregates for the routing dashboard."""

    def __init__(self, organization, days: int = 7):
        self.organization = organization
        self.since = timezone.now() - timedelta(days=days)

    def build(self) -> dict:
        decisions = RoutingDecision.objects.for_organization(self.organization).filter(
            created_at__gte=self.since
        )
        total = decisions.count()
        served = decisions.filter(outcome=RouteOutcome.SERVED)
        fell_back = served.filter(attempt__gt=1).count()
        no_route = decisions.filter(chain=[]).count()

        return {
            "total": total,
            "served": served.count(),
            "fell_back": fell_back,
            # The headline number: how often the first choice was not usable.
            "fallback_rate": (fell_back / served.count()) if served.count() else 0.0,
            "no_route": no_route,
            "exhausted": decisions.filter(outcome=RouteOutcome.EXHAUSTED).exclude(chain=[]).count(),
            "pending": decisions.filter(outcome=RouteOutcome.PENDING).count(),
            "top_models": self._top_models(served),
        }

    @staticmethod
    def _top_models(served) -> list[dict]:
        rows = (
            served.exclude(served_model=None)
            .values("served_model__model_name", "served_model__provider__name")
            .annotate(count=Count("id"))
            .order_by("-count")[:5]
        )
        return [
            {
                "model": row["served_model__model_name"],
                "provider": row["served_model__provider__name"],
                "count": row["count"],
            }
            for row in rows
        ]
