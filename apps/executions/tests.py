from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.agents.models import AIAgent, AutonomyLevel
from apps.applications_registry.models import AIApplication
from apps.core.constants import Environment, RiskLevel
from apps.executions.generators import MockExecutionGenerator, purge
from apps.executions.models import (
    ApprovalState,
    Execution,
    ExecutionEvent,
    ExecutionStatusChoices,
    ExecutionTrace,
)
from apps.executions.services import AssetResolver, ExecutionService
from apps.models_registry.models import AIModel
from apps.organizations.models import Organization, OrganizationMembership
from apps.providers.models import AIProvider
from integrations.base.events import EventType

User = get_user_model()


class ExecutionFixtureMixin:
    def build_estate(self, name="Estate Org"):
        self.organization = Organization.objects.create(name=name)
        self.provider = AIProvider.objects.create(
            organization=self.organization, name="Mock Provider", adapter_key="mock"
        )
        self.model = AIModel.objects.create(
            organization=self.organization,
            provider=self.provider,
            model_name="Mock Large",
            model_id="mock-large",
            input_cost_per_1k=Decimal("0.003"),
            output_cost_per_1k=Decimal("0.015"),
            approved_for_use=True,
        )
        self.agent = AIAgent.objects.create(
            organization=self.organization,
            name="Support Agent",
            agent_id="support-agent",
            provider=self.provider,
            primary_model=self.model,
            environment=Environment.PRODUCTION,
        )
        self.application = AIApplication.objects.create(
            organization=self.organization, name="Support Portal"
        )
        self.application.agents.add(self.agent)


class ExecutionModelTests(ExecutionFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()

    def test_total_tokens_is_maintained_on_save(self):
        execution = Execution.objects.create(
            organization=self.organization, external_id="e1", input_tokens=100, output_tokens=50
        )
        self.assertEqual(execution.total_tokens, 150)

    def test_duration_falls_back_to_the_timestamps(self):
        started = timezone.now()
        execution = Execution.objects.create(
            organization=self.organization,
            external_id="e2",
            started_at=started,
            ended_at=started + timedelta(seconds=3),
        )
        self.assertEqual(execution.duration_ms, 3000)
        self.assertEqual(execution.duration_display, "3.00 s")

    def test_reported_latency_wins_over_the_timestamps(self):
        started = timezone.now()
        execution = Execution.objects.create(
            organization=self.organization,
            external_id="e3",
            started_at=started,
            ended_at=started + timedelta(seconds=9),
            latency_ms=1200,
        )
        self.assertEqual(execution.duration_ms, 1200)

    def test_live_and_terminal_states(self):
        running = Execution.objects.create(
            organization=self.organization, external_id="e4",
            status=ExecutionStatusChoices.RUNNING,
        )
        done = Execution.objects.create(
            organization=self.organization, external_id="e5",
            status=ExecutionStatusChoices.COMPLETED,
        )
        self.assertTrue(running.is_live)
        self.assertFalse(running.is_terminal)
        self.assertTrue(done.is_terminal)

    def test_execution_ids_are_unique_within_an_organization(self):
        from django.db import IntegrityError

        Execution.objects.create(organization=self.organization, external_id="dup")
        with self.assertRaises(IntegrityError):
            Execution.objects.create(organization=self.organization, external_id="dup")

    def test_the_same_execution_id_may_exist_in_another_organization(self):
        other = Organization.objects.create(name="Other Estate")
        Execution.objects.create(organization=self.organization, external_id="shared")
        Execution.objects.create(organization=other, external_id="shared")
        self.assertEqual(Execution.objects.filter(external_id="shared").count(), 2)


class AssetResolverTests(ExecutionFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.resolver = AssetResolver(self.organization)

    def test_an_agent_resolves_by_business_id(self):
        self.assertEqual(self.resolver.agent("support-agent"), self.agent)

    def test_an_agent_resolves_by_name(self):
        self.assertEqual(self.resolver.agent("Support Agent"), self.agent)

    def test_an_agent_resolves_by_uuid(self):
        self.assertEqual(self.resolver.agent(str(self.agent.pk)), self.agent)

    def test_a_model_resolves_by_provider_side_id(self):
        self.assertEqual(self.resolver.model("mock-large"), self.model)

    def test_an_unknown_identifier_resolves_to_none(self):
        self.assertIsNone(self.resolver.agent("no-such-agent"))
        self.assertIsNone(self.resolver.agent(""))

    def test_resolution_never_crosses_an_organization_boundary(self):
        other = Organization.objects.create(name="Foreign Org")
        foreign_agent = AIAgent.objects.create(
            organization=other, name="Foreign", agent_id="foreign"
        )
        self.assertIsNone(self.resolver.agent("foreign"))
        self.assertIsNone(self.resolver.agent(str(foreign_agent.pk)))


class ExecutionServiceTests(ExecutionFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()

    def test_record_creates_an_execution_and_resolves_assets(self):
        execution = ExecutionService.record(
            organization=self.organization, external_id="run-1", agent="support-agent"
        )
        self.assertEqual(execution.agent, self.agent)
        self.assertEqual(execution.provider, self.provider)
        self.assertEqual(execution.model, self.model)
        self.assertEqual(execution.application, self.application)

    def test_record_is_idempotent_on_the_external_id(self):
        ExecutionService.record(
            organization=self.organization, external_id="run-2", input_tokens=10
        )
        ExecutionService.record(
            organization=self.organization, external_id="run-2", input_tokens=99
        )
        self.assertEqual(Execution.objects.filter(external_id="run-2").count(), 1)
        self.assertEqual(Execution.objects.get(external_id="run-2").input_tokens, 99)

    def test_an_execution_without_an_id_still_records(self):
        execution = ExecutionService.record(organization=self.organization)
        self.assertTrue(execution.external_id)

    def test_a_trace_is_created_and_reused(self):
        first = ExecutionService.record(
            organization=self.organization, external_id="t1", trace_id="trace-abc"
        )
        second = ExecutionService.record(
            organization=self.organization, external_id="t2", trace_id="trace-abc"
        )
        self.assertEqual(first.trace, second.trace)
        self.assertEqual(ExecutionTrace.objects.filter(trace_id="trace-abc").count(), 1)
        self.assertEqual(first.trace_key, "trace-abc")

    def test_cost_is_derived_from_the_registry_when_the_engine_reports_none(self):
        execution = ExecutionService.record(
            organization=self.organization,
            external_id="cost-1",
            agent="support-agent",
            input_tokens=1000,
            output_tokens=1000,
        )
        # 1000/1000 * 0.003 + 1000/1000 * 0.015
        self.assertEqual(execution.cost, Decimal("0.018000"))
        self.assertTrue(execution.cost_is_derived)

    def test_an_engine_reported_cost_is_not_overwritten(self):
        execution = ExecutionService.record(
            organization=self.organization,
            external_id="cost-2",
            agent="support-agent",
            input_tokens=1000,
            output_tokens=1000,
            cost=Decimal("0.99"),
        )
        self.assertEqual(execution.cost, Decimal("0.99"))
        self.assertFalse(execution.cost_is_derived)

    def test_agent_settings_seed_the_execution(self):
        self.agent.autonomy_level = AutonomyLevel.AUTONOMOUS
        self.agent.requires_human_approval = True
        self.agent.save()

        execution = ExecutionService.record(
            organization=self.organization, external_id="appr-1", agent="support-agent"
        )
        self.assertTrue(execution.approval_required)
        self.assertEqual(execution.environment, Environment.PRODUCTION)

    def test_requiring_approval_does_not_by_itself_mark_a_run_pending(self):
        """Policy is not a decision: a finished run must not show 'Pending'."""
        self.agent.requires_human_approval = True
        self.agent.save()

        execution = ExecutionService.record(
            organization=self.organization,
            external_id="appr-2",
            agent="support-agent",
            status=ExecutionStatusChoices.COMPLETED,
        )
        self.assertTrue(execution.approval_required)
        self.assertEqual(execution.approval_status, ApprovalState.NOT_REQUIRED)

    def test_a_run_paused_for_approval_is_marked_pending(self):
        self.agent.requires_human_approval = True
        self.agent.save()

        execution = ExecutionService.record(
            organization=self.organization,
            external_id="appr-3",
            agent="support-agent",
            status=ExecutionStatusChoices.WAITING_FOR_APPROVAL,
        )
        self.assertEqual(execution.approval_status, ApprovalState.PENDING)


class ExecutionEventTests(ExecutionFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.execution = ExecutionService.record(
            organization=self.organization, external_id="ev-1", agent="support-agent"
        )

    def test_events_are_appended_with_increasing_sequence(self):
        ExecutionService.append_events(
            self.execution,
            [
                {"event_type": "EXECUTION_STARTED"},
                {"event_type": "MODEL_REQUEST_STARTED"},
            ],
        )
        sequences = list(self.execution.events.values_list("sequence", flat=True))
        self.assertEqual(sequences, [0, 1])

    def test_unknown_event_types_are_dropped_not_guessed(self):
        created = ExecutionService.append_events(
            self.execution,
            [{"event_type": "EXECUTION_STARTED"}, {"event_type": "SOMETHING_ELSE"}],
        )
        self.assertEqual(len(created), 1)

    def test_replaying_an_event_id_does_not_duplicate(self):
        event_id = "11111111-1111-1111-1111-111111111111"
        payload = [{"event_id": event_id, "event_type": "EXECUTION_STARTED"}]

        ExecutionService.append_events(self.execution, payload)
        ExecutionService.append_events(self.execution, payload)

        self.assertEqual(self.execution.events.count(), 1)

    def test_usage_rolls_up_from_events_when_totals_are_absent(self):
        ExecutionService.append_events(
            self.execution,
            [
                {"event_type": "MODEL_REQUEST_COMPLETED", "input_tokens": 500, "output_tokens": 200},
                {"event_type": "MODEL_REQUEST_COMPLETED", "input_tokens": 300, "output_tokens": 100},
            ],
        )
        self.execution.refresh_from_db()
        self.assertEqual(self.execution.input_tokens, 800)
        self.assertEqual(self.execution.output_tokens, 300)
        self.assertEqual(self.execution.total_tokens, 1100)

    def test_reported_totals_are_not_overwritten_by_event_rollup(self):
        execution = ExecutionService.record(
            organization=self.organization, external_id="ev-2", input_tokens=5000, output_tokens=100
        )
        ExecutionService.append_events(
            execution, [{"event_type": "MODEL_REQUEST_COMPLETED", "input_tokens": 7}]
        )
        execution.refresh_from_db()
        self.assertEqual(execution.input_tokens, 5000)

    def test_a_terminal_event_closes_the_execution(self):
        ExecutionService.append_events(
            self.execution,
            [{"event_type": "EXECUTION_STARTED"}, {"event_type": "EXECUTION_COMPLETED"}],
        )
        self.execution.refresh_from_db()
        self.assertEqual(self.execution.status, ExecutionStatusChoices.COMPLETED)
        self.assertIsNotNone(self.execution.ended_at)

    def test_an_approval_request_event_flags_the_execution(self):
        ExecutionService.append_events(self.execution, [{"event_type": "APPROVAL_REQUESTED"}])
        self.execution.refresh_from_db()
        self.assertTrue(self.execution.approval_required)
        self.assertEqual(self.execution.approval_status, ApprovalState.PENDING)


class ExecutionLifecycleTests(ExecutionFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.execution = ExecutionService.record(
            organization=self.organization,
            external_id="lc-1",
            agent="support-agent",
            status=ExecutionStatusChoices.RUNNING,
            started_at=timezone.now() - timedelta(seconds=4),
        )

    def test_complete_sets_status_latency_and_end_time(self):
        execution = ExecutionService.complete(self.execution, input_tokens=10, output_tokens=5)
        self.assertEqual(execution.status, ExecutionStatusChoices.COMPLETED)
        self.assertIsNotNone(execution.ended_at)
        self.assertGreater(execution.latency_ms, 0)
        self.assertEqual(execution.total_tokens, 15)

    def test_fail_records_the_error(self):
        execution = ExecutionService.fail(self.execution, error_message="provider timeout")
        self.assertEqual(execution.status, ExecutionStatusChoices.FAILED)
        self.assertEqual(execution.error_message, "provider timeout")

    def test_cancel_records_the_reason(self):
        execution = ExecutionService.cancel(self.execution, reason="operator stopped it")
        self.assertEqual(execution.status, ExecutionStatusChoices.CANCELLED)
        self.assertEqual(execution.error_message, "operator stopped it")

    def test_finishing_clears_a_pending_approval(self):
        self.execution.approval_required = True
        self.execution.approval_status = ApprovalState.PENDING
        self.execution.save()

        execution = ExecutionService.complete(self.execution)
        self.assertEqual(execution.approval_status, ApprovalState.CANCELLED)

    def test_pricing_is_applied_on_completion(self):
        execution = ExecutionService.complete(self.execution, input_tokens=1000, output_tokens=1000)
        self.assertEqual(execution.cost, Decimal("0.018000"))


class MockExecutionGeneratorTests(ExecutionFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()

    def test_it_generates_executions_with_events(self):
        result = MockExecutionGenerator(self.organization, seed=1).generate(20)

        self.assertEqual(result.executions, 20)
        self.assertGreater(result.events, 0)
        self.assertEqual(Execution.objects.for_organization(self.organization).count(), 20)
        self.assertTrue(ExecutionEvent.objects.exists())

    def test_generated_runs_span_a_range_of_statuses(self):
        MockExecutionGenerator(self.organization, seed=7).generate(60)
        statuses = set(
            Execution.objects.for_organization(self.organization).values_list("status", flat=True)
        )
        self.assertIn(ExecutionStatusChoices.COMPLETED, statuses)
        self.assertGreater(len(statuses), 1)

    def test_generated_runs_are_priced(self):
        MockExecutionGenerator(self.organization, seed=3).generate(10)
        self.assertTrue(
            Execution.objects.for_organization(self.organization).filter(cost__gt=0).exists()
        )

    def test_live_runs_are_recent(self):
        MockExecutionGenerator(self.organization, seed=5).generate(60)
        stale_live = Execution.objects.for_organization(self.organization).filter(
            status__in=[
                ExecutionStatusChoices.RUNNING,
                ExecutionStatusChoices.WAITING_FOR_APPROVAL,
            ],
            started_at__lt=timezone.now() - timedelta(hours=2),
        )
        self.assertFalse(stale_live.exists())

    def test_purge_removes_generated_runs_only(self):
        MockExecutionGenerator(self.organization, seed=2).generate(5)
        ExecutionService.record(
            organization=self.organization, external_id="real-1", source_adapter="control_tower_api"
        )

        purge(self.organization)
        remaining = Execution.objects.for_organization(self.organization)
        self.assertEqual(remaining.count(), 1)
        self.assertEqual(remaining.first().external_id, "real-1")

    def test_a_live_run_carries_no_terminal_event(self):
        """A terminal event would close the run out on ingest."""
        MockExecutionGenerator(self.organization, seed=11).generate(120)

        live = Execution.objects.for_organization(self.organization).filter(
            status__in=[
                ExecutionStatusChoices.RUNNING,
                ExecutionStatusChoices.WAITING_FOR_APPROVAL,
            ]
        )
        self.assertTrue(live.exists())
        for execution in live:
            types = set(execution.events.values_list("event_type", flat=True))
            self.assertNotIn(str(EventType.EXECUTION_COMPLETED), types)

    def test_a_failed_run_ends_on_an_error_event(self):
        MockExecutionGenerator(self.organization, seed=13).generate(120)

        failed = Execution.objects.for_organization(self.organization).filter(
            status=ExecutionStatusChoices.FAILED
        )
        self.assertTrue(failed.exists())
        for execution in failed:
            types = set(execution.events.values_list("event_type", flat=True))
            self.assertIn(str(EventType.ERROR), types)
            self.assertNotIn(str(EventType.EXECUTION_COMPLETED), types)

    def test_running_executions_are_produced(self):
        MockExecutionGenerator(self.organization, seed=17).generate(200)
        self.assertTrue(
            Execution.objects.for_organization(self.organization)
            .filter(status=ExecutionStatusChoices.RUNNING)
            .exists()
        )

    def test_generation_is_a_no_op_without_agents(self):
        empty_org = Organization.objects.create(name="No Agents Org")
        result = MockExecutionGenerator(empty_org).generate(10)
        self.assertEqual(result.executions, 0)


class ExecutionViewTests(ExecutionFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate("View Org")
        self.user = User.objects.create_user(email="ops@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.user, role=Role.VIEWER, is_default=True
        )
        self.client.force_login(self.user)

        self.execution = ExecutionService.record(
            organization=self.organization,
            external_id="visible-run",
            agent="support-agent",
            status=ExecutionStatusChoices.COMPLETED,
        )
        ExecutionService.append_events(
            self.execution,
            [{"event_type": "EXECUTION_STARTED"}, {"event_type": "EXECUTION_COMPLETED"}],
        )

        self.other_org = Organization.objects.create(name="Rival Org")
        self.foreign = Execution.objects.create(
            organization=self.other_org, external_id="foreign-run"
        )

    def test_the_list_shows_only_the_active_organization(self):
        response = self.client.get(reverse("executions:list"))
        self.assertContains(response, "visible-run")
        self.assertNotContains(response, "foreign-run")

    def test_a_foreign_execution_returns_404(self):
        response = self.client.get(reverse("executions:detail", args=[self.foreign.pk]))
        self.assertEqual(response.status_code, 404)

    def test_the_detail_page_renders_the_event_timeline(self):
        response = self.client.get(reverse("executions:detail", args=[self.execution.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Execution Timeline")
        self.assertEqual(len(response.context["events"]), 2)

    def test_filtering_by_status(self):
        ExecutionService.record(
            organization=self.organization,
            external_id="failed-run",
            status=ExecutionStatusChoices.FAILED,
        )
        response = self.client.get(reverse("executions:list"), {"status": "failed"})
        self.assertContains(response, "failed-run")
        self.assertNotContains(response, "visible-run")

    def test_the_summary_reflects_the_filtered_set(self):
        response = self.client.get(reverse("executions:list"), {"status": "completed"})
        self.assertEqual(response.context["summary"]["total"], 1)

    def test_the_live_monitor_partial_renders(self):
        response = self.client.get(reverse("executions:live_table"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("stats", response.context)

    def test_the_live_monitor_page_renders(self):
        self.assertEqual(self.client.get(reverse("executions:live")).status_code, 200)

    def test_anonymous_users_are_redirected(self):
        self.client.logout()
        response = self.client.get(reverse("executions:list"))
        self.assertEqual(response.status_code, 302)


class DashboardExecutionMetricsTests(ExecutionFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate("Metrics Org")
        self.user = User.objects.create_user(email="exec@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.user, role=Role.ORG_ADMIN, is_default=True
        )
        self.client.force_login(self.user)

        for index in range(3):
            ExecutionService.record(
                organization=self.organization,
                external_id=f"today-{index}",
                agent="support-agent",
                status=ExecutionStatusChoices.COMPLETED,
                input_tokens=1000,
                output_tokens=500,
            )
        ExecutionService.record(
            organization=self.organization,
            external_id="today-failed",
            agent="support-agent",
            status=ExecutionStatusChoices.FAILED,
        )

    def test_execution_metrics_are_populated(self):
        response = self.client.get(reverse("dashboard:home"), {"range": "1"})
        metrics = {m.label: m.value for m in response.context["data"].metrics}

        self.assertEqual(metrics["Executions"], "4")
        self.assertEqual(metrics["Successful"], "3")
        self.assertEqual(metrics["Failed"], "1")
        self.assertEqual(metrics["Tokens"], "4,500")

    def test_cost_metrics_reflect_derived_pricing(self):
        response = self.client.get(reverse("dashboard:home"), {"range": "1"})
        metrics = {m.label: m.value for m in response.context["data"].metrics}
        # 3 runs x (1000/1000*0.003 + 500/1000*0.015) = 3 x 0.0105
        self.assertEqual(metrics["Cost"], "$0.03")

    def test_execution_charts_follow_the_selected_range(self):
        response = self.client.get(reverse("dashboard:home"), {"range": "7"})
        charts = response.context["data"].charts

        self.assertIn("execution_volume", charts)
        self.assertIn("spend_over_time", charts)
        self.assertEqual(len(charts["execution_volume"]["labels"]), 7)

    def test_a_failing_agent_reaches_the_attention_panel(self):
        for index in range(9):
            ExecutionService.record(
                organization=self.organization,
                external_id=f"bad-{index}",
                agent="support-agent",
                status=ExecutionStatusChoices.FAILED,
            )
        response = self.client.get(reverse("dashboard:home"))
        titles = " ".join(item.title for item in response.context["data"].attention)
        self.assertIn("is failing", titles)
