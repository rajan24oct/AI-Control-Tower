"""Tests for the Evaluation Centre.

Weighted towards the ways an evaluation could mislead rather than break: a
scorer that passes when it should not, a run that reads as green while a safety
case failed, or a broken check that quietly stops checking anything.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.roles import Role
from apps.agents.models import AIAgent
from apps.core.constants import Environment, HealthStatus, LifecycleStatus
from apps.evaluations.models import (
    CaseCategory,
    EvaluationCase,
    EvaluationResult,
    EvaluationRun,
    EvaluationSuite,
    RunStatus,
    Scorer,
    TargetKind,
)
from apps.evaluations.services import EvaluationError, EvaluationService, EvaluationStats
from apps.evaluations import scorers
from apps.gateway.services import GatewayResponse, PolicyDenied
from apps.models_registry.models import AIModel, ModelType
from apps.organizations.models import Organization, OrganizationMembership
from apps.providers.models import AIProvider

User = get_user_model()

#: Distinguishes "no config given" from a deliberate empty one.
_UNSET = object()


def score(scorer, config, output, **context):
    case = EvaluationCase(scorer=scorer, configuration=config)
    return scorers.apply(case, output, context)


class ScorerTests(TestCase):
    def test_contains_requires_every_value_by_default(self):
        self.assertTrue(score(Scorer.CONTAINS, {"values": ["alpha", "beta"]}, "alpha and beta").passed)
        self.assertFalse(score(Scorer.CONTAINS, {"values": ["alpha", "beta"]}, "only alpha").passed)

    def test_contains_can_accept_any_value(self):
        outcome = score(
            Scorer.CONTAINS, {"values": ["alpha", "beta"], "match_all": False}, "only alpha"
        )
        self.assertTrue(outcome.passed)

    def test_contains_is_case_insensitive_unless_told_otherwise(self):
        self.assertTrue(score(Scorer.CONTAINS, {"values": ["Alpha"]}, "alpha").passed)
        self.assertFalse(
            score(Scorer.CONTAINS, {"values": ["Alpha"], "case_sensitive": True}, "alpha").passed
        )

    def test_a_contains_check_with_nothing_configured_fails(self):
        # A check that checks nothing must not report success; that is how a
        # control silently stops being a control.
        self.assertFalse(score(Scorer.CONTAINS, {}, "anything").passed)

    def test_not_contains_catches_forbidden_text(self):
        outcome = score(Scorer.NOT_CONTAINS, {"values": ["internal only"]}, "This is internal only.")
        self.assertFalse(outcome.passed)
        self.assertIn("internal only", outcome.detail)

    def test_regex_matches(self):
        self.assertTrue(score(Scorer.REGEX, {"pattern": r"POL-\d+"}, "see POL-88213").passed)
        self.assertFalse(score(Scorer.REGEX, {"pattern": r"POL-\d+"}, "no reference").passed)

    def test_a_pattern_that_does_not_compile_fails_rather_than_passes(self):
        outcome = score(Scorer.REGEX, {"pattern": "([unclosed"}, "anything")
        self.assertFalse(outcome.passed)
        self.assertIn("does not compile", outcome.detail)

    def test_refusal_detects_a_decline(self):
        self.assertTrue(score(Scorer.REFUSAL, {}, "I'm sorry, but I can't help with that.").passed)

    def test_refusal_fails_when_the_model_complied(self):
        outcome = score(Scorer.REFUSAL, {}, "Sure! Here is how you do it.")
        self.assertFalse(outcome.passed)
        self.assertIn("expected to decline", outcome.detail)

    def test_json_schema_accepts_a_fenced_block(self):
        outcome = score(
            Scorer.JSON_SCHEMA, {"required_keys": ["risk"]},
            '```json\n{"risk": "high"}\n```',
        )
        self.assertTrue(outcome.passed)

    def test_json_schema_reports_missing_keys(self):
        outcome = score(Scorer.JSON_SCHEMA, {"required_keys": ["risk", "reason"]}, '{"risk": "high"}')
        self.assertFalse(outcome.passed)
        self.assertIn("reason", outcome.detail)

    def test_no_pii_uses_the_monitors_detector(self):
        outcome = score(Scorer.NO_PII, {}, "Contact j.okafor@example.com about it.")
        self.assertFalse(outcome.passed)
        self.assertIn("email", outcome.detail.lower())

    def test_no_pii_passes_on_ordinary_business_text(self):
        self.assertTrue(score(Scorer.NO_PII, {}, "Policy POL-88213 renews in October.").passed)

    def test_max_latency(self):
        self.assertTrue(score(Scorer.MAX_LATENCY, {"milliseconds": 2000}, "x", latency_ms=800).passed)
        self.assertFalse(score(Scorer.MAX_LATENCY, {"milliseconds": 500}, "x", latency_ms=800).passed)

    def test_max_cost(self):
        self.assertTrue(
            score(Scorer.MAX_COST, {"amount": "0.01"}, "x", cost=Decimal("0.005")).passed
        )
        self.assertFalse(
            score(Scorer.MAX_COST, {"amount": "0.001"}, "x", cost=Decimal("0.005")).passed
        )

    def test_an_unknown_scorer_fails_rather_than_passes(self):
        case = EvaluationCase(scorer="not_a_real_scorer", configuration={})
        self.assertFalse(scorers.apply(case, "anything", {}).passed)

    def test_a_scorer_that_raises_fails_the_case(self):
        with patch.dict(scorers.SCORERS, {Scorer.CONTAINS: lambda *a: 1 / 0}):
            self.assertFalse(score(Scorer.CONTAINS, {"values": ["x"]}, "x").passed)


class CaseConfigurationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Eval Org")
        self.suite = EvaluationSuite.objects.create(
            organization=self.organization, name="Suite", status=LifecycleStatus.ACTIVE
        )

    def test_an_unknown_configuration_key_is_rejected_on_save(self):
        """A typo must not silently turn a check into one that always passes."""
        case = EvaluationCase(
            organization=self.organization, suite=self.suite, name="Typo",
            scorer=Scorer.CONTAINS, configuration={"value": ["alpha"]},  # 'values'
        )
        with self.assertRaises(ValidationError) as ctx:
            case.save()
        self.assertIn("value", str(ctx.exception))

    def test_a_valid_configuration_saves(self):
        case = EvaluationCase.objects.create(
            organization=self.organization, suite=self.suite, name="Good",
            scorer=Scorer.CONTAINS, configuration={"values": ["alpha"], "match_all": True},
            prompt="Say alpha.",
        )
        self.assertEqual(case.suite, self.suite)


class EvaluationFixtureMixin:
    def build_estate(self):
        self.organization = Organization.objects.create(name="Eval Org")
        self.provider = AIProvider.objects.create(
            organization=self.organization, name="Mock", adapter_key="mock",
            status=LifecycleStatus.ACTIVE, health_status=HealthStatus.HEALTHY,
        )
        self.model = AIModel.objects.create(
            organization=self.organization, provider=self.provider,
            model_name="Mock Large", model_id="mock-large", model_type=ModelType.CHAT,
            approved_for_use=True, status=LifecycleStatus.ACTIVE,
            input_cost_per_1k=Decimal("0.001"), output_cost_per_1k=Decimal("0.002"),
        )
        self.suite = EvaluationSuite.objects.create(
            organization=self.organization, name="Baseline",
            target_kind=TargetKind.MODEL, status=LifecycleStatus.ACTIVE,
            pass_threshold=80, blocking_categories=[CaseCategory.SAFETY],
        )

    def case(self, name, scorer=Scorer.CONTAINS, config=_UNSET, category=CaseCategory.QUALITY,
             weight=1, order=0):
        if config is _UNSET:
            config = {"values": ["ok"]}
        return EvaluationCase.objects.create(
            organization=self.organization, suite=self.suite, name=name, order=order,
            category=category, scorer=scorer, configuration=config,
            weight=weight, prompt=f"prompt for {name}",
        )

    def gateway_response(self, text="ok"):
        return GatewayResponse(
            output_text=text, model=self.model, attempts=[],
            input_tokens=10, output_tokens=5, cost=Decimal("0.0001"),
            currency="USD", latency_ms=120,
        )

    def run_suite(self, output="ok"):
        run = EvaluationService.create_run(self.suite, model=self.model)
        with patch("apps.gateway.services.GatewayService.complete") as complete:
            complete.return_value = self.gateway_response(output)
            return EvaluationService.execute(run)


class RunCreationTests(EvaluationFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.case("One")

    def test_a_run_records_its_case_count_up_front(self):
        run = EvaluationService.create_run(self.suite, model=self.model)
        self.assertEqual(run.total_cases, 1)
        self.assertEqual(run.status, RunStatus.QUEUED)

    def test_a_suite_with_no_cases_cannot_be_run(self):
        empty = EvaluationSuite.objects.create(
            organization=self.organization, name="Empty", status=LifecycleStatus.ACTIVE
        )
        with self.assertRaises(EvaluationError):
            EvaluationService.create_run(empty, model=self.model)

    def test_a_model_suite_needs_a_model(self):
        agent = AIAgent.objects.create(
            organization=self.organization, name="A", agent_id="a", status=LifecycleStatus.ACTIVE
        )
        with self.assertRaises(EvaluationError):
            EvaluationService.create_run(self.suite, agent=agent)

    def test_a_run_never_crosses_organizations(self):
        other = Organization.objects.create(name="Elsewhere")
        foreign_provider = AIProvider.objects.create(
            organization=other, name="Foreign", adapter_key="mock", status=LifecycleStatus.ACTIVE
        )
        foreign_model = AIModel.objects.create(
            organization=other, provider=foreign_provider, model_name="Foreign",
            model_id="foreign", model_type=ModelType.CHAT, status=LifecycleStatus.ACTIVE,
        )
        with self.assertRaises(EvaluationError):
            EvaluationService.create_run(self.suite, model=foreign_model)


class RunExecutionTests(EvaluationFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()

    def test_a_passing_suite_completes_and_passes(self):
        self.case("One")
        self.case("Two", order=1)
        run = self.run_suite(output="ok")

        self.assertEqual(run.status, RunStatus.COMPLETED)
        self.assertEqual(run.passed_cases, 2)
        self.assertEqual(run.score, Decimal("100.00"))
        self.assertTrue(run.passed)
        self.assertEqual(run.verdict, "passed")

    def test_the_score_is_weighted_not_counted(self):
        self.case("Cheap", config={"values": ["ok"]}, weight=1)
        self.case("Heavy", config={"values": ["absent"]}, weight=4, order=1)
        run = self.run_suite(output="ok")

        # 1 of 5 weight passed, not 1 of 2 cases.
        self.assertEqual(run.score, Decimal("20.00"))

    def test_a_run_below_the_bar_fails(self):
        self.case("One", config={"values": ["absent"]})
        run = self.run_suite(output="ok")

        self.assertFalse(run.passed)
        self.assertEqual(run.verdict, "failed")

    def test_a_failed_safety_case_blocks_a_run_that_scored_well(self):
        """The most dangerous kind of green tick is the one this prevents."""
        for index in range(9):
            self.case(f"Quality {index}", order=index)
        self.case(
            "Must refuse", scorer=Scorer.REFUSAL, config={},
            category=CaseCategory.SAFETY, order=99,
        )
        run = self.run_suite(output="ok")

        self.assertEqual(run.score, Decimal("90.00"))
        self.assertGreaterEqual(run.score, run.suite.pass_threshold)
        self.assertFalse(run.passed)
        self.assertEqual(run.verdict, "blocked")
        self.assertEqual(run.blocked_by_category, CaseCategory.SAFETY)

    def test_a_non_blocking_category_does_not_block(self):
        self.suite.blocking_categories = []
        self.suite.save(update_fields=["blocking_categories"])
        for index in range(9):
            self.case(f"Quality {index}", order=index)
        self.case("Must refuse", scorer=Scorer.REFUSAL, config={},
                  category=CaseCategory.SAFETY, order=99)
        run = self.run_suite(output="ok")

        self.assertTrue(run.passed)
        self.assertEqual(run.verdict, "passed")

    def test_a_gateway_refusal_is_a_failed_case_not_a_broken_run(self):
        # If production would refuse this prompt, the evaluation should say so
        # rather than route around the control to get a score.
        self.case("One")
        run = EvaluationService.create_run(self.suite, model=self.model)

        with patch("apps.gateway.services.GatewayService.complete") as complete:
            complete.side_effect = PolicyDenied("Blocked by policy.")
            run = EvaluationService.execute(run)

        self.assertEqual(run.status, RunStatus.COMPLETED)
        self.assertEqual(run.passed_cases, 0)
        result = EvaluationResult.objects.get()
        self.assertIn("refused", result.detail)
        self.assertEqual(result.error_message, "policy_denied")

    def test_results_are_written_as_the_run_progresses(self):
        """A crash halfway through must leave evidence, not nothing."""
        self.case("One")
        self.case("Two", order=1)
        seen = []

        def complete(*args, **kwargs):
            seen.append(EvaluationResult.objects.count())
            return self.gateway_response("ok")

        run = EvaluationService.create_run(self.suite, model=self.model)
        with patch("apps.gateway.services.GatewayService.complete", side_effect=complete):
            EvaluationService.execute(run)

        self.assertEqual(seen, [0, 1])

    def test_the_run_records_what_it_spent(self):
        self.case("One")
        self.case("Two", order=1)
        run = self.run_suite()
        self.assertEqual(run.total_cost, Decimal("0.0002"))

    def test_running_an_already_finished_run_is_a_no_op(self):
        self.case("One")
        run = self.run_suite()
        finished_at = run.finished_at

        again = EvaluationService.execute(run)
        self.assertEqual(again.finished_at, finished_at)
        self.assertEqual(EvaluationResult.objects.count(), 1)

    def test_the_category_breakdown_marks_blocking_categories(self):
        self.case("Quality")
        self.case("Safety", category=CaseCategory.SAFETY, order=1)
        run = self.run_suite()

        breakdown = {row["category"]: row for row in EvaluationService.category_breakdown(run)}
        self.assertTrue(breakdown[CaseCategory.SAFETY]["blocking"])
        self.assertFalse(breakdown[CaseCategory.QUALITY]["blocking"])


class EvaluationStatsTests(EvaluationFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.case("One")

    def test_approved_models_with_no_evaluation_are_counted(self):
        self.assertEqual(
            EvaluationStats(self.organization).build()["unevaluated_approved_models"], 1
        )

    def test_an_evaluated_model_stops_being_counted(self):
        self.run_suite()
        self.assertEqual(
            EvaluationStats(self.organization).build()["unevaluated_approved_models"], 0
        )


class EvaluationViewTests(EvaluationFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.case("One")

    def login(self, role=Role.GOVERNANCE_MANAGER, email="gov@example.com"):
        user = User.objects.create_user(email=email, password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=user, role=role, is_default=True
        )
        self.client.force_login(user)
        return user

    def test_the_centre_renders(self):
        self.login()
        response = self.client.get(reverse("evaluations:home"))
        self.assertContains(response, "Baseline")

    def test_the_suite_page_lists_its_cases(self):
        self.login()
        response = self.client.get(reverse("evaluations:suite", args=[self.suite.pk]))
        self.assertContains(response, "One")

    def test_starting_a_run_requires_the_approval_permission(self):
        self.login(role=Role.VIEWER, email="viewer@example.com")
        response = self.client.post(
            reverse("evaluations:start_run", args=[self.suite.pk]), {"target": str(self.model.pk)}
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(EvaluationRun.objects.exists())

    def test_a_governance_manager_can_start_a_run(self):
        self.login()
        with patch("apps.evaluations.views.StartRunView._dispatch"):
            response = self.client.post(
                reverse("evaluations:start_run", args=[self.suite.pk]),
                {"target": str(self.model.pk)},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(EvaluationRun.objects.count(), 1)

    def test_the_run_page_renders_results(self):
        self.login()
        run = self.run_suite()
        response = self.client.get(reverse("evaluations:run", args=[run.pk]))
        self.assertContains(response, "One")
        self.assertContains(response, "Passed")

    def test_progress_is_available_as_json(self):
        self.login()
        run = self.run_suite()
        payload = self.client.get(reverse("evaluations:run_progress", args=[run.pk])).json()

        self.assertTrue(payload["finished"])
        self.assertEqual(payload["progress"], 100)
        self.assertEqual(payload["verdict"], "passed")

    def test_model_output_is_withheld_from_a_viewer(self):
        # Evaluation output is model output, governed by the same permission
        # that governs prompts on the monitor.
        self.case("Fails", config={"values": ["absent"]}, order=1)
        run = self.run_suite(output="a secret answer")

        self.login(role=Role.VIEWER, email="viewer2@example.com")
        response = self.client.get(reverse("evaluations:run", args=[run.pk]))
        self.assertNotContains(response, "a secret answer")

    def test_another_tenants_run_is_not_reachable(self):
        self.login()
        other = Organization.objects.create(name="Elsewhere")
        foreign_suite = EvaluationSuite.objects.create(organization=other, name="Foreign")
        foreign_run = EvaluationRun.objects.create(
            organization=other, suite=foreign_suite, total_cases=1
        )
        response = self.client.get(reverse("evaluations:run", args=[foreign_run.pk]))
        self.assertEqual(response.status_code, 404)
