"""Tests for the platform-module charts on the dashboard.

Each chart answers one question for one module, so the tests pin the answer
rather than the rendering: that verdicts land in the right bucket, that the
window is respected, that tenants never see each other, and that the
evaluation chart shows the latest evidence rather than the flattering one.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.core.constants import LifecycleStatus
from apps.dashboard.ranges import DateRange
from apps.dashboard.services import DashboardService
from apps.evaluations.models import EvaluationRun, EvaluationSuite, RunStatus
from apps.executions.models import Execution
from apps.models_registry.models import AIModel
from apps.organizations.models import Organization, OrganizationMembership
from apps.policies.models import Policy, PolicyEffect, PolicyType, PolicyViolation
from apps.providers.models import AIProvider
from apps.routing.models import RouteOutcome, RoutingDecision

User = get_user_model()


class ModuleChartFixture:
    def setUp(self):
        self.organization = Organization.objects.create(name="Charts Org")
        self.other = Organization.objects.create(name="Elsewhere")
        self.provider = AIProvider.objects.create(
            organization=self.organization, name="Provider", adapter_key="mock"
        )
        self.policy = self._policy(self.organization)

    def _policy(self, organization):
        return Policy.objects.create(
            organization=organization, name=f"Gate {organization.pk}",
            policy_type=PolicyType.TOOL_ACCESS, effect=PolicyEffect.DENY,
            status=LifecycleStatus.ACTIVE,
        )

    def _model(self, name, approved=True):
        return AIModel.objects.create(
            organization=self.organization, provider=self.provider,
            model_name=name, model_id=name.lower().replace(" ", "-"),
            approved_for_use=approved,
        )

    def charts(self, range_key="7"):
        date_range = DateRange.from_request({"range": range_key})
        return DashboardService(self.organization, date_range).build().charts

    @staticmethod
    def age(obj, field, days):
        type(obj).objects.filter(pk=obj.pk).update(
            **{field: timezone.now() - timedelta(days=days)}
        )


class PolicyVerdictChartTests(ModuleChartFixture, TestCase):
    def violation(self, effect, organization=None, policy=None):
        return PolicyViolation.objects.create(
            organization=organization or self.organization,
            policy=policy or self.policy,
            effect=effect,
        )

    def test_verdicts_are_bucketed_by_effect(self):
        for effect in (PolicyEffect.DENY, PolicyEffect.DENY,
                       PolicyEffect.REQUIRE_APPROVAL, PolicyEffect.WARN):
            self.violation(effect)

        verdicts = self.charts()["policy_verdicts"]
        self.assertEqual(verdicts["totals"], {"denied": 2, "approval": 1, "warned": 1})

    def test_every_day_in_the_window_is_present(self):
        # A quiet day is a zero, not a gap the reader has to interpret.
        verdicts = self.charts("7")["policy_verdicts"]
        self.assertEqual(len(verdicts["labels"]), 7)
        self.assertEqual(verdicts["denied"], [0] * 7)

    def test_verdicts_outside_the_window_are_excluded(self):
        old = self.violation(PolicyEffect.DENY)
        self.age(old, "created_at", 30)
        self.violation(PolicyEffect.DENY)

        self.assertEqual(self.charts("7")["policy_verdicts"]["totals"]["denied"], 1)

    def test_another_tenants_verdicts_never_appear(self):
        self.violation(
            PolicyEffect.DENY, organization=self.other, policy=self._policy(self.other)
        )
        self.assertEqual(self.charts()["policy_verdicts"]["totals"]["denied"], 0)


class RoutingOutcomeChartTests(ModuleChartFixture, TestCase):
    def decision(self, outcome, attempt=None, organization=None):
        return RoutingDecision.objects.create(
            organization=organization or self.organization,
            strategy="balanced", outcome=outcome, attempt=attempt,
        )

    def test_outcomes_split_first_choice_fallback_and_failure(self):
        self.decision(RouteOutcome.SERVED, attempt=1)
        self.decision(RouteOutcome.SERVED, attempt=2)
        self.decision(RouteOutcome.SERVED, attempt=3)
        self.decision(RouteOutcome.EXHAUSTED)

        routing = self.charts()["routing_outcomes"]
        self.assertEqual(routing["totals"], {"first_choice": 1, "fallback": 2, "failed": 1})

    def test_a_served_outcome_with_no_attempt_number_is_not_called_a_fallback(self):
        self.decision(RouteOutcome.SERVED, attempt=None)
        self.assertEqual(self.charts()["routing_outcomes"]["totals"]["first_choice"], 1)

    def test_the_fallback_rate_is_over_served_requests_only(self):
        self.decision(RouteOutcome.SERVED, attempt=1)
        self.decision(RouteOutcome.SERVED, attempt=2)
        self.decision(RouteOutcome.EXHAUSTED)  # not served, so not in the rate

        self.assertAlmostEqual(self.charts()["routing_outcomes"]["fallback_rate"], 0.5)

    def test_no_decisions_is_a_zero_rate_not_a_crash(self):
        self.assertEqual(self.charts()["routing_outcomes"]["fallback_rate"], 0.0)

    def test_pending_decisions_are_not_counted_as_anything(self):
        self.decision(RouteOutcome.PENDING)
        totals = self.charts()["routing_outcomes"]["totals"]
        self.assertEqual(sum(totals.values()), 0)


class GatewayCoverageChartTests(ModuleChartFixture, TestCase):
    def execution(self, source, index, organization=None):
        return Execution.objects.create(
            organization=organization or self.organization,
            external_id=f"{source}-{index}",
            source_adapter=source,
            started_at=timezone.now(),
        )

    def test_the_share_through_the_gateway_is_reported(self):
        self.execution("gateway", 1)
        for index in range(3):
            self.execution("mock", index)

        coverage = self.charts()["gateway_coverage"]
        self.assertEqual(coverage["totals"], {"gateway": 1, "reported": 3})
        self.assertAlmostEqual(coverage["gateway_share"], 0.25)

    def test_an_empty_window_is_a_zero_share(self):
        self.assertEqual(self.charts()["gateway_coverage"]["gateway_share"], 0.0)

    def test_another_tenants_traffic_is_excluded(self):
        self.execution("gateway", 1, organization=self.other)
        self.assertEqual(self.charts()["gateway_coverage"]["totals"]["gateway"], 0)


class EvaluationEvidenceChartTests(ModuleChartFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.suite = EvaluationSuite.objects.create(
            organization=self.organization, name="Baseline",
            pass_threshold=80, status=LifecycleStatus.ACTIVE,
        )

    def record_run(self, model, *, passed, score, days_ago=0, blocked=""):
        return EvaluationRun.objects.create(
            organization=self.organization, suite=self.suite, model=model,
            status=RunStatus.COMPLETED, passed=passed, score=Decimal(score),
            blocked_by_category=blocked, total_cases=5,
            finished_at=timezone.now() - timedelta(days=days_ago),
        )

    def test_the_latest_run_is_shown_not_the_best_one(self):
        model = self._model("Model A")
        self.record_run(model, passed=True, score="99.00", days_ago=5)
        self.record_run(model, passed=False, score="40.00", days_ago=0)

        evidence = self.charts()["evaluation_evidence"]
        self.assertEqual(evidence["labels"], ["Model A"])
        self.assertEqual(evidence["scores"], [40.0])
        self.assertEqual(evidence["verdicts"], ["failed"])

    def test_worst_verdicts_come_first(self):
        self.record_run(self._model("Passing"), passed=True, score="95.00")
        self.record_run(self._model("Failing"), passed=False, score="60.00")
        self.record_run(self._model("Blocked"), passed=False, score="92.00", blocked="safety")

        evidence = self.charts()["evaluation_evidence"]
        self.assertEqual(evidence["labels"], ["Blocked", "Failing", "Passing"])
        self.assertEqual(evidence["blocked_by"][0], "safety")

    def test_evidence_is_not_limited_by_the_date_range(self):
        # A model evaluated months ago still has that as its current evidence.
        self.record_run(self._model("Old"), passed=True, score="90.00", days_ago=200)
        self.assertEqual(self.charts("7")["evaluation_evidence"]["labels"], ["Old"])

    def test_each_bar_carries_its_own_suites_pass_bar(self):
        self.record_run(self._model("Model A"), passed=True, score="95.00")
        self.assertEqual(self.charts()["evaluation_evidence"]["thresholds"], [80])

    def test_approved_models_with_no_evidence_are_counted(self):
        self._model("Never Tested", approved=True)
        self._model("Not Approved", approved=False)
        self.record_run(self._model("Tested"), passed=True, score="95.00")

        self.assertEqual(self.charts()["evaluation_evidence"]["unevaluated_approved"], 1)

    def test_the_chart_is_capped(self):
        for index in range(DashboardService.EVIDENCE_LIMIT + 3):
            self.record_run(self._model(f"Model {index:02d}"), passed=True, score="95.00")
        self.assertEqual(
            len(self.charts()["evaluation_evidence"]["labels"]), DashboardService.EVIDENCE_LIMIT
        )


class ModuleChartRenderTests(ModuleChartFixture, TestCase):
    def test_the_dashboard_renders_every_module_chart_with_a_table_twin(self):
        user = User.objects.create_user(email="exec@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=user, role=Role.ORG_ADMIN, is_default=True
        )
        self.client.force_login(user)

        response = self.client.get(reverse("dashboard:home"))
        for canvas in ("chart-policy-verdicts", "chart-routing-outcomes",
                       "chart-gateway-coverage", "chart-evaluation-evidence"):
            with self.subTest(chart=canvas):
                self.assertContains(response, f'id="{canvas}"')
        self.assertContains(response, "View as table", count=4)

    def test_the_latency_chart_no_longer_carries_a_second_axis(self):
        user = User.objects.create_user(email="exec2@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=user, role=Role.ORG_ADMIN, is_default=True
        )
        self.client.force_login(user)

        response = self.client.get(reverse("dashboard:home"))
        self.assertNotContains(response, "yAxisID")
