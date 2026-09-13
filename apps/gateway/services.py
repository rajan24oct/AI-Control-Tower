"""The gateway request pipeline.

An agent's model call arrives here and passes through, in order:

    identity -> policy -> budget -> routing -> attempt chain -> record

The first three are the reason the gateway exists. Everywhere else in the
platform a policy verdict is advice an engine may ignore; here the platform is
holding the request, so `deny` actually denies and a blown budget actually
stops. That is the whole trade the gateway makes: real enforcement in exchange
for being in the request path.

Being in the request path sets the rules the rest of this module follows:

  * Every upstream call has a timeout, and the whole request has a deadline.
    An attempt that would start past the deadline is not started.
  * A provider that keeps failing is taken out of the chain by its breaker,
    so a dead upstream costs one probe rather than every request's patience.
  * Recording is best-effort and happens after the answer is in hand. The
    Control Tower failing to write its own telemetry must never turn a
    successful model call into a failed one for the caller.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.constants import Environment
from apps.gateway.models import CircuitState, ProviderCircuit
from apps.routing.services import RoutingRequest, RoutingService
from integrations.base.capabilities import Capability
from integrations.base.events import ExecutionStatus
from integrations.base.exceptions import AdapterError
from integrations.base.types import ExecutionRequest

logger = logging.getLogger(__name__)

#: Roughly four characters per token. Only used when a provider streams without
#: reporting usage at all; the result is always marked as an estimate.
CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


def config() -> dict:
    return settings.CONTROL_TOWER.get("GATEWAY", {})


def setting(name: str, default):
    return config().get(name, default)


class GatewayError(Exception):
    """A request the gateway will not or cannot serve.

    Carries an HTTP status because the gateway's callers are HTTP clients that
    need to tell "you may not" apart from "nothing worked".
    """

    status_code = 502
    code = "gateway_error"

    def __init__(self, message: str, *, detail=None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class PolicyDenied(GatewayError):
    status_code = 403
    code = "policy_denied"


class ApprovalRequired(GatewayError):
    # 202: the request is legitimate and has been recorded, but a human has to
    # decide before it can proceed. The caller should poll, not retry.
    status_code = 202
    code = "approval_required"


class BudgetExceeded(GatewayError):
    status_code = 402
    code = "budget_exceeded"


class NoRouteAvailable(GatewayError):
    status_code = 503
    code = "no_route"


class AllAttemptsFailed(GatewayError):
    status_code = 502
    code = "all_attempts_failed"


@dataclass
class GatewayRequest:
    """What an agent asked the gateway to do."""

    agent: object = None
    messages: list = field(default_factory=list)
    prompt: str = ""
    model: str = ""
    model_type: str = "chat"
    environment: str = Environment.PRODUCTION
    max_tokens: int | None = None
    temperature: float | None = None
    tools: list = field(default_factory=list)
    trace_id: str = ""
    external_id: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        """The prompt as a single string, for estimation and recording."""
        if self.prompt:
            return self.prompt
        parts = []
        for message in self.messages:
            if isinstance(message, dict):
                parts.append(str(message.get("content", "")))
        return "\n\n".join(part for part in parts if part)


@dataclass
class Attempt:
    model: object
    ok: bool
    latency_ms: int
    error: str = ""
    skipped: str = ""

    def as_entry(self, position: int) -> dict:
        return {
            "position": position,
            "model": self.model.model_name,
            "provider": self.model.provider.name,
            "ok": self.ok,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "skipped": self.skipped,
        }


@dataclass
class GatewayResponse:
    output_text: str
    model: object
    attempts: list[Attempt]
    input_tokens: int
    output_tokens: int
    cost: Decimal
    currency: str
    latency_ms: int
    execution: object = None
    decision: object = None
    raw: dict = field(default_factory=dict)

    @property
    def used_fallback(self) -> bool:
        return len([a for a in self.attempts if not a.skipped]) > 1

    def as_payload(self) -> dict:
        return {
            "output_text": self.output_text,
            "model": self.model.model_id,
            "model_name": self.model.model_name,
            "provider": self.model.provider.name,
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
            },
            "cost": str(self.cost),
            "currency": self.currency,
            "latency_ms": self.latency_ms,
            "used_fallback": self.used_fallback,
            "attempts": [
                attempt.as_entry(index)
                for index, attempt in enumerate(self.attempts, start=1)
            ],
            "execution_id": self.execution.external_id if self.execution else None,
            "routing_decision_id": str(self.decision.pk) if self.decision else None,
        }


class Breaker:
    """Circuit-breaker operations, kept together so the rules live in one place."""

    @staticmethod
    def threshold() -> int:
        return int(setting("CIRCUIT_FAILURE_THRESHOLD", 5))

    @staticmethod
    def cool_off() -> int:
        return int(setting("CIRCUIT_COOL_OFF_SECONDS", 30))

    @classmethod
    def for_providers(cls, provider_ids) -> dict:
        circuits = ProviderCircuit.objects.filter(provider_id__in=list(provider_ids))
        return {circuit.provider_id: circuit for circuit in circuits}

    @classmethod
    def available(cls, circuit) -> bool:
        return circuit is None or circuit.is_available(cool_off_seconds=cls.cool_off())

    @classmethod
    def record_success(cls, provider) -> None:
        """Close the breaker. Only writes when there is something to change."""
        circuit = ProviderCircuit.objects.filter(provider=provider).first()
        if circuit is None or (
            circuit.state == CircuitState.CLOSED and circuit.consecutive_failures == 0
        ):
            return
        circuit.state = CircuitState.CLOSED
        circuit.consecutive_failures = 0
        circuit.opened_at = None
        circuit.last_error = ""
        circuit.save(
            update_fields=["state", "consecutive_failures", "opened_at", "last_error", "updated_at"]
        )

    @classmethod
    def record_failure(cls, provider, error: str) -> None:
        circuit, _ = ProviderCircuit.objects.get_or_create(provider=provider)
        circuit.consecutive_failures += 1
        circuit.last_failure_at = timezone.now()
        circuit.last_error = str(error)[:500]

        if circuit.consecutive_failures >= cls.threshold() and circuit.state != CircuitState.OPEN:
            circuit.state = CircuitState.OPEN
            circuit.opened_at = timezone.now()
            circuit.trips += 1
            logger.warning(
                "Gateway circuit opened for provider %s after %s failures: %s",
                provider.name, circuit.consecutive_failures, error,
            )
        circuit.save()


class GatewayService:
    """Serves a model call on behalf of an agent."""

    @classmethod
    def complete(cls, organization, request: GatewayRequest, *, service_account=None) -> GatewayResponse:
        started = time.monotonic()
        deadline = started + float(setting("REQUEST_DEADLINE_SECONDS", 60))

        cls._enforce_policy(organization, request)
        cls._enforce_budget(organization, request)

        plan, decision = RoutingService.decide(
            organization,
            RoutingRequest(
                model_type=request.model_type,
                environment=request.environment,
                agent=request.agent,
            ),
            service_account=service_account,
        )

        candidates = cls._chain(plan, request)
        if not candidates:
            raise NoRouteAvailable(
                plan.reason or "No model is currently eligible to serve this request.",
                detail={"excluded": [e.as_entry() for e in plan.exclusions]},
            )

        response = cls._attempt_chain(organization, request, candidates, deadline, decision)
        response.latency_ms = int((time.monotonic() - started) * 1000)

        cls._record(organization, request, response, service_account)
        return response

    # -- Streaming ----------------------------------------------------------

    @classmethod
    def stream(cls, organization, request: GatewayRequest, *, service_account=None):
        """Serve a call as a stream of events.

        The rule that shapes everything here: **fallback stops being possible
        the moment the first byte leaves.** The client has already begun
        rendering model A's answer, so quietly retrying on model B would splice
        two different answers into one response and nobody downstream could
        tell. So the chain is walked only up to the first content chunk; after
        that a failure is reported *in* the stream and the stream ends.

        Enforcement therefore happens before a single byte is sent, which is
        also why policy and budget refusals are ordinary HTTP errors rather
        than a 200 carrying bad news.

        Yields `(event_name, payload)` rather than wire format, so the pipeline
        can be tested without parsing SSE.
        """
        started = time.monotonic()
        deadline = started + float(setting("REQUEST_DEADLINE_SECONDS", 60))

        cls._enforce_policy(organization, request)
        cls._enforce_budget(organization, request)

        plan, decision = RoutingService.decide(
            organization,
            RoutingRequest(
                model_type=request.model_type,
                environment=request.environment,
                agent=request.agent,
            ),
            service_account=service_account,
        )
        candidates = cls._chain(plan, request)
        if not candidates:
            raise NoRouteAvailable(
                plan.reason or "No model is currently eligible to serve this request.",
                detail={"excluded": [e.as_entry() for e in plan.exclusions]},
            )

        yield from cls._stream_chain(
            organization, request, candidates, deadline, decision, service_account
        )

    @classmethod
    def _stream_chain(cls, organization, request, candidates, deadline, decision, service_account):
        from apps.routing.models import RouteOutcome
        from apps.routing.services import RoutingService as Routing

        circuits = Breaker.for_providers({model.provider_id for model in candidates})
        attempts: list[Attempt] = []
        last_error = ""

        for model in candidates:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                attempts.append(Attempt(model, False, 0, skipped="Request deadline reached."))
                break

            circuit = circuits.get(model.provider_id)
            if not Breaker.available(circuit):
                attempts.append(
                    Attempt(model, False, 0, skipped=f"Circuit open for {model.provider.name}.")
                )
                continue

            denial = cls._model_denial(organization, request, model)
            if denial:
                attempts.append(Attempt(model, False, 0, skipped=denial))
                last_error = denial
                continue

            adapter = model.provider.get_adapter(
                timeout=min(remaining, float(setting("ATTEMPT_TIMEOUT_SECONDS", 30)))
            )
            if not adapter.supports(Capability.SUPPORTS_STREAMING):
                # Not a failure of the provider, so it must not count against
                # its breaker -- it simply cannot answer this shape of request.
                attempts.append(
                    Attempt(model, False, 0, skipped=f"{model.provider.name} cannot stream.")
                )
                continue

            attempt_started = time.monotonic()
            chunks = None
            first = None
            try:
                chunks = adapter.stream(cls._execution_request(model, request))
                first = cls._first_content(chunks)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"[:500]
                attempts.append(
                    Attempt(model, False, int((time.monotonic() - attempt_started) * 1000),
                            error=last_error)
                )
                Breaker.record_failure(model.provider, last_error)
                logger.warning("Gateway stream failed to open on %s: %s", model.model_name, last_error)
                continue

            attempts.append(Attempt(model, True, int((time.monotonic() - attempt_started) * 1000)))
            Breaker.record_success(model.provider)
            yield from cls._emit(
                organization, request, model, chunks, first, attempts,
                attempt_started, deadline, decision, service_account,
            )
            return

        Routing.report_outcome(
            decision, outcome=RouteOutcome.EXHAUSTED, detail=last_error or "No attempt succeeded."
        )
        cls._raise_exhausted(
            attempts, last_error, "Every model in the route failed before streaming could start."
        )

    @staticmethod
    def _first_content(chunks):
        """Pull chunks until one carries text, or the stream ends.

        Opening a stream is not proof it works -- a provider can accept the
        connection and fail on the first token. Waiting for actual content is
        what makes the fallback window meaningful rather than decorative.
        """
        for chunk in chunks:
            if chunk.text:
                return chunk
            if chunk.is_final:
                return chunk
        return None

    @classmethod
    def _emit(cls, organization, request, model, chunks, first, attempts,
              attempt_started, deadline, decision, service_account):
        from apps.routing.models import RouteOutcome
        from apps.routing.services import RoutingService as Routing

        served_attempt = len([a for a in attempts if not a.skipped])
        yield "start", {
            "model": model.model_id,
            "model_name": model.model_name,
            "provider": model.provider.name,
            "attempt": served_attempt,
            "used_fallback": served_attempt > 1,
        }

        collected: list[str] = []
        usage = None
        failed = ""

        def take(chunk):
            nonlocal usage
            if chunk is None:
                return None
            # A zero-token usage object is not a measurement -- it is what an
            # adapter yields when it has nothing to report. Treating it as one
            # would make a streamed call cost nothing rather than an estimate.
            if chunk.usage and (chunk.usage.input_tokens or chunk.usage.output_tokens):
                usage = chunk.usage
            if chunk.text:
                collected.append(chunk.text)
                return {"text": chunk.text}
            return None

        opening = take(first)
        if opening:
            yield "delta", opening

        try:
            for chunk in chunks:
                if time.monotonic() > deadline:
                    failed = "Request deadline reached mid-stream."
                    break
                payload = take(chunk)
                if payload:
                    yield "delta", payload
        except Exception as exc:
            # Past the first byte there is no going back: the client holds part
            # of this model's answer, so the honest move is to say so rather
            # than splice in another model's.
            failed = f"{type(exc).__name__}: {exc}"[:500]
            Breaker.record_failure(model.provider, failed)
            logger.warning("Gateway stream broke mid-flight on %s: %s", model.model_name, failed)

        text = "".join(collected)
        latency_ms = int((time.monotonic() - attempt_started) * 1000)
        response = GatewayResponse(
            output_text=text,
            model=model,
            attempts=attempts,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            cost=Decimal("0"),
            currency="USD",
            latency_ms=latency_ms,
            decision=decision,
        )
        if usage is None:
            # Nothing reported counts; estimate so a streamed call is not free
            # in the cost dashboard, and mark it so nobody mistakes it for
            # measured usage.
            response.input_tokens = _estimate_tokens(request.text)
            response.output_tokens = _estimate_tokens(text)
        response.cost = cls._price_tokens(model, response.input_tokens, response.output_tokens)

        Routing.report_outcome(
            decision,
            model=model,
            attempt=served_attempt,
            outcome=RouteOutcome.SERVED if not failed else RouteOutcome.EXHAUSTED,
            detail=failed,
        )
        cls._record(
            organization, request, response, service_account,
            streamed=True, estimated_usage=usage is None, error=failed,
        )

        if failed:
            yield "error", {"error": "stream_interrupted", "detail": failed, "recoverable": False}

        yield "done", {
            "model": model.model_id,
            "usage": {
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "total_tokens": response.input_tokens + response.output_tokens,
                "estimated": usage is None,
            },
            "cost": str(response.cost),
            "currency": response.currency,
            "latency_ms": latency_ms,
            "used_fallback": served_attempt > 1,
            "attempts": [a.as_entry(i) for i, a in enumerate(attempts, start=1)],
            "execution_id": response.execution.external_id if response.execution else None,
            "routing_decision_id": str(decision.pk) if decision else None,
        }

    @staticmethod
    def _execution_request(model, request: GatewayRequest) -> ExecutionRequest:
        return ExecutionRequest(
            model_id=model.model_id,
            messages=request.messages,
            prompt=request.prompt,
            tools=request.tools,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            stream=True,
            trace_id=request.trace_id or None,
            execution_id=request.external_id or None,
            metadata=request.metadata,
        )

    # -- Enforcement --------------------------------------------------------

    @staticmethod
    def _enforce_policy(organization, request: GatewayRequest) -> None:
        """Where a policy verdict stops being advice.

        Everywhere else a `deny` is a recommendation an engine may ignore. The
        gateway is holding the request, so here it is the answer.

        Only the agent-level policies are decided here. Which model serves is
        not known until routing has run, and checking the model the agent
        merely *declares* would refuse requests that routing would have served
        from a permitted model -- see `_model_denial`.
        """
        from apps.policies.services import PolicyContext, PolicyEngine

        context = PolicyContext(
            agent=request.agent,
            application=None,
            model=None,
            provider=None,
            execution=None,
            action="gateway.complete",
            tool="",
            data_classification=getattr(request.agent, "data_classification", "") or "",
            environment=request.environment,
            estimated_cost=Decimal("0"),
        )
        decision = PolicyEngine.check_agent(
            organization, context, requested_action="gateway.complete"
        )

        if not decision.allowed:
            raise PolicyDenied(
                decision.reason or "Blocked by policy.",
                detail={"policies": decision.policy_names, "severity": decision.severity},
            )
        if decision.requires_approval:
            raise ApprovalRequired(
                decision.reason or "This request needs human approval before it can run.",
                detail={"policies": decision.policy_names},
            )

    @staticmethod
    def _enforce_budget(organization, request: GatewayRequest) -> None:
        """Stop at a hard budget rather than merely alerting after the fact."""
        if not setting("ENFORCE_BUDGETS", True):
            return

        from apps.costs.models import Budget, BudgetEnforcement

        budgets = (
            Budget.objects.for_organization(organization)
            .filter(enforcement=BudgetEnforcement.HARD_STOP)
            .select_related()
        )
        for budget in budgets:
            if not budget.applies_to_agent(request.agent):
                continue
            if budget.is_exhausted():
                raise BudgetExceeded(
                    f"Budget '{budget.name}' is exhausted for the current period.",
                    detail={"budget": budget.name, "amount": str(budget.amount)},
                )

    # -- Attempting ---------------------------------------------------------

    @staticmethod
    def _chain(plan, request: GatewayRequest) -> list:
        """The plan's models, honouring an explicit model pin if given.

        A caller that names a model gets it first -- the platform's opinion
        becomes the fallback rather than the override, because an engineer
        pinning a model is usually debugging something specific.
        """
        models = [candidate.model for candidate in plan.candidates]
        if not request.model:
            return models

        pinned = [m for m in models if request.model in (m.model_id, str(m.pk), m.model_name)]
        rest = [m for m in models if m not in pinned]
        return pinned + rest

    @classmethod
    def _attempt_chain(cls, organization, request, candidates, deadline, decision) -> GatewayResponse:
        from apps.routing.models import RouteOutcome
        from apps.routing.services import RoutingService as Routing

        circuits = Breaker.for_providers({model.provider_id for model in candidates})
        attempts: list[Attempt] = []
        last_error = ""

        for position, model in enumerate(candidates, start=1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                attempts.append(Attempt(model, False, 0, skipped="Request deadline reached."))
                break

            circuit = circuits.get(model.provider_id)
            if not Breaker.available(circuit):
                attempts.append(
                    Attempt(model, False, 0, skipped=f"Circuit open for {model.provider.name}.")
                )
                continue

            denial = cls._model_denial(organization, request, model)
            if denial:
                attempts.append(Attempt(model, False, 0, skipped=denial))
                last_error = denial
                continue

            attempt_started = time.monotonic()
            try:
                result = cls._call(model, request, timeout=min(remaining, float(setting("ATTEMPT_TIMEOUT_SECONDS", 30))))
            except Exception as exc:  # adapter errors, upstream errors, timeouts
                latency = int((time.monotonic() - attempt_started) * 1000)
                last_error = f"{type(exc).__name__}: {exc}"[:500]
                attempts.append(Attempt(model, False, latency, error=last_error))
                Breaker.record_failure(model.provider, last_error)
                logger.warning("Gateway attempt %s failed on %s: %s", position, model.model_name, last_error)
                continue

            latency = int((time.monotonic() - attempt_started) * 1000)
            attempts.append(Attempt(model, True, latency))
            Breaker.record_success(model.provider)

            cost = cls._price(organization, model, result)
            Routing.report_outcome(
                decision,
                model=model,
                attempt=len([a for a in attempts if not a.skipped]),
                outcome=RouteOutcome.SERVED,
            )
            return GatewayResponse(
                output_text=result.output_text,
                model=model,
                attempts=attempts,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                cost=cost,
                currency=result.currency or "USD",
                latency_ms=latency,
                decision=decision,
                raw=result.raw or {},
            )

        Routing.report_outcome(
            decision, outcome=RouteOutcome.EXHAUSTED, detail=last_error or "No attempt succeeded."
        )
        cls._raise_exhausted(attempts, last_error, "Every model in the route failed.")

    @staticmethod
    def _raise_exhausted(attempts, last_error: str, message: str):
        """Fail with the status that matches *why* nothing served.

        A chain where every candidate was refused by policy is a refusal, not a
        bad gateway: the caller must not retry it, and a 502 would tell them to.
        """
        entries = [a.as_entry(i) for i, a in enumerate(attempts, start=1)]
        attempted = [a for a in attempts if not a.skipped]

        if attempts and not attempted and all(a.skipped for a in attempts):
            if any("policy" in a.skipped.lower() or "not approved" in a.skipped.lower()
                   or "evaluat" in a.skipped.lower() for a in attempts):
                raise PolicyDenied(
                    "No permitted model is available for this request.",
                    detail={"attempts": entries},
                )

        raise AllAttemptsFailed(
            message, detail={"attempts": entries, "last_error": last_error}
        )

    @staticmethod
    def _model_denial(organization, request: GatewayRequest, model) -> str:
        """Why this candidate may not serve, or an empty string.

        The pre-flight check runs before routing, so it can only see the model
        the *agent declares*. Routing may well choose a different one -- so the
        model-dependent policies are re-checked here, against the model that
        would actually run. Without this, gating on model approval or on
        evaluation evidence would be checking the wrong model.
        """
        from apps.policies.services import PolicyContext, PolicyEngine

        context = PolicyContext(
            agent=request.agent,
            action="gateway.complete",
            data_classification=getattr(request.agent, "data_classification", "") or "",
            environment=request.environment,
            estimated_cost=Decimal("0"),
        )
        decision = PolicyEngine.check_model(
            organization, model, context, requested_action="gateway.complete"
        )
        if decision.allowed and not decision.requires_approval:
            return ""
        return decision.reason or f"{model.model_name} is not permitted by policy."

    @staticmethod
    def _call(model, request: GatewayRequest, *, timeout: float):
        """One provider call, through the adapter registry.

        No provider branching: the adapter is looked up by key, exactly as
        everywhere else in the platform.
        """
        adapter = model.provider.get_adapter(timeout=timeout)
        result = adapter.execute(
            ExecutionRequest(
                model_id=model.model_id,
                messages=request.messages,
                prompt=request.prompt,
                tools=request.tools,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                trace_id=request.trace_id or None,
                execution_id=request.external_id or None,
                metadata=request.metadata,
            )
        )
        if result.status == ExecutionStatus.FAILED:
            raise AdapterError(result.error_message or "Provider reported a failure.")
        return result

    @classmethod
    def _price(cls, organization, model, result) -> Decimal:
        if result.cost:
            return result.cost
        return cls._price_tokens(model, result.usage.input_tokens, result.usage.output_tokens)

    @staticmethod
    def _price_tokens(model, input_tokens: int, output_tokens: int) -> Decimal:
        input_cost = (Decimal(input_tokens) / 1000) * (model.input_cost_per_1k or 0)
        output_cost = (Decimal(output_tokens) / 1000) * (model.output_cost_per_1k or 0)
        return input_cost + output_cost

    # -- Recording ----------------------------------------------------------

    @classmethod
    def _record(cls, organization, request, response, service_account, **facts) -> None:
        """Write the run as an ordinary Execution, best effort.

        Gateway traffic becomes indistinguishable from reported traffic in the
        monitor, the cost dashboard and the audit trail -- and if this write
        fails, the caller still gets the answer it already paid for.
        """
        try:
            with transaction.atomic():
                response.execution = cls._write_execution(
                    organization, request, response, service_account, **facts
                )
        except Exception:
            logger.exception("Gateway could not record execution for %s", request.external_id)

    @staticmethod
    def _write_execution(organization, request, response, service_account, *,
                         streamed=False, estimated_usage=False, error=""):
        from apps.executions.services import ExecutionService

        external_id = request.external_id or f"gw-{uuid.uuid4().hex[:16]}"
        execution = ExecutionService.record(
            organization=organization,
            external_id=external_id,
            service_account=service_account,
            source_adapter="gateway",
            agent=str(request.agent.pk) if request.agent else "",
            model=str(response.model.pk),
            provider=str(response.model.provider_id),
            trace_id=request.trace_id,
            # A stream that broke after the first byte delivered a partial
            # answer. Recording that as "completed" would quietly overstate the
            # platform's success rate and hide a real class of failure.
            status=ExecutionStatus.FAILED if error else ExecutionStatus.COMPLETED,
            error_message=error,
            environment=request.environment,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost=response.cost,
            currency=response.currency,
            latency_ms=response.latency_ms,
            metadata={
                "gateway": True,
                "streamed": streamed,
                "used_fallback": response.used_fallback,
                # Flagged so nobody reads an estimate as a measurement when
                # reconciling the cost dashboard against a provider invoice.
                "usage_estimated": estimated_usage,
                "attempts": [a.as_entry(i) for i, a in enumerate(response.attempts, start=1)],
            },
        )

        # The prompt and the answer ride on the normalized events, under the
        # same keys an external engine would use -- so the monitor's transcript
        # and its personal-data highlighting work on gateway traffic unchanged.
        ExecutionService.append_events(
            execution,
            [
                {
                    "event_type": "PROMPT_CREATED",
                    "sequence": 0,
                    "metadata": {"prompt": request.text},
                },
                {
                    "event_type": "MODEL_REQUEST_COMPLETED",
                    "sequence": 1,
                    "duration_ms": response.latency_ms,
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "model_id": response.model.model_id,
                    "metadata": {"completion": response.output_text},
                },
            ],
        )
        return execution
