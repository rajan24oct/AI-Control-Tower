"""Tests for model routing.

Routing decides what runs and what it costs, so the tests are about the two
things that make a selection defensible: that hard rules are actually hard, and
that the ranking reflects what this estate has observed rather than what a
datasheet claims.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.agents.models import AIAgent
from apps.core.constants import Environment, HealthStatus, LifecycleStatus
from apps.executions.models import Execution, ExecutionStatusChoices
from apps.models_registry.models import AIModel, ModelType
from apps.organizations.models import Organization, OrganizationMembership
from apps.providers.models import AIProvider
from apps.routing.models import RouteOutcome, RoutingDecision, RoutingPolicy, RoutingStrategy
from apps.routing.services import CONFIDENCE_FLOOR, RoutingRequest, RoutingService, RoutingStats

User = get_user_model()


class RoutingFixtureMixin:
    def build_estate(self):
        self.organization = Organization.objects.create(name="Routing Org")
        self.cheap_provider = self._provider("Cheap Co")
        self.fast_provider = self._provider("Fast Co")

        self.cheap = self._model(
            self.cheap_provider, "Cheap Model", "cheap-1",
            input_cost=Decimal("0.0005"), output_cost=Decimal("0.001"),
        )
        self.premium = self._model(
            self.fast_provider, "Premium Model", "premium-1",
            input_cost=Decimal("0.010"), output_cost=Decimal("0.030"),
        )
        self.agent = AIAgent.objects.create(
            organization=self.organization, name="Router Agent", agent_id="router-agent",
            provider=self.cheap_provider, primary_model=self.cheap,
            environment=Environment.PRODUCTION, status=LifecycleStatus.ACTIVE,
        )

    def _provider(self, name, health=HealthStatus.HEALTHY):
        return AIProvider.objects.create(
            organization=self.organization, name=name, adapter_key="mock",
            status=LifecycleStatus.ACTIVE, health_status=health,
        )

    def _model(self, provider, name, model_id, *, input_cost=Decimal("0.001"),
               output_cost=Decimal("0.002"), approved=True, **kwargs):
        return AIModel.objects.create(
            organization=self.organization, provider=provider,
            model_name=name, model_id=model_id, model_type=ModelType.CHAT,
            input_cost_per_1k=input_cost, output_cost_per_1k=output_cost,
            approved_for_use=approved, status=LifecycleStatus.ACTIVE,
            context_window=kwargs.pop("context_window", 128000), **kwargs,
        )

    def _policy(self, **kwargs):
        defaults = {
            "organization": self.organization,
            "name": kwargs.pop("name", "Chat"),
            "model_type": ModelType.CHAT,
            "status": LifecycleStatus.ACTIVE,
        }
        defaults.update(kwargs)
        return RoutingPolicy.objects.create(**defaults)

    def plan(self, **kwargs):
        return RoutingService.plan(self.organization, RoutingRequest(**kwargs))

    def _runs(self, model, *, total, failed=0, latency=1000):
        for index in range(total):
            Execution.objects.create(
                organization=self.organization,
                external_id=f"{model.model_id}-{index}-{timezone.now().timestamp()}",
                model=model, provider=model.provider,
                status=(
                    ExecutionStatusChoices.FAILED if index < failed
                    else ExecutionStatusChoices.COMPLETED
                ),
                latency_ms=latency,
                started_at=timezone.now() - timedelta(hours=1),
            )


class AdmissibilityTests(RoutingFixtureMixin, TestCase):
    """Hard rules. No score rescues a candidate that fails one."""

    def setUp(self):
        self.build_estate()

    def test_an_unapproved_model_is_excluded_with_a_reason(self):
        self.premium.approved_for_use = False
        self.premium.save(update_fields=["approved_for_use"])
        self._policy(require_approved_models=True)

        plan = self.plan()
        self.assertNotIn(self.premium, [c.model for c in plan.candidates])
        self.assertIn("Not approved", "".join(e.reason for e in plan.exclusions))

    def test_an_unhealthy_provider_is_never_offered_even_as_a_fallback(self):
        # A fallback that is known to be down is not a fallback; it is a way of
        # spending the caller's remaining patience.
        self.fast_provider.health_status = HealthStatus.UNHEALTHY
        self.fast_provider.save(update_fields=["health_status"])
        self._policy()

        plan = self.plan()
        self.assertEqual([c.model for c in plan.candidates], [self.cheap])

    def test_a_degraded_provider_is_kept_by_default(self):
        self.fast_provider.health_status = HealthStatus.DEGRADED
        self.fast_provider.save(update_fields=["health_status"])
        self._policy(allow_degraded_providers=True)

        self.assertIn(self.premium, [c.model for c in self.plan().candidates])

    def test_a_degraded_provider_can_be_excluded_by_policy(self):
        self.fast_provider.health_status = HealthStatus.DEGRADED
        self.fast_provider.save(update_fields=["health_status"])
        self._policy(allow_degraded_providers=False)

        self.assertNotIn(self.premium, [c.model for c in self.plan().candidates])

    def test_a_blocked_provider_is_excluded(self):
        policy = self._policy()
        policy.blocked_providers.add(self.fast_provider)

        self.assertNotIn(self.premium, [c.model for c in self.plan().candidates])

    def test_an_allow_list_excludes_everything_else(self):
        policy = self._policy()
        policy.allowed_providers.add(self.cheap_provider)

        self.assertEqual([c.model for c in self.plan().candidates], [self.cheap])

    def test_a_cost_ceiling_excludes_expensive_models(self):
        self._policy(max_input_cost_per_1k=Decimal("0.001"))
        self.assertEqual([c.model for c in self.plan().candidates], [self.cheap])

    def test_a_missing_capability_excludes_a_model(self):
        self.cheap.capabilities = ["supports_chat"]
        self.cheap.save(update_fields=["capabilities"])
        self.premium.capabilities = ["supports_chat", "supports_vision"]
        self.premium.save(update_fields=["capabilities"])
        self._policy()

        plan = self.plan(required_capabilities=("supports_vision",))
        self.assertEqual([c.model for c in plan.candidates], [self.premium])

    def test_a_context_window_that_is_too_small_excludes_a_model(self):
        self.cheap.context_window = 8000
        self.cheap.save(update_fields=["context_window"])
        self._policy(min_context_window=32000)

        self.assertEqual([c.model for c in self.plan().candidates], [self.premium])

    def test_a_model_already_tried_is_not_offered_again(self):
        self._policy()
        plan = self.plan(exclude_model_ids=(str(self.cheap.pk),))
        self.assertEqual([c.model for c in plan.candidates], [self.premium])

    def test_no_eligible_models_produces_an_empty_plan_with_reasons(self):
        AIModel.objects.for_organization(self.organization).update(approved_for_use=False)
        self._policy(require_approved_models=True)

        plan = self.plan()
        self.assertFalse(plan.has_route)
        self.assertEqual(len(plan.exclusions), 2)
        self.assertIn("excluded", plan.reason)


class StrategyTests(RoutingFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()

    def test_the_cost_strategy_prefers_the_cheaper_model(self):
        self._policy(strategy=RoutingStrategy.COST)
        self.assertEqual(self.plan().primary, self.cheap)

    def test_the_latency_strategy_prefers_the_faster_model(self):
        self._runs(self.cheap, total=CONFIDENCE_FLOOR, latency=5000)
        self._runs(self.premium, total=CONFIDENCE_FLOOR, latency=300)
        self._policy(strategy=RoutingStrategy.LATENCY)

        self.assertEqual(self.plan().primary, self.premium)

    def test_the_reliability_strategy_avoids_a_model_that_keeps_failing(self):
        # The cheap model is cheaper on every strategy; only its observed
        # failure rate should be able to cost it the top slot.
        self._runs(self.cheap, total=CONFIDENCE_FLOOR, failed=CONFIDENCE_FLOOR // 2)
        self._runs(self.premium, total=CONFIDENCE_FLOOR, failed=0)
        self._policy(strategy=RoutingStrategy.RELIABILITY)

        self.assertEqual(self.plan().primary, self.premium)

    def test_a_failing_model_is_still_offered_as_a_fallback(self):
        self._runs(self.cheap, total=CONFIDENCE_FLOOR, failed=CONFIDENCE_FLOOR // 2)
        self._runs(self.premium, total=CONFIDENCE_FLOOR)
        self._policy(strategy=RoutingStrategy.RELIABILITY)

        self.assertEqual([c.model for c in self.plan().candidates], [self.premium, self.cheap])

    def test_a_handful_of_runs_does_not_decide_the_ranking(self):
        """One unlucky run must not bury a model."""
        self._runs(self.premium, total=2, failed=2)
        self._policy(strategy=RoutingStrategy.RELIABILITY)

        premium = next(c for c in self.plan().candidates if c.model == self.premium)
        # Blended toward neutral rather than scored at zero.
        self.assertGreater(premium.signals.reliability, 0.4)

    def test_health_discounts_a_degraded_provider_on_every_strategy(self):
        self.cheap_provider.health_status = HealthStatus.DEGRADED
        self.cheap_provider.save(update_fields=["health_status"])
        self._policy(strategy=RoutingStrategy.COST)

        cheap = next(c for c in self.plan().candidates if c.model == self.cheap)
        self.assertLess(cheap.signals.health, 1.0)

    def test_the_chain_is_capped_by_the_policy(self):
        self._model(self.cheap_provider, "Third", "third-1")
        self._policy(fallback_depth=1)

        self.assertEqual(len(self.plan().candidates), 2)

    def test_every_candidate_carries_a_reason(self):
        self._policy()
        for candidate in self.plan().candidates:
            self.assertTrue(candidate.reason)


class PolicySelectionTests(RoutingFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()

    def test_an_agent_scoped_policy_beats_an_estate_wide_one(self):
        self._policy(name="Everyone", priority=10)
        specific = self._policy(name="Just this agent", priority=100)
        specific.agents.add(self.agent)

        plan = self.plan(agent=self.agent)
        self.assertEqual(plan.policy, specific)

    def test_an_agent_scoped_policy_does_not_leak_to_other_agents(self):
        general = self._policy(name="Everyone")
        specific = self._policy(name="Just this agent", priority=1)
        specific.agents.add(self.agent)

        other = AIAgent.objects.create(
            organization=self.organization, name="Other", agent_id="other-agent",
            status=LifecycleStatus.ACTIVE,
        )
        self.assertEqual(self.plan(agent=other).policy, general)

    def test_an_environment_scoped_policy_is_skipped_elsewhere(self):
        self._policy(name="Prod only", environments=[Environment.PRODUCTION])
        plan = self.plan(environment=Environment.DEVELOPMENT)
        self.assertIsNone(plan.policy)

    def test_routing_works_with_no_policy_at_all(self):
        plan = self.plan()
        self.assertIsNone(plan.policy)
        self.assertTrue(plan.has_route)

    def test_a_retired_policy_is_ignored(self):
        self._policy(status=LifecycleStatus.RETIRED)
        self.assertIsNone(self.plan().policy)

    def test_routing_never_crosses_organizations(self):
        other = Organization.objects.create(name="Elsewhere")
        foreign_provider = AIProvider.objects.create(
            organization=other, name="Foreign", adapter_key="mock",
            status=LifecycleStatus.ACTIVE, health_status=HealthStatus.HEALTHY,
        )
        AIModel.objects.create(
            organization=other, provider=foreign_provider, model_name="Foreign Model",
            model_id="foreign-1", model_type=ModelType.CHAT, approved_for_use=True,
            status=LifecycleStatus.ACTIVE,
        )
        names = {c.model.model_name for c in self.plan().candidates}
        self.assertNotIn("Foreign Model", names)


class DecisionRecordTests(RoutingFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self._policy()

    def test_deciding_records_the_plan_that_was_sent(self):
        plan, decision = RoutingService.decide(self.organization, RoutingRequest())

        self.assertEqual(decision.selected_model, plan.primary)
        self.assertEqual(len(decision.chain), len(plan.candidates))
        self.assertEqual(decision.outcome, RouteOutcome.PENDING)

    def test_a_plan_with_no_route_is_recorded_as_exhausted(self):
        AIModel.objects.for_organization(self.organization).update(approved_for_use=False)
        _, decision = RoutingService.decide(self.organization, RoutingRequest())

        self.assertEqual(decision.outcome, RouteOutcome.EXHAUSTED)
        self.assertFalse(decision.has_route)
        self.assertTrue(decision.excluded)

    def test_reporting_an_outcome_closes_the_loop(self):
        _, decision = RoutingService.decide(self.organization, RoutingRequest())
        RoutingService.report_outcome(decision, model=self.premium, attempt=2)

        decision.refresh_from_db()
        self.assertEqual(decision.served_model, self.premium)
        self.assertTrue(decision.used_fallback)
        self.assertIsNotNone(decision.outcome_reported_at)

    def test_the_recorded_chain_is_not_re_derived_later(self):
        """Health moves; a decision log that re-derives answers a different question."""
        _, decision = RoutingService.decide(self.organization, RoutingRequest())
        recorded = list(decision.chain)

        self.cheap_provider.health_status = HealthStatus.UNHEALTHY
        self.cheap_provider.save(update_fields=["health_status"])

        decision.refresh_from_db()
        self.assertEqual(decision.chain, recorded)


class RoutingStatsTests(RoutingFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self._policy()

    def test_the_fallback_rate_counts_only_served_requests(self):
        for attempt in (1, 1, 2):
            _, decision = RoutingService.decide(self.organization, RoutingRequest())
            RoutingService.report_outcome(decision, model=self.cheap, attempt=attempt)

        stats = RoutingStats(self.organization).build()
        self.assertEqual(stats["served"], 3)
        self.assertEqual(stats["fell_back"], 1)
        self.assertAlmostEqual(stats["fallback_rate"], 1 / 3)

    def test_no_served_requests_is_a_zero_rate_not_a_crash(self):
        self.assertEqual(RoutingStats(self.organization).build()["fallback_rate"], 0.0)


class RoutingViewTests(RoutingFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.policy = self._policy()
        user = User.objects.create_user(email="ops@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=user, role=Role.PLATFORM_ADMIN, is_default=True
        )
        self.client.force_login(user)

    def test_the_policy_list_renders(self):
        response = self.client.get(reverse("routing:list"))
        self.assertContains(response, self.policy.name)

    def test_the_detail_page_shows_the_live_plan(self):
        response = self.client.get(reverse("routing:detail", args=[self.policy.pk]))
        self.assertContains(response, "What this policy would choose right now")
        self.assertContains(response, self.cheap.model_name)

    def test_the_simulator_renders_a_plan(self):
        response = self.client.get(reverse("routing:simulator"))
        self.assertContains(response, self.premium.model_name)

    def test_the_simulator_explains_exclusions(self):
        self.premium.approved_for_use = False
        self.premium.save(update_fields=["approved_for_use"])

        response = self.client.get(reverse("routing:simulator"))
        self.assertContains(response, "Not approved for use")

    def test_the_decision_list_renders(self):
        RoutingService.decide(self.organization, RoutingRequest())
        response = self.client.get(reverse("routing:decisions"))
        self.assertEqual(response.status_code, 200)

    def test_another_tenants_policy_is_not_reachable(self):
        other = Organization.objects.create(name="Elsewhere")
        foreign = RoutingPolicy.objects.create(
            organization=other, name="Foreign", model_type=ModelType.CHAT
        )
        response = self.client.get(reverse("routing:detail", args=[foreign.pk]))
        self.assertEqual(response.status_code, 404)
