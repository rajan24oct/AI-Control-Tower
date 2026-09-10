"""Tests for the execution and telemetry APIs (section 5)."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.roles import Role
from apps.agents.models import AIAgent
from apps.api.models import APIKey, ServiceAccount
from apps.executions.models import Execution, ExecutionStatusChoices
from apps.models_registry.models import AIModel
from apps.organizations.models import Organization, OrganizationMembership
from apps.providers.models import AIProvider

User = get_user_model()


class ExecutionAPIMixin:
    def build_api_estate(self):
        self.client = APIClient()
        self.organization = Organization.objects.create(name="API Estate")
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
            name="API Agent",
            agent_id="api-agent",
            provider=self.provider,
            primary_model=self.model,
        )
        self.account = ServiceAccount.objects.create(
            organization=self.organization,
            name="langgraph-runtime",
            external_system="LangGraph on Kubernetes",
            role=Role.AI_DEVELOPER,
        )
        _, self.raw_key = APIKey.generate(self.account)

    def post(self, url, data, **kwargs):
        return self.client.post(url, data, format="json", HTTP_X_API_KEY=self.raw_key, **kwargs)

    def get(self, url, data=None):
        return self.client.get(url, data, HTTP_X_API_KEY=self.raw_key)


class ExecutionIngestionAPITests(ExecutionAPIMixin, TestCase):
    def setUp(self):
        self.build_api_estate()
        self.list_url = reverse("api:execution-list")

    def test_an_engine_registers_a_run(self):
        response = self.post(
            self.list_url,
            {
                "execution_id": "run-alpha",
                "agent": "api-agent",
                "status": "running",
                "trace_id": "trace-1",
            },
        )
        self.assertEqual(response.status_code, 201)

        body = response.json()
        self.assertEqual(body["execution_id"], "run-alpha")
        self.assertEqual(body["agent_name"], "API Agent")
        self.assertEqual(body["trace_id"], "trace-1")

    def test_a_run_can_be_submitted_with_its_whole_timeline_at_once(self):
        response = self.post(
            self.list_url,
            {
                "execution_id": "run-beta",
                "agent": "api-agent",
                "status": "completed",
                "input_tokens": 1000,
                "output_tokens": 1000,
                "events": [
                    {"event_type": "EXECUTION_STARTED"},
                    {"event_type": "MODEL_REQUEST_COMPLETED", "duration_ms": 900},
                    {"event_type": "EXECUTION_COMPLETED"},
                ],
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["event_count"], 3)

    def test_resubmitting_the_same_run_updates_it(self):
        self.post(self.list_url, {"execution_id": "run-gamma", "status": "running"})
        self.post(self.list_url, {"execution_id": "run-gamma", "status": "completed"})

        self.assertEqual(Execution.objects.filter(external_id="run-gamma").count(), 1)
        self.assertEqual(
            Execution.objects.get(external_id="run-gamma").status,
            ExecutionStatusChoices.COMPLETED,
        )

    def test_cost_is_derived_from_the_registry_on_ingest(self):
        response = self.post(
            self.list_url,
            {
                "execution_id": "run-cost",
                "agent": "api-agent",
                "input_tokens": 1000,
                "output_tokens": 1000,
            },
        )
        self.assertEqual(Decimal(response.json()["cost"]), Decimal("0.018000"))
        self.assertTrue(response.json()["cost_is_derived"])

    def test_the_submitting_service_account_is_recorded(self):
        self.post(self.list_url, {"execution_id": "run-src"})
        execution = Execution.objects.get(external_id="run-src")
        self.assertEqual(execution.service_account, self.account)

    def test_an_invalid_status_is_rejected(self):
        response = self.post(self.list_url, {"execution_id": "bad", "status": "exploded"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("status", response.json())


class ExecutionEventAPITests(ExecutionAPIMixin, TestCase):
    def setUp(self):
        self.build_api_estate()
        self.post(
            reverse("api:execution-list"),
            {"execution_id": "run-events", "agent": "api-agent", "status": "running"},
        )
        self.execution = Execution.objects.get(external_id="run-events")
        self.events_url = reverse("api:execution-events", args=[self.execution.pk])

    def test_events_can_be_appended_in_a_batch(self):
        response = self.post(
            self.events_url,
            [
                {"event_type": "AGENT_STARTED"},
                {"event_type": "TOOL_CALL_COMPLETED", "duration_ms": 120,
                 "metadata": {"tool": "crm_lookup"}},
            ],
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["accepted"], 2)
        self.assertEqual(self.execution.events.count(), 2)

    def test_a_single_event_can_be_appended(self):
        response = self.post(self.events_url, {"event_type": "PROMPT_CREATED"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["accepted"], 1)

    def test_the_timeline_can_be_read_back(self):
        self.post(self.events_url, [{"event_type": "AGENT_STARTED"}])
        response = self.get(self.events_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["event_type"], "AGENT_STARTED")

    def test_an_unknown_event_type_is_rejected_by_validation(self):
        response = self.post(self.events_url, [{"event_type": "MADE_UP"}])
        self.assertEqual(response.status_code, 400)

    def test_usage_on_events_rolls_up_to_the_execution(self):
        self.post(
            self.events_url,
            [{"event_type": "MODEL_REQUEST_COMPLETED", "input_tokens": 400, "output_tokens": 100}],
        )
        self.execution.refresh_from_db()
        self.assertEqual(self.execution.total_tokens, 500)

    def test_an_execution_is_addressable_by_the_engines_own_id(self):
        url = reverse("api:execution-events", args=["run-events"])
        response = self.post(url, [{"event_type": "AGENT_STARTED"}])
        self.assertEqual(response.status_code, 201)


class ExecutionLifecycleAPITests(ExecutionAPIMixin, TestCase):
    def setUp(self):
        self.build_api_estate()
        self.post(
            reverse("api:execution-list"),
            {"execution_id": "run-lifecycle", "agent": "api-agent", "status": "running"},
        )
        self.execution = Execution.objects.get(external_id="run-lifecycle")

    def test_complete(self):
        url = reverse("api:execution-complete", args=[self.execution.pk])
        response = self.post(url, {"input_tokens": 10, "output_tokens": 5})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "completed")
        self.assertEqual(response.json()["total_tokens"], 15)

    def test_fail(self):
        url = reverse("api:execution-fail", args=[self.execution.pk])
        response = self.post(url, {"error_message": "provider timeout"})

        self.assertEqual(response.json()["status"], "failed")
        self.assertEqual(response.json()["error_message"], "provider timeout")

    def test_cancel(self):
        url = reverse("api:execution-cancel", args=[self.execution.pk])
        response = self.post(url, {"reason": "operator stopped it"})

        self.assertEqual(response.json()["status"], "cancelled")
        self.assertEqual(response.json()["error_message"], "operator stopped it")

    def test_completing_computes_latency(self):
        url = reverse("api:execution-complete", args=[self.execution.pk])
        self.assertIsNotNone(self.post(url, {}).json()["latency_ms"])

    def test_a_reported_latency_beats_the_wall_clock(self):
        # The engine timed the run; we only saw the gap between two HTTP calls.
        url = reverse("api:execution-complete", args=[self.execution.pk])
        self.assertEqual(self.post(url, {"latency_ms": 1240}).json()["latency_ms"], 1240)


class TelemetryWebhookAPITests(ExecutionAPIMixin, TestCase):
    def setUp(self):
        self.build_api_estate()
        self.url = reverse("api:telemetry-webhook")

    def test_a_whole_engine_payload_is_normalized(self):
        response = self.post(
            self.url,
            {
                "payload": {
                    "execution_id": "hook-1",
                    "status": "succeeded",
                    "agent": "api-agent",
                    "usage": {"input_tokens": 500, "output_tokens": 250},
                    "events": [
                        {"type": "EXECUTION_STARTED"},
                        {"type": "EXECUTION_COMPLETED"},
                    ],
                }
            },
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["execution_id"], "hook-1")
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["event_count"], 2)

    def test_an_unknown_adapter_returns_422(self):
        response = self.post(
            self.url, {"adapter": "nonexistent", "payload": {"execution_id": "x"}}
        )
        self.assertEqual(response.status_code, 422)

    def test_a_missing_payload_is_rejected(self):
        self.assertEqual(self.post(self.url, {}).status_code, 400)


class ExecutionAPIAccessControlTests(ExecutionAPIMixin, TestCase):
    def setUp(self):
        self.build_api_estate()
        self.list_url = reverse("api:execution-list")

        self.other_org = Organization.objects.create(name="Rival")
        self.foreign = Execution.objects.create(
            organization=self.other_org, external_id="rival-run"
        )
        self.post(self.list_url, {"execution_id": "mine-run"})

    def test_unauthenticated_access_is_refused(self):
        self.assertEqual(APIClient().get(self.list_url).status_code, 401)

    def test_a_service_account_sees_only_its_own_organization(self):
        ids = [row["execution_id"] for row in self.get(self.list_url).json()["results"]]
        self.assertEqual(ids, ["mine-run"])

    def test_a_foreign_execution_is_not_retrievable(self):
        url = reverse("api:execution-detail", args=[self.foreign.pk])
        self.assertEqual(self.get(url).status_code, 404)

    def test_a_viewer_may_read_but_not_ingest(self):
        viewer = User.objects.create_user(email="viewer@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=viewer, role=Role.VIEWER, is_default=True
        )
        client = APIClient()
        client.force_login(viewer)

        self.assertEqual(client.get(self.list_url).status_code, 200)
        response = client.post(self.list_url, {"execution_id": "nope"}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_a_viewer_cannot_post_to_the_webhook(self):
        viewer = User.objects.create_user(email="viewer2@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=viewer, role=Role.VIEWER, is_default=True
        )
        client = APIClient()
        client.force_login(viewer)

        response = client.post(
            reverse("api:telemetry-webhook"), {"payload": {"execution_id": "x"}}, format="json"
        )
        self.assertEqual(response.status_code, 403)

    def test_filtering_and_search_are_organization_bound(self):
        response = self.get(self.list_url, {"search": "rival-run"})
        self.assertEqual(response.json()["count"], 0)
