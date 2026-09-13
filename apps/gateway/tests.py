"""Tests for the AI Gateway.

The gateway is the one component that sits in the caller's critical path, so
these lean on the ways it can hurt somebody: enforcing when it should not,
failing to enforce when it should, hanging, or turning a successful model call
into a failed one because the platform could not write its own telemetry.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Perm, Role, role_has_permission
from apps.agents.models import AIAgent
from apps.api.models import APIKey, ServiceAccount
from apps.core.constants import Environment, HealthStatus, LifecycleStatus
from apps.costs.models import Budget, BudgetEnforcement, BudgetPeriod, BudgetScope
from apps.executions.models import Execution, ExecutionStatusChoices
from apps.gateway.messaging import MessageBlocked, MessageRequest, MessagingService
from apps.gateway.models import CircuitState, GatewayMessage, ProviderCircuit
from apps.gateway.services import (
    AllAttemptsFailed,
    BudgetExceeded,
    Breaker,
    GatewayRequest,
    GatewayService,
    NoRouteAvailable,
    PolicyDenied,
)
from apps.models_registry.models import AIModel, ModelType
from apps.organizations.models import Organization, OrganizationMembership
from apps.policies.models import Policy, PolicyEffect, PolicyType
from apps.providers.models import AIProvider
from apps.routing.models import RouteOutcome, RoutingDecision, RoutingPolicy
from integrations.base.events import ExecutionStatus
from integrations.base.types import ExecutionResult, StreamChunk, TokenUsage

User = get_user_model()


def result(text="hello", input_tokens=100, output_tokens=20):
    return ExecutionResult(
        execution_id="x",
        status=ExecutionStatus.COMPLETED,
        output_text=text,
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        currency="USD",
    )


class GatewayFixtureMixin:
    def build_estate(self):
        self.organization = Organization.objects.create(name="Gateway Org")

        self.primary_provider = AIProvider.objects.create(
            organization=self.organization, name="Primary", adapter_key="mock",
            status=LifecycleStatus.ACTIVE, health_status=HealthStatus.HEALTHY,
        )
        self.backup_provider = AIProvider.objects.create(
            organization=self.organization, name="Backup", adapter_key="mock",
            status=LifecycleStatus.ACTIVE, health_status=HealthStatus.HEALTHY,
        )

        self.primary_model = AIModel.objects.create(
            organization=self.organization, provider=self.primary_provider,
            model_name="Primary Large", model_id="primary-large",
            model_type=ModelType.CHAT, approved_for_use=True,
            input_cost_per_1k=Decimal("0.001"), output_cost_per_1k=Decimal("0.002"),
            status=LifecycleStatus.ACTIVE, context_window=128000,
        )
        self.backup_model = AIModel.objects.create(
            organization=self.organization, provider=self.backup_provider,
            model_name="Backup Large", model_id="backup-large",
            model_type=ModelType.CHAT, approved_for_use=True,
            input_cost_per_1k=Decimal("0.004"), output_cost_per_1k=Decimal("0.008"),
            status=LifecycleStatus.ACTIVE, context_window=128000,
        )

        self.agent = AIAgent.objects.create(
            organization=self.organization, name="Desk Agent", agent_id="desk-agent",
            provider=self.primary_provider, primary_model=self.primary_model,
            environment=Environment.PRODUCTION, status=LifecycleStatus.ACTIVE,
        )
        RoutingPolicy.objects.create(
            organization=self.organization, name="Chat", model_type=ModelType.CHAT,
            status=LifecycleStatus.ACTIVE, fallback_depth=2,
        )

    def request(self, **kwargs):
        defaults = {"agent": self.agent, "prompt": "Summarise the position."}
        defaults.update(kwargs)
        return GatewayRequest(**defaults)


class GatewaySuccessTests(GatewayFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()

    def test_a_call_is_served_and_recorded_as_an_execution(self):
        with patch("apps.providers.models.registry.get_provider") as get_provider:
            get_provider.return_value.execute.return_value = result()
            response = GatewayService.complete(self.organization, self.request())

        self.assertEqual(response.output_text, "hello")
        self.assertFalse(response.used_fallback)

        execution = Execution.objects.get(pk=response.execution.pk)
        self.assertEqual(execution.source_adapter, "gateway")
        self.assertEqual(execution.total_tokens, 120)
        self.assertEqual(execution.agent, self.agent)

    def test_the_prompt_and_answer_ride_on_normalized_events(self):
        # So the monitor's transcript and its personal-data highlighting work
        # on gateway traffic without knowing the gateway exists.
        with patch("apps.providers.models.registry.get_provider") as get_provider:
            get_provider.return_value.execute.return_value = result(text="the answer")
            response = GatewayService.complete(self.organization, self.request())

        events = {e.event_type: e for e in response.execution.events.all()}
        self.assertEqual(events["PROMPT_CREATED"].metadata["prompt"], "Summarise the position.")
        self.assertEqual(
            events["MODEL_REQUEST_COMPLETED"].metadata["completion"], "the answer"
        )

    def test_cost_is_derived_from_the_registry_when_the_provider_reports_none(self):
        with patch("apps.providers.models.registry.get_provider") as get_provider:
            get_provider.return_value.execute.return_value = result(
                input_tokens=1000, output_tokens=1000
            )
            response = GatewayService.complete(self.organization, self.request())

        # 1000/1000 tokens at 0.001 and 0.002 per 1k.
        self.assertEqual(response.cost, Decimal("0.003"))

    def test_a_routing_decision_is_recorded_and_closed_out(self):
        with patch("apps.providers.models.registry.get_provider") as get_provider:
            get_provider.return_value.execute.return_value = result()
            GatewayService.complete(self.organization, self.request())

        decision = RoutingDecision.objects.for_organization(self.organization).get()
        self.assertEqual(decision.outcome, RouteOutcome.SERVED)
        self.assertEqual(decision.attempt, 1)
        self.assertFalse(decision.used_fallback)


class GatewayFallbackTests(GatewayFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()

    def test_a_failing_primary_falls_through_to_the_backup(self):
        calls = []

        def execute(request):
            calls.append(request.model_id)
            if len(calls) == 1:
                raise RuntimeError("upstream exploded")
            return result(text="from the backup")

        with patch("apps.providers.models.registry.get_provider") as get_provider:
            get_provider.return_value.execute.side_effect = execute
            response = GatewayService.complete(self.organization, self.request())

        self.assertEqual(response.output_text, "from the backup")
        self.assertTrue(response.used_fallback)
        self.assertEqual(len(calls), 2)

    def test_the_fallback_is_recorded_as_the_attempt_that_served(self):
        def execute(request):
            if not getattr(execute, "called", False):
                execute.called = True
                raise RuntimeError("nope")
            return result()

        with patch("apps.providers.models.registry.get_provider") as get_provider:
            get_provider.return_value.execute.side_effect = execute
            GatewayService.complete(self.organization, self.request())

        decision = RoutingDecision.objects.for_organization(self.organization).get()
        self.assertEqual(decision.attempt, 2)
        self.assertTrue(decision.used_fallback)

    def test_every_model_failing_raises_rather_than_returning_empty(self):
        with patch("apps.providers.models.registry.get_provider") as get_provider:
            get_provider.return_value.execute.side_effect = RuntimeError("all down")
            with self.assertRaises(AllAttemptsFailed):
                GatewayService.complete(self.organization, self.request())

        decision = RoutingDecision.objects.for_organization(self.organization).get()
        self.assertEqual(decision.outcome, RouteOutcome.EXHAUSTED)

    def test_no_eligible_model_is_a_distinct_failure(self):
        AIModel.objects.for_organization(self.organization).update(
            status=LifecycleStatus.RETIRED
        )
        with self.assertRaises(NoRouteAvailable):
            GatewayService.complete(self.organization, self.request())


@override_settings(
    CONTROL_TOWER={
        "DEFAULT_CURRENCY": "USD",
        "RISK_THRESHOLDS": {"LOW": 4, "MODERATE": 9, "HIGH": 16},
        "AGENT_AUTONOMY_APPROVAL_THRESHOLD": 3,
        "GATEWAY": {
            "ATTEMPT_TIMEOUT_SECONDS": 5,
            "REQUEST_DEADLINE_SECONDS": 60,
            "CIRCUIT_FAILURE_THRESHOLD": 2,
            "CIRCUIT_COOL_OFF_SECONDS": 30,
            "ENFORCE_BUDGETS": True,
        },
        "PROMPT_DISPLAY": {"MAX_CHARS": 1200, "REDACT_CLASSIFICATIONS": []},
    }
)
class CircuitBreakerTests(GatewayFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()

    def test_repeated_failures_open_the_breaker(self):
        for _ in range(2):
            Breaker.record_failure(self.primary_provider, "timeout")

        circuit = ProviderCircuit.objects.get(provider=self.primary_provider)
        self.assertEqual(circuit.state, CircuitState.OPEN)
        self.assertEqual(circuit.trips, 1)

    def test_an_open_breaker_takes_the_provider_out_of_the_chain(self):
        Breaker.record_failure(self.primary_provider, "boom")
        Breaker.record_failure(self.primary_provider, "boom")

        with patch("apps.providers.models.registry.get_provider") as get_provider:
            get_provider.return_value.execute.return_value = result(text="backup served")
            response = GatewayService.complete(self.organization, self.request())

        self.assertEqual(response.model, self.backup_model)
        skipped = [a for a in response.attempts if a.skipped]
        self.assertTrue(any("Circuit open" in a.skipped for a in skipped))

    def test_success_closes_a_breaker_that_was_counting_failures(self):
        Breaker.record_failure(self.primary_provider, "one")
        Breaker.record_success(self.primary_provider)

        circuit = ProviderCircuit.objects.get(provider=self.primary_provider)
        self.assertEqual(circuit.state, CircuitState.CLOSED)
        self.assertEqual(circuit.consecutive_failures, 0)

    def test_the_breaker_lets_a_probe_through_after_the_cool_off(self):
        Breaker.record_failure(self.primary_provider, "boom")
        Breaker.record_failure(self.primary_provider, "boom")

        circuit = ProviderCircuit.objects.get(provider=self.primary_provider)
        circuit.opened_at = timezone.now() - timezone.timedelta(seconds=120)
        circuit.save(update_fields=["opened_at"])

        self.assertTrue(Breaker.available(circuit))


class GatewayEnforcementTests(GatewayFixtureMixin, TestCase):
    """Where advisory verdicts become real refusals."""

    def setUp(self):
        self.build_estate()

    def test_a_deny_policy_stops_the_request(self):
        with patch("apps.policies.services.PolicyEngine.check_agent") as check:
            check.return_value.allowed = False
            check.return_value.requires_approval = False
            check.return_value.reason = "Blocked by policy."
            check.return_value.policy_names = ["No production chat"]
            check.return_value.severity = "high"

            with self.assertRaises(PolicyDenied):
                GatewayService.complete(self.organization, self.request())

    def test_a_denied_request_never_reaches_a_provider(self):
        with patch("apps.policies.services.PolicyEngine.check_agent") as check, \
             patch("apps.providers.models.registry.get_provider") as get_provider:
            check.return_value.allowed = False
            check.return_value.requires_approval = False
            check.return_value.reason = "no"
            check.return_value.policy_names = []
            check.return_value.severity = "high"

            with self.assertRaises(PolicyDenied):
                GatewayService.complete(self.organization, self.request())

            get_provider.assert_not_called()

    def test_an_exhausted_hard_stop_budget_refuses_the_request(self):
        Budget.objects.create(
            organization=self.organization, name="Tiny", scope=BudgetScope.ORGANIZATION,
            amount=Decimal("0.01"), period=BudgetPeriod.MONTHLY,
            enforcement=BudgetEnforcement.HARD_STOP, status=LifecycleStatus.ACTIVE,
        )
        Execution.objects.create(
            organization=self.organization, external_id="spend-1", agent=self.agent,
            cost=Decimal("5.00"), started_at=timezone.now(),
        )

        with self.assertRaises(BudgetExceeded):
            GatewayService.complete(self.organization, self.request())

    def test_an_advisory_budget_does_not_refuse(self):
        """Existing budgets must not start blocking traffic on upgrade."""
        Budget.objects.create(
            organization=self.organization, name="Advisory", scope=BudgetScope.ORGANIZATION,
            amount=Decimal("0.01"), period=BudgetPeriod.MONTHLY, status=LifecycleStatus.ACTIVE,
        )
        Execution.objects.create(
            organization=self.organization, external_id="spend-2", agent=self.agent,
            cost=Decimal("5.00"), started_at=timezone.now(),
        )

        with patch("apps.providers.models.registry.get_provider") as get_provider:
            get_provider.return_value.execute.return_value = result()
            response = GatewayService.complete(self.organization, self.request())
        self.assertEqual(response.output_text, "hello")


class ModelGateTests(GatewayFixtureMixin, TestCase):
    """The model that actually runs has to clear model-dependent policy.

    The pre-flight check runs before routing, so it can only see the model the
    agent declares. These cover the gap that leaves.
    """

    def setUp(self):
        self.build_estate()
        # Routing has its own approval filter. Turn it off so a denied model
        # still reaches the chain and the *policy* layer is what stops it --
        # otherwise this suite would only be re-testing routing.
        RoutingPolicy.objects.filter(organization=self.organization).update(
            require_approved_models=False
        )

    def _deny_model(self, model):
        """Deny exactly one model, leaving the rest permitted."""
        Policy.objects.create(
            organization=self.organization,
            name=f"Block {model.model_name}",
            policy_type=PolicyType.MODEL_APPROVAL,
            effect=PolicyEffect.DENY,
            status=LifecycleStatus.ACTIVE,
        )
        model.approved_for_use = False
        model.save(update_fields=["approved_for_use"])

    def test_a_denied_primary_falls_through_to_a_permitted_fallback(self):
        self._deny_model(self.primary_model)

        with patch("apps.providers.models.registry.get_provider") as get_provider:
            get_provider.return_value.execute.return_value = result(text="from the backup")
            response = GatewayService.complete(self.organization, self.request())

        self.assertEqual(response.model, self.backup_model)
        self.assertTrue(any("not approved" in a.skipped for a in response.attempts if a.skipped))

    def test_a_denied_model_is_never_called(self):
        self._deny_model(self.primary_model)
        called = []

        def get_provider(key, config):
            from unittest.mock import MagicMock

            adapter = MagicMock()
            adapter.execute.side_effect = lambda r: (called.append(r.model_id), result())[1]
            return adapter

        with patch("apps.providers.models.registry.get_provider", side_effect=get_provider):
            GatewayService.complete(self.organization, self.request())

        self.assertEqual(called, ["backup-large"])

    def test_a_chain_where_every_model_is_denied_is_a_refusal_not_a_bad_gateway(self):
        # A 502 would tell the caller to retry, and retrying will never work.
        self._deny_model(self.primary_model)
        self._deny_model(self.backup_model)

        with patch("apps.providers.models.registry.get_provider") as get_provider:
            with self.assertRaises(PolicyDenied):
                GatewayService.complete(self.organization, self.request())
            get_provider.return_value.execute.assert_not_called()

    def test_an_evaluation_gate_blocks_the_model_that_would_serve(self):
        """The bug this exists to prevent: gating the wrong model."""
        from apps.evaluations.models import EvaluationRun, EvaluationSuite, RunStatus

        Policy.objects.create(
            organization=self.organization, name="Must pass evaluation",
            policy_type=PolicyType.MODEL_APPROVAL, effect=PolicyEffect.DENY,
            status=LifecycleStatus.ACTIVE,
            configuration={"require_evaluation": True},
        )
        suite = EvaluationSuite.objects.create(
            organization=self.organization, name="Baseline", status=LifecycleStatus.ACTIVE
        )
        # Only the backup has evidence, so only the backup may serve -- even
        # though routing would otherwise prefer the cheaper primary.
        EvaluationRun.objects.create(
            organization=self.organization, suite=suite, model=self.backup_model,
            status=RunStatus.COMPLETED, passed=True, score=Decimal("95.00"),
            total_cases=5, completed_cases=5, passed_cases=5, finished_at=timezone.now(),
        )

        with patch("apps.providers.models.registry.get_provider") as get_provider:
            get_provider.return_value.execute.return_value = result()
            response = GatewayService.complete(self.organization, self.request())

        self.assertEqual(response.model, self.backup_model)
        self.assertTrue(
            any("never been evaluated" in a.skipped for a in response.attempts if a.skipped)
        )

    def test_the_streaming_path_applies_the_same_gate(self):
        self._deny_model(self.primary_model)

        from unittest.mock import MagicMock

        adapter = MagicMock()
        adapter.supports.return_value = True
        adapter.stream.side_effect = lambda r: chunks("backup text")

        with patch("apps.providers.models.registry.get_provider", return_value=adapter):
            events = dict(GatewayService.stream(self.organization, self.request()))

        self.assertEqual(events["start"]["model"], "backup-large")


class GatewayResilienceTests(GatewayFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()

    def test_a_telemetry_failure_does_not_fail_the_caller(self):
        """The caller already paid for this answer; give it to them."""
        with patch("apps.providers.models.registry.get_provider") as get_provider, \
             patch("apps.gateway.services.GatewayService._write_execution") as write:
            get_provider.return_value.execute.return_value = result(text="still yours")
            write.side_effect = RuntimeError("database on fire")

            response = GatewayService.complete(self.organization, self.request())

        self.assertEqual(response.output_text, "still yours")
        self.assertIsNone(response.execution)

    def test_each_attempt_is_given_a_timeout(self):
        seen = {}

        def get_provider(key, config):
            seen["timeout"] = config.timeout
            from unittest.mock import MagicMock

            adapter = MagicMock()
            adapter.execute.return_value = result()
            return adapter

        with patch("apps.providers.models.registry.get_provider", side_effect=get_provider):
            GatewayService.complete(self.organization, self.request())

        self.assertTrue(0 < seen["timeout"] <= 30)


def chunks(*texts, usage=None, final=True):
    """A provider stream: text chunks, then a final chunk carrying usage."""
    for text in texts:
        yield StreamChunk(text=text)
    if final:
        yield StreamChunk(text="", is_final=True, usage=usage or TokenUsage(50, 10))


class StreamingTests(GatewayFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()

    def collect(self, **kwargs):
        return list(GatewayService.stream(self.organization, self.request(**kwargs)))

    def _adapter(self, stream_impl, *, streams=True):
        adapter = MagicMock()
        adapter.supports.return_value = streams
        adapter.stream.side_effect = stream_impl
        return adapter

    def test_deltas_arrive_in_order_and_are_bounded_by_start_and_done(self):
        with patch("apps.providers.models.registry.get_provider") as get_provider:
            get_provider.return_value = self._adapter(
                lambda request: chunks("Hello ", "there", "!")
            )
            events = self.collect()

        self.assertEqual(events[0][0], "start")
        self.assertEqual([p["text"] for name, p in events if name == "delta"],
                         ["Hello ", "there", "!"])
        self.assertEqual(events[-1][0], "done")

    def test_reported_usage_is_used_and_priced(self):
        with patch("apps.providers.models.registry.get_provider") as get_provider:
            get_provider.return_value = self._adapter(
                lambda request: chunks("hi", usage=TokenUsage(1000, 1000))
            )
            done = dict(self.collect())["done"]

        self.assertFalse(done["usage"]["estimated"])
        self.assertEqual(done["usage"]["total_tokens"], 2000)
        self.assertEqual(Decimal(done["cost"]), Decimal("0.003"))

    def test_usage_is_estimated_and_flagged_when_the_provider_reports_none(self):
        # A streamed call that costs nothing in the dashboard is worse than one
        # that costs an approximation, as long as nobody mistakes the two.
        with patch("apps.providers.models.registry.get_provider") as get_provider:
            get_provider.return_value = self._adapter(
                lambda request: chunks("some text here", usage=TokenUsage(0, 0))
            )
            done = dict(self.collect())["done"]

        self.assertTrue(done["usage"]["estimated"])
        self.assertGreater(done["usage"]["total_tokens"], 0)

    def test_a_failure_before_the_first_byte_falls_back(self):
        def stream(request):
            if request.model_id == "primary-large":
                raise RuntimeError("cold start failed")
            return chunks("from the backup")

        with patch("apps.providers.models.registry.get_provider") as get_provider:
            get_provider.return_value = self._adapter(stream)
            events = self.collect()

        start = dict(events)["start"]
        self.assertTrue(start["used_fallback"])
        self.assertEqual(start["model"], "backup-large")

    def test_a_stream_that_opens_then_dies_before_content_still_falls_back(self):
        """Accepting a connection is not proof of working."""
        def stream(request):
            def dies():
                raise RuntimeError("died on first token")
                yield  # pragma: no cover - generator marker
            return dies() if request.model_id == "primary-large" else chunks("backup text")

        with patch("apps.providers.models.registry.get_provider") as get_provider:
            get_provider.return_value = self._adapter(stream)
            events = dict(self.collect())

        self.assertEqual(events["start"]["model"], "backup-large")

    def test_a_failure_after_the_first_byte_never_falls_back(self):
        """The client holds part of model A's answer; splicing in B would lie."""
        def stream(request):
            def partial():
                yield StreamChunk(text="The answer is ")
                raise RuntimeError("connection reset")
            return partial()

        with patch("apps.providers.models.registry.get_provider") as get_provider:
            get_provider.return_value = self._adapter(stream)
            events = self.collect()

        names = [name for name, _ in events]
        self.assertEqual(names.count("start"), 1)
        self.assertIn("error", names)
        payload = dict(events)["error"]
        self.assertFalse(payload["recoverable"])
        # The partial text the caller already received is not retracted.
        self.assertEqual([p["text"] for n, p in events if n == "delta"], ["The answer is "])

    def test_an_interrupted_stream_is_recorded_as_failed(self):
        def stream(request):
            def partial():
                yield StreamChunk(text="half an ")
                raise RuntimeError("reset")
            return partial()

        with patch("apps.providers.models.registry.get_provider") as get_provider:
            get_provider.return_value = self._adapter(stream)
            self.collect()

        execution = Execution.objects.get(source_adapter="gateway")
        self.assertEqual(execution.status, ExecutionStatusChoices.FAILED)
        self.assertIn("reset", execution.error_message)

    def test_a_provider_that_cannot_stream_is_skipped_without_blame(self):
        # Not being able to stream is not a fault, so it must not trip the
        # breaker and take the provider out of non-streaming traffic too.
        def get_provider(key, config):
            adapter = MagicMock()
            adapter.supports.return_value = False
            return adapter

        with patch("apps.providers.models.registry.get_provider", side_effect=get_provider):
            with self.assertRaises(AllAttemptsFailed):
                self.collect()

        self.assertFalse(ProviderCircuit.objects.exists())

    def test_policy_is_enforced_before_any_event_is_produced(self):
        with patch("apps.policies.services.PolicyEngine.check_agent") as check, \
             patch("apps.providers.models.registry.get_provider") as get_provider:
            check.return_value.allowed = False
            check.return_value.requires_approval = False
            check.return_value.reason = "no"
            check.return_value.policy_names = []
            check.return_value.severity = "high"

            with self.assertRaises(PolicyDenied):
                self.collect()
            get_provider.assert_not_called()

    def test_the_streamed_transcript_is_recorded_for_the_monitor(self):
        with patch("apps.providers.models.registry.get_provider") as get_provider:
            get_provider.return_value = self._adapter(lambda r: chunks("one ", "two"))
            self.collect()

        execution = Execution.objects.get(source_adapter="gateway")
        events = {e.event_type: e for e in execution.events.all()}
        self.assertEqual(events["MODEL_REQUEST_COMPLETED"].metadata["completion"], "one two")
        self.assertTrue(execution.metadata["streamed"])

    def test_a_served_stream_closes_out_the_routing_decision(self):
        with patch("apps.providers.models.registry.get_provider") as get_provider:
            get_provider.return_value = self._adapter(lambda r: chunks("ok"))
            self.collect()

        decision = RoutingDecision.objects.for_organization(self.organization).get()
        self.assertEqual(decision.outcome, RouteOutcome.SERVED)
        self.assertEqual(decision.attempt, 1)


class StreamingEndpointTests(GatewayFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.url = reverse("api:gateway-chat")
        account = ServiceAccount.objects.create(
            organization=self.organization, name="streamer", role=Role.AI_DEVELOPER
        )
        _, self.key = APIKey.generate(account)

    def post(self, **body):
        payload = {"agent": "desk-agent", "prompt": "hello", "stream": True}
        payload.update(body)
        return self.client.post(
            self.url, payload, content_type="application/json", HTTP_X_API_KEY=self.key
        )

    def test_a_stream_is_served_as_server_sent_events(self):
        adapter = MagicMock()
        adapter.supports.return_value = True
        adapter.stream.side_effect = lambda r: chunks("Hi ", "there")

        with patch("apps.providers.models.registry.get_provider", return_value=adapter):
            response = self.post()
            body = b"".join(response.streaming_content).decode()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/event-stream")
        self.assertIn("event: start", body)
        self.assertIn('"text": "Hi "', body)
        self.assertIn("event: done", body)

    def test_a_refusal_stays_a_real_status_code_not_a_200_stream(self):
        """A client must not have to parse a success to learn it was denied."""
        with patch("apps.policies.services.PolicyEngine.check_agent") as check:
            check.return_value.allowed = False
            check.return_value.requires_approval = False
            check.return_value.reason = "Blocked by policy."
            check.return_value.policy_names = ["Test"]
            check.return_value.severity = "high"

            response = self.post()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "policy_denied")

    def test_no_eligible_model_is_a_503_before_the_stream_opens(self):
        AIModel.objects.for_organization(self.organization).update(
            status=LifecycleStatus.RETIRED
        )
        response = self.post()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "no_route")

    def test_streaming_requires_the_same_permission_as_a_plain_call(self):
        auditor = ServiceAccount.objects.create(
            organization=self.organization, name="auditor-stream", role=Role.AUDITOR
        )
        _, key = APIKey.generate(auditor)
        response = self.client.post(
            self.url,
            {"agent": "desk-agent", "prompt": "hello", "stream": True},
            content_type="application/json",
            HTTP_X_API_KEY=key,
        )
        self.assertEqual(response.status_code, 403)

    def test_a_non_streaming_call_is_unaffected(self):
        adapter = MagicMock()
        adapter.execute.return_value = result()

        with patch("apps.providers.models.registry.get_provider", return_value=adapter):
            response = self.post(stream=False)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")


class GatewayPermissionTests(GatewayFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.url = reverse("api:gateway-chat")

    def _key_for(self, role):
        account = ServiceAccount.objects.create(
            organization=self.organization, name=f"engine-{role}", role=role
        )
        _, raw = APIKey.generate(account)
        return raw

    def test_a_developer_service_account_may_invoke(self):
        with patch("apps.providers.models.registry.get_provider") as get_provider:
            get_provider.return_value.execute.return_value = result()
            response = self.client.post(
                self.url,
                {"agent": "desk-agent", "prompt": "hello"},
                content_type="application/json",
                HTTP_X_API_KEY=self._key_for(Role.AI_DEVELOPER),
            )
        self.assertEqual(response.status_code, 200)

    def test_an_auditor_may_not_spend_money_through_the_gateway(self):
        response = self.client.post(
            self.url,
            {"agent": "desk-agent", "prompt": "hello"},
            content_type="application/json",
            HTTP_X_API_KEY=self._key_for(Role.AUDITOR),
        )
        self.assertEqual(response.status_code, 403)

    def test_gateway_access_is_not_implied_by_read_access(self):
        for role in (Role.VIEWER, Role.AUDITOR, Role.APPROVER, Role.RISK_MANAGER):
            with self.subTest(role=role):
                self.assertFalse(role_has_permission(role, Perm.GATEWAY_INVOKE))

    def test_an_unauthenticated_call_is_challenged(self):
        response = self.client.post(
            self.url, {"prompt": "hello"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 401)


class MessagingTests(GatewayFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.other_agent = AIAgent.objects.create(
            organization=self.organization, name="Pricing Agent", agent_id="pricing-agent",
            provider=self.primary_provider, primary_model=self.primary_model,
            status=LifecycleStatus.ACTIVE,
        )

    def _send(self, **kwargs):
        defaults = {
            "sender": self.agent,
            "recipient": self.other_agent,
            "body": "Need a rate for SUB-40182.",
        }
        defaults.update(kwargs)
        return MessagingService.send(self.organization, MessageRequest(**defaults))

    def test_a_message_is_queued_for_the_recipient(self):
        message = self._send()
        self.assertEqual(message.status, GatewayMessage.Status.QUEUED)
        self.assertEqual(message.recipient, self.other_agent)

    def test_collecting_marks_messages_read_and_returns_them_once(self):
        self._send()
        first = MessagingService.collect(self.organization, self.other_agent)
        second = MessagingService.collect(self.organization, self.other_agent)

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_a_blocked_message_is_kept_with_its_reason(self):
        with patch("apps.policies.services.PolicyEngine.check_agent") as check:
            check.return_value.allowed = False
            check.return_value.requires_approval = False
            check.return_value.reason = "Agents may not discuss pricing."
            check.return_value.policy_names = ["Pricing firewall"]

            with self.assertRaises(MessageBlocked):
                self._send()

        message = GatewayMessage.objects.get()
        self.assertEqual(message.status, GatewayMessage.Status.BLOCKED)
        self.assertIn("pricing", message.blocked_reason.lower())

    def test_a_blocked_message_is_never_delivered(self):
        with patch("apps.policies.services.PolicyEngine.check_agent") as check:
            check.return_value.allowed = False
            check.return_value.requires_approval = False
            check.return_value.reason = "no"
            check.return_value.policy_names = []
            with self.assertRaises(MessageBlocked):
                self._send()

        self.assertEqual(MessagingService.collect(self.organization, self.other_agent), [])

    def test_messaging_never_crosses_organizations(self):
        other_org = Organization.objects.create(name="Elsewhere")
        foreign = AIAgent.objects.create(
            organization=other_org, name="Foreign", agent_id="foreign-agent",
            status=LifecycleStatus.ACTIVE,
        )
        with self.assertRaises(MessageBlocked):
            self._send(recipient=foreign)
