from decimal import Decimal

from django.test import TestCase

from apps.accounts.roles import Role
from apps.agents.models import AIAgent
from apps.api.models import ServiceAccount
from apps.executions.models import Execution, ExecutionStatusChoices
from apps.models_registry.models import AIModel
from apps.organizations.models import Organization
from apps.providers.models import AIProvider
from apps.telemetry.models import IngestionStatus, RawTelemetryPayload
from apps.telemetry.services import TelemetryIngestionError, TelemetryService


class TelemetryIngestionTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Ingest Org")
        self.provider = AIProvider.objects.create(
            organization=self.organization, name="Mock", adapter_key="mock"
        )
        self.model = AIModel.objects.create(
            organization=self.organization,
            provider=self.provider,
            model_name="Mock Large",
            model_id="mock-large",
            input_cost_per_1k=Decimal("0.003"),
            output_cost_per_1k=Decimal("0.015"),
        )
        self.agent = AIAgent.objects.create(
            organization=self.organization,
            name="Ingest Agent",
            agent_id="ingest-agent",
            provider=self.provider,
            primary_model=self.model,
        )
        self.service_account = ServiceAccount.objects.create(
            organization=self.organization, name="engine", role=Role.AI_DEVELOPER
        )

    def _payload(self, **overrides):
        payload = {
            "execution_id": "engine-run-1",
            "status": "succeeded",
            "agent": "ingest-agent",
            "model": "mock-large",
            "trace_id": "trace-xyz",
            "latency_ms": 2400,
            "usage": {"input_tokens": 1000, "output_tokens": 1000},
            "events": [
                {"type": "EXECUTION_STARTED", "timestamp": "2026-09-09T10:00:00Z"},
                {
                    "type": "MODEL_REQUEST_COMPLETED",
                    "timestamp": "2026-09-09T10:00:02Z",
                    "duration_ms": 2100,
                    "usage": {"input_tokens": 1000, "output_tokens": 1000},
                },
                {"type": "EXECUTION_COMPLETED", "timestamp": "2026-09-09T10:00:02Z"},
            ],
        }
        payload.update(overrides)
        return payload

    def test_a_payload_becomes_an_execution_with_a_timeline(self):
        execution = TelemetryService.ingest(
            organization=self.organization,
            payload=self._payload(),
            service_account=self.service_account,
        )

        self.assertEqual(execution.external_id, "engine-run-1")
        self.assertEqual(execution.status, ExecutionStatusChoices.COMPLETED)
        self.assertEqual(execution.agent, self.agent)
        self.assertEqual(execution.model, self.model)
        self.assertEqual(execution.events.count(), 3)

    def test_the_trace_is_recorded(self):
        execution = TelemetryService.ingest(
            organization=self.organization, payload=self._payload()
        )
        self.assertEqual(execution.trace_key, "trace-xyz")

    def test_the_submitting_system_is_recorded(self):
        execution = TelemetryService.ingest(
            organization=self.organization,
            payload=self._payload(),
            service_account=self.service_account,
        )
        self.assertEqual(execution.service_account, self.service_account)
        self.assertEqual(execution.source_adapter, "generic_webhook")

    def test_the_raw_payload_is_retained(self):
        TelemetryService.ingest(organization=self.organization, payload=self._payload())

        raw = RawTelemetryPayload.objects.get()
        self.assertEqual(raw.status, IngestionStatus.PROCESSED)
        self.assertEqual(raw.event_count, 3)
        self.assertEqual(raw.payload["execution_id"], "engine-run-1")

    def test_cost_is_derived_from_the_registry(self):
        execution = TelemetryService.ingest(
            organization=self.organization, payload=self._payload()
        )
        self.assertEqual(execution.cost, Decimal("0.018000"))
        self.assertTrue(execution.cost_is_derived)

    def test_ingestion_is_idempotent(self):
        TelemetryService.ingest(organization=self.organization, payload=self._payload())
        TelemetryService.ingest(organization=self.organization, payload=self._payload())

        self.assertEqual(Execution.objects.count(), 1)
        self.assertEqual(RawTelemetryPayload.objects.count(), 2)

    def test_a_foreign_payload_shape_works_through_adapter_options(self):
        execution = TelemetryService.ingest(
            organization=self.organization,
            payload={"run": {"id": "vendor-1", "state": "in_progress"}},
            adapter_options={
                "field_mapping": {"external_id": "run.id", "status": "run.state"}
            },
        )
        self.assertEqual(execution.external_id, "vendor-1")
        self.assertEqual(execution.status, ExecutionStatusChoices.RUNNING)

    def test_an_unknown_adapter_is_rejected_and_recorded(self):
        with self.assertRaises(TelemetryIngestionError):
            TelemetryService.ingest(
                organization=self.organization,
                payload=self._payload(),
                adapter_key="no-such-adapter",
            )

        raw = RawTelemetryPayload.objects.get()
        self.assertEqual(raw.status, IngestionStatus.REJECTED)
        self.assertIn("no-such-adapter", raw.processing_error)

    def test_ingestion_writes_an_audit_entry(self):
        from apps.audit.models import AuditLog

        TelemetryService.ingest(
            organization=self.organization,
            payload=self._payload(),
            service_account=self.service_account,
        )
        entry = AuditLog.objects.filter(action="telemetry_ingest").first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.metadata["events"], 3)

    def test_a_stored_payload_can_be_replayed(self):
        TelemetryService.ingest(organization=self.organization, payload=self._payload())
        Execution.objects.all().delete()

        raw = RawTelemetryPayload.objects.first()
        execution = TelemetryService.replay(raw)

        self.assertEqual(execution.external_id, "engine-run-1")
        self.assertEqual(execution.events.count(), 3)

    def test_unrecognized_event_types_do_not_break_ingestion(self):
        payload = self._payload(
            events=[{"type": "EXECUTION_STARTED"}, {"type": "VENDOR_SPECIFIC_THING"}]
        )
        execution = TelemetryService.ingest(organization=self.organization, payload=payload)

        self.assertEqual(execution.events.count(), 1)
        # The unrecognized event survives in the raw payload for later analysis.
        raw = RawTelemetryPayload.objects.get()
        self.assertEqual(len(raw.payload["events"]), 2)
