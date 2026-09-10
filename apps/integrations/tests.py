"""Tests for the vendor-neutral adapter layer."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from django.test import SimpleTestCase

from integrations.agents.mock import MockAgentAdapter
from integrations.base.agent import BaseAgentAdapter
from integrations.base.capabilities import Capability
from integrations.base.config import AdapterConfig
from integrations.base.events import EventType, ExecutionStatus
from integrations.base.exceptions import (
    AdapterNotFound,
    CapabilityNotSupported,
    CredentialsUnavailable,
    ExecutionNotFound,
)
from integrations.base.provider import BaseAIProviderAdapter
from integrations.base.types import ExecutionRequest, ModelInfo, TokenUsage
from integrations.providers.generic_rest import GenericRESTProviderAdapter, dig
from integrations.providers.mock import MockProviderAdapter
from integrations.registry import AdapterKind, AdapterRegistry, registry
from integrations.webhooks.generic import GenericWebhookExecutionAdapter


class AdapterRegistryTests(SimpleTestCase):
    def setUp(self):
        self.registry = AdapterRegistry()

    def test_registering_and_resolving_a_provider(self):
        self.registry.register_provider("mock", MockProviderAdapter)
        adapter = self.registry.get_provider("mock")
        self.assertIsInstance(adapter, MockProviderAdapter)

    def test_an_unknown_key_raises_a_clear_error(self):
        with self.assertRaises(AdapterNotFound) as ctx:
            self.registry.get_provider("does-not-exist")
        self.assertIn("does-not-exist", str(ctx.exception))

    def test_a_wrong_base_class_is_refused(self):
        class NotAProvider(BaseAgentAdapter):
            adapter_key = "nope"

            def create_execution(self, request):
                raise NotImplementedError

            def get_execution(self, execution_id):
                raise NotImplementedError

            def get_events(self, execution_id):
                raise NotImplementedError

        with self.assertRaises(TypeError):
            self.registry.register_provider("nope", NotAProvider)

    def test_registering_a_different_class_on_the_same_key_needs_replace(self):
        class OtherMock(MockProviderAdapter):
            pass

        self.registry.register_provider("mock", MockProviderAdapter)
        with self.assertRaises(ValueError):
            self.registry.register_provider("mock", OtherMock)

        self.registry.register_provider("mock", OtherMock, replace=True)
        self.assertIsInstance(self.registry.get_provider("mock"), OtherMock)

    def test_config_is_passed_through_to_the_instance(self):
        self.registry.register_provider("mock", MockProviderAdapter)
        config = AdapterConfig(key="mock", endpoint="https://example.test")
        self.assertEqual(self.registry.get_provider("mock", config).config.endpoint, "https://example.test")

    def test_describe_reports_capabilities(self):
        self.registry.register_provider("mock", MockProviderAdapter)
        described = self.registry.describe(AdapterKind.PROVIDER)[0]
        self.assertEqual(described["key"], "mock")
        self.assertIn("supports_chat", described["capabilities"])


class BootstrappedRegistryTests(SimpleTestCase):
    """The adapters loaded by the app's ready() hook."""

    def test_the_expected_adapters_are_registered_at_startup(self):
        self.assertIn("mock", registry.keys(AdapterKind.PROVIDER))
        self.assertIn("openai", registry.keys(AdapterKind.PROVIDER))
        self.assertIn("generic_rest", registry.keys(AdapterKind.PROVIDER))
        self.assertIn("mock", registry.keys(AdapterKind.AGENT))
        self.assertIn("generic_webhook", registry.keys(AdapterKind.EXECUTION))

    def test_the_openai_adapter_imports_without_the_vendor_sdk(self):
        # Registration must not require the `openai` package to be installed.
        adapter = registry.get_provider("openai")
        self.assertTrue(adapter.supports(Capability.SUPPORTS_CHAT))


class CapabilityEnforcementTests(SimpleTestCase):
    def test_an_unsupported_operation_is_refused_before_dispatch(self):
        class NoStreaming(BaseAIProviderAdapter):
            adapter_key = "no-stream"
            capabilities = frozenset({Capability.SUPPORTS_CHAT})

            def list_models(self):
                return []

            def health_check(self):
                raise NotImplementedError

            def execute(self, request):
                raise NotImplementedError

        adapter = NoStreaming()
        self.assertFalse(adapter.supports(Capability.SUPPORTS_STREAMING))
        with self.assertRaises(CapabilityNotSupported):
            list(adapter.stream(ExecutionRequest(prompt="hi")))

    def test_capability_list_is_sorted_strings(self):
        capabilities = MockProviderAdapter().capability_list()
        self.assertEqual(capabilities, sorted(capabilities))
        self.assertTrue(all(isinstance(c, str) for c in capabilities))


class CredentialResolutionTests(SimpleTestCase):
    def test_a_missing_credential_reference_raises(self):
        config = AdapterConfig(key="x", credential_ref="")
        with self.assertRaises(CredentialsUnavailable):
            config.resolve_credential()

    def test_an_unset_environment_variable_raises(self):
        config = AdapterConfig(key="x", credential_ref="DEFINITELY_NOT_SET_12345")
        with self.assertRaises(CredentialsUnavailable):
            config.resolve_credential()
        self.assertFalse(config.has_credential())

    def test_a_resolvable_credential_is_returned(self):
        import os

        os.environ["CT_TEST_SECRET"] = "s3cret"
        try:
            config = AdapterConfig(key="x", credential_ref="CT_TEST_SECRET")
            self.assertEqual(config.resolve_credential(), "s3cret")
            self.assertTrue(config.has_credential())
        finally:
            del os.environ["CT_TEST_SECRET"]

    def test_optional_resolution_returns_none_instead_of_raising(self):
        config = AdapterConfig(key="x", credential_ref="ALSO_NOT_SET_98765")
        self.assertIsNone(config.resolve_credential(required=False))


class MockProviderAdapterTests(SimpleTestCase):
    def setUp(self):
        self.adapter = MockProviderAdapter()

    def test_it_lists_models_with_pricing(self):
        models = self.adapter.list_models()
        self.assertTrue(models)
        self.assertIsNotNone(self.adapter.get_model("mock-large").input_cost_per_1k)

    def test_health_check_is_always_healthy(self):
        self.assertTrue(self.adapter.health_check().healthy)

    def test_execution_is_deterministic_for_the_same_input(self):
        request = ExecutionRequest(model_id="mock-large", prompt="explain the control tower")
        first = self.adapter.execute(request)
        second = self.adapter.execute(request)
        self.assertEqual(first.usage.output_tokens, second.usage.output_tokens)

    def test_execution_reports_usage_and_cost(self):
        result = self.adapter.execute(ExecutionRequest(model_id="mock-large", prompt="hello world"))
        self.assertEqual(result.status, ExecutionStatus.COMPLETED)
        self.assertGreater(result.usage.total_tokens, 0)
        self.assertGreater(result.cost, Decimal("0"))

    def test_streaming_ends_with_a_final_chunk(self):
        chunks = list(self.adapter.stream(ExecutionRequest(model_id="mock-small", prompt="hi")))
        self.assertTrue(chunks[-1].is_final)

    def test_cost_is_computed_from_registered_rates(self):
        model = ModelInfo(
            model_id="m",
            name="m",
            input_cost_per_1k=Decimal("0.01"),
            output_cost_per_1k=Decimal("0.03"),
        )
        estimate = self.adapter.calculate_cost(TokenUsage(1000, 1000), model)
        self.assertEqual(estimate.amount, Decimal("0.04"))

    def test_cost_for_an_unknown_model_is_zero_rather_than_an_error(self):
        estimate = self.adapter.calculate_cost(TokenUsage(100, 100), "not-a-model")
        self.assertEqual(estimate.amount, Decimal("0"))
        self.assertEqual(estimate.pricing_source, "unknown-model")

    def test_usage_records_cover_the_requested_window(self):
        end = datetime.now(timezone.utc)
        records = self.adapter.get_usage(end - timedelta(days=3), end)
        self.assertTrue(records)
        self.assertTrue(all(r.cost >= 0 for r in records))


class MockAgentAdapterTests(SimpleTestCase):
    def setUp(self):
        self.adapter = MockAgentAdapter()

    def test_a_run_produces_a_normalized_timeline(self):
        handle = self.adapter.create_execution(
            ExecutionRequest(execution_id="exec-1", model_id="mock-large")
        )
        events = self.adapter.get_events(handle.execution_id)

        types = [e.event_type for e in events]
        self.assertEqual(types[0], EventType.EXECUTION_STARTED)
        self.assertIn(EventType.AGENT_STARTED, types)
        self.assertIn(EventType.MODEL_REQUEST_COMPLETED, types)

    def test_events_carry_the_trace_id_and_are_ordered(self):
        handle = self.adapter.create_execution(ExecutionRequest(execution_id="exec-2", trace_id="t-9"))
        events = self.adapter.get_events(handle.execution_id)
        self.assertTrue(all(e.trace_id == "t-9" for e in events))
        self.assertEqual([e.sequence for e in events], sorted(e.sequence for e in events))

    def test_an_approval_pause_is_reported_and_resumable(self):
        request = ExecutionRequest(execution_id="exec-3", metadata={"requires_approval": True})
        handle = self.adapter.create_execution(request)
        self.assertEqual(handle.status, ExecutionStatus.WAITING_FOR_APPROVAL)

        resumed = self.adapter.resume_execution("exec-3", decision="approved")
        self.assertEqual(resumed.status, ExecutionStatus.COMPLETED)
        self.assertIn(EventType.APPROVAL_GRANTED, [e.event_type for e in self.adapter.get_events("exec-3")])

    def test_cancelling_a_run(self):
        self.adapter.create_execution(ExecutionRequest(execution_id="exec-4"))
        self.assertEqual(
            self.adapter.cancel_execution("exec-4", reason="operator").status,
            ExecutionStatus.CANCELLED,
        )

    def test_usage_aggregates_across_steps(self):
        self.adapter.create_execution(ExecutionRequest(execution_id="exec-5"))
        usage = self.adapter.get_usage("exec-5")
        self.assertGreater(usage.total_tokens, 0)
        self.assertGreater(self.adapter.get_cost("exec-5"), Decimal("0"))

    def test_an_unknown_execution_raises(self):
        with self.assertRaises(ExecutionNotFound):
            self.adapter.get_execution("never-created")

    def test_events_serialize_to_json_safe_dicts(self):
        import json

        self.adapter.create_execution(ExecutionRequest(execution_id="exec-6"))
        events = self.adapter.get_events("exec-6")
        json.dumps([e.to_dict() for e in events])


class WebhookNormalizationTests(SimpleTestCase):
    def setUp(self):
        self.adapter = GenericWebhookExecutionAdapter()

    def test_a_default_shaped_payload_is_normalized(self):
        payload = {
            "execution_id": "run-77",
            "status": "succeeded",
            "model": "gpt-4o",
            "trace_id": "trace-1",
            "latency_ms": 1830,
            "usage": {"input_tokens": 1200, "output_tokens": 340, "cost": "0.0182"},
        }
        normalized = self.adapter.normalize_execution(payload)

        self.assertEqual(normalized["external_id"], "run-77")
        self.assertEqual(normalized["status"], ExecutionStatus.COMPLETED)
        self.assertEqual(normalized["input_tokens"], 1200)
        self.assertEqual(normalized["cost"], Decimal("0.0182"))

    def test_a_foreign_payload_shape_is_handled_by_configuration_alone(self):
        adapter = GenericWebhookExecutionAdapter(
            AdapterConfig(
                key="generic_webhook",
                options={
                    "field_mapping": {
                        "external_id": "run.id",
                        "status": "run.state",
                        "input_tokens": "run.tokens.prompt",
                        "output_tokens": "run.tokens.completion",
                    }
                },
            )
        )
        normalized = adapter.normalize_execution(
            {"run": {"id": "abc", "state": "in_progress", "tokens": {"prompt": 10, "completion": 5}}}
        )
        self.assertEqual(normalized["external_id"], "abc")
        self.assertEqual(normalized["status"], ExecutionStatus.RUNNING)
        self.assertEqual(normalized["output_tokens"], 5)

    def test_events_are_translated_and_unknown_types_dropped(self):
        events = self.adapter.normalize_events(
            {
                "execution_id": "run-1",
                "events": [
                    {"type": "EXECUTION_STARTED", "timestamp": "2026-09-09T10:00:00Z"},
                    {"type": "SOMETHING_PROPRIETARY"},
                    {"type": "tool_call_completed", "duration_ms": 42},
                ],
            }
        )
        self.assertEqual(
            [e.event_type for e in events],
            [EventType.EXECUTION_STARTED, EventType.TOOL_CALL_COMPLETED],
        )

    def test_an_event_type_map_bridges_a_foreign_vocabulary(self):
        adapter = GenericWebhookExecutionAdapter(
            AdapterConfig(key="w", options={"event_type_map": {"node_finished": "AGENT_STEP_COMPLETED"}})
        )
        events = adapter.normalize_events(
            {"execution_id": "r", "events": [{"type": "node_finished"}]}
        )
        self.assertEqual(events[0].event_type, EventType.AGENT_STEP_COMPLETED)

    def test_signature_verification_passes_when_no_secret_is_configured(self):
        self.assertTrue(self.adapter.verify_signature({}, b"{}"))

    def test_signature_verification_rejects_a_bad_hmac(self):
        import hashlib
        import hmac
        import os

        os.environ["CT_WEBHOOK_SECRET"] = "shhh"
        try:
            adapter = GenericWebhookExecutionAdapter(
                AdapterConfig(key="w", credential_ref="CT_WEBHOOK_SECRET")
            )
            body = b'{"execution_id":"1"}'
            good = hmac.new(b"shhh", body, hashlib.sha256).hexdigest()

            self.assertTrue(adapter.verify_signature({"X-Signature": good}, body))
            self.assertFalse(adapter.verify_signature({"X-Signature": "deadbeef"}, body))
            self.assertFalse(adapter.verify_signature({}, body))
        finally:
            del os.environ["CT_WEBHOOK_SECRET"]


class JsonPathTests(SimpleTestCase):
    def test_dotted_paths_traverse_dicts_and_lists(self):
        payload = {"choices": [{"message": {"content": "hello"}}]}
        self.assertEqual(dig(payload, "choices.0.message.content"), "hello")

    def test_a_missing_path_returns_the_default(self):
        self.assertEqual(dig({"a": 1}, "a.b.c", "fallback"), "fallback")
        self.assertEqual(dig({}, "", "fallback"), "fallback")


class GenericRestConfigurationTests(SimpleTestCase):
    def test_models_can_be_declared_entirely_in_configuration(self):
        adapter = GenericRESTProviderAdapter(
            AdapterConfig(
                key="generic_rest",
                endpoint="https://inference.internal.test",
                options={
                    "models": [
                        {
                            "model_id": "internal-7b",
                            "name": "Internal 7B",
                            "input_cost_per_1k": "0.0002",
                            "output_cost_per_1k": "0.0004",
                        }
                    ]
                },
            )
        )
        models = adapter.list_models()
        self.assertEqual(models[0].model_id, "internal-7b")
        self.assertEqual(models[0].input_cost_per_1k, Decimal("0.0002"))

    def test_health_check_without_an_endpoint_reports_unhealthy(self):
        result = GenericRESTProviderAdapter().health_check()
        self.assertFalse(result.healthy)
