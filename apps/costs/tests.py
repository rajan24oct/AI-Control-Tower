from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.agents.models import AIAgent
from apps.applications_registry.models import AIApplication
from apps.core.constants import LifecycleStatus
from apps.costs.models import (
    Budget,
    BudgetAlert,
    BudgetPeriod,
    BudgetScope,
    DailyUsageRollup,
    ModelPricing,
    PricingVersion,
)
from apps.costs.services import (
    BudgetService,
    CostAnalyticsService,
    CostCalculationService,
    PricingService,
    RollupService,
    period_window,
)
from apps.executions.models import Execution, ExecutionStatusChoices
from apps.executions.services import ExecutionService
from apps.models_registry.models import AIModel
from apps.organizations.models import Organization, OrganizationMembership, Team
from apps.providers.models import AIProvider

User = get_user_model()


class CostFixtureMixin:
    def build_estate(self, name="Cost Org"):
        self.organization = Organization.objects.create(name=name)
        self.team = Team.objects.create(organization=self.organization, name="Platform")
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
        )
        self.agent = AIAgent.objects.create(
            organization=self.organization,
            name="Cost Agent",
            agent_id="cost-agent",
            provider=self.provider,
            primary_model=self.model,
        )
        self.application = AIApplication.objects.create(
            organization=self.organization, name="Cost App", team=self.team
        )
        self.application.agents.add(self.agent)

    def make_execution(self, cost="0", started_at=None, **kwargs):
        defaults = {
            "organization": self.organization,
            "agent": self.agent,
            "application": self.application,
            "provider": self.provider,
            "model": self.model,
            "status": ExecutionStatusChoices.COMPLETED,
            "cost": Decimal(cost),
            "started_at": started_at or timezone.now(),
        }
        defaults.update(kwargs)
        defaults.setdefault("external_id", f"exec-{Execution.objects.count() + 1}")
        return Execution.objects.create(**defaults)


class PricingVersionTests(CostFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.now = timezone.now()

        self.old = PricingVersion.objects.create(
            organization=self.organization,
            name="2025 Rates",
            effective_from=self.now - timedelta(days=400),
            effective_to=self.now - timedelta(days=100),
        )
        ModelPricing.objects.create(
            version=self.old, model=self.model,
            input_cost_per_1k=Decimal("0.010"), output_cost_per_1k=Decimal("0.030"),
        )

        self.current = PricingVersion.objects.create(
            organization=self.organization,
            name="2026 Rates",
            effective_from=self.now - timedelta(days=99),
        )
        ModelPricing.objects.create(
            version=self.current, model=self.model,
            input_cost_per_1k=Decimal("0.003"), output_cost_per_1k=Decimal("0.015"),
        )

    def test_the_current_version_is_resolved_for_now(self):
        pricing = PricingService.resolve(self.model, self.now)
        self.assertEqual(pricing.version, self.current)

    def test_a_historic_moment_resolves_the_historic_version(self):
        pricing = PricingService.resolve(self.model, self.now - timedelta(days=200))
        self.assertEqual(pricing.version, self.old)

    def test_a_moment_before_any_version_resolves_nothing(self):
        self.assertIsNone(PricingService.resolve(self.model, self.now - timedelta(days=500)))

    def test_an_inactive_version_is_ignored(self):
        self.current.status = LifecycleStatus.RETIRED
        self.current.save()
        self.assertIsNone(PricingService.resolve(self.model, self.now))

    def test_covers_respects_the_window(self):
        self.assertTrue(self.current.covers(self.now))
        self.assertFalse(self.current.covers(self.now - timedelta(days=300)))


class CostCalculationTests(CostFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.now = timezone.now()

    def test_it_falls_back_to_the_model_registry_without_a_version(self):
        breakdown = CostCalculationService.calculate(self.model, 1000, 1000, self.now)
        self.assertEqual(breakdown.amount, Decimal("0.018000"))
        self.assertEqual(breakdown.source, "model_registry")

    def test_a_pricing_version_takes_precedence(self):
        version = PricingVersion.objects.create(
            organization=self.organization,
            name="Negotiated",
            effective_from=self.now - timedelta(days=10),
        )
        ModelPricing.objects.create(
            version=version, model=self.model,
            input_cost_per_1k=Decimal("0.001"), output_cost_per_1k=Decimal("0.002"),
        )
        breakdown = CostCalculationService.calculate(self.model, 1000, 1000, self.now)

        self.assertEqual(breakdown.amount, Decimal("0.003000"))
        self.assertEqual(breakdown.source, "pricing_version")
        self.assertEqual(breakdown.pricing_version, "Negotiated")

    def test_a_historic_run_keeps_the_rates_that_applied_then(self):
        """The whole point of versioned pricing."""
        old = PricingVersion.objects.create(
            organization=self.organization, name="Old",
            effective_from=self.now - timedelta(days=100),
            effective_to=self.now - timedelta(days=30),
        )
        ModelPricing.objects.create(
            version=old, model=self.model,
            input_cost_per_1k=Decimal("0.100"), output_cost_per_1k=Decimal("0.100"),
        )
        new = PricingVersion.objects.create(
            organization=self.organization, name="New",
            effective_from=self.now - timedelta(days=29),
        )
        ModelPricing.objects.create(
            version=new, model=self.model,
            input_cost_per_1k=Decimal("0.001"), output_cost_per_1k=Decimal("0.001"),
        )

        historic = CostCalculationService.calculate(
            self.model, 1000, 0, self.now - timedelta(days=60)
        )
        recent = CostCalculationService.calculate(self.model, 1000, 0, self.now)

        self.assertEqual(historic.amount, Decimal("0.100000"))
        self.assertEqual(recent.amount, Decimal("0.001000"))

    def test_no_model_or_no_tokens_costs_nothing(self):
        self.assertEqual(CostCalculationService.calculate(None, 100, 100).amount, Decimal("0"))
        self.assertEqual(CostCalculationService.calculate(self.model, 0, 0).amount, Decimal("0"))

    def test_execution_pricing_uses_the_applicable_version(self):
        version = PricingVersion.objects.create(
            organization=self.organization, name="Applied",
            effective_from=self.now - timedelta(days=5),
        )
        ModelPricing.objects.create(
            version=version, model=self.model,
            input_cost_per_1k=Decimal("0.500"), output_cost_per_1k=Decimal("0"),
        )
        execution = ExecutionService.record(
            organization=self.organization,
            external_id="priced",
            agent="cost-agent",
            input_tokens=1000,
        )
        self.assertEqual(execution.cost, Decimal("0.500000"))
        self.assertEqual(execution.metadata["pricing_source"], "pricing_version")


class PeriodWindowTests(TestCase):
    def test_daily(self):
        start, end = period_window(BudgetPeriod.DAILY, date(2026, 5, 14))
        self.assertEqual((start, end), (date(2026, 5, 14), date(2026, 5, 15)))

    def test_weekly_starts_on_monday(self):
        start, end = period_window(BudgetPeriod.WEEKLY, date(2026, 5, 14))
        self.assertEqual(start.weekday(), 0)
        self.assertEqual((end - start).days, 7)

    def test_monthly(self):
        start, end = period_window(BudgetPeriod.MONTHLY, date(2026, 5, 14))
        self.assertEqual((start, end), (date(2026, 5, 1), date(2026, 6, 1)))

    def test_quarterly(self):
        self.assertEqual(
            period_window(BudgetPeriod.QUARTERLY, date(2026, 5, 14)),
            (date(2026, 4, 1), date(2026, 7, 1)),
        )

    def test_quarterly_rolls_into_the_next_year(self):
        self.assertEqual(
            period_window(BudgetPeriod.QUARTERLY, date(2026, 11, 2)),
            (date(2026, 10, 1), date(2027, 1, 1)),
        )

    def test_annual(self):
        self.assertEqual(
            period_window(BudgetPeriod.ANNUAL, date(2026, 5, 14)),
            (date(2026, 1, 1), date(2027, 1, 1)),
        )


class BudgetScopingTests(CostFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.other_agent = AIAgent.objects.create(
            organization=self.organization, name="Other", agent_id="other",
            provider=self.provider, primary_model=self.model,
        )
        self.make_execution(cost="10.00")
        self.make_execution(cost="5.00", agent=self.other_agent, application=None)

    def _budget(self, scope, amount="100", **kwargs):
        return Budget.objects.create(
            organization=self.organization,
            name=f"{scope}-budget-{Budget.objects.count()}",
            scope=scope,
            amount=Decimal(amount),
            **kwargs,
        )

    def test_organization_scope_covers_everything(self):
        status = BudgetService.status_for(self._budget(BudgetScope.ORGANIZATION))
        self.assertEqual(status["spend"], Decimal("15.00"))

    def test_agent_scope_covers_only_that_agent(self):
        status = BudgetService.status_for(self._budget(BudgetScope.AGENT, agent=self.agent))
        self.assertEqual(status["spend"], Decimal("10.00"))

    def test_application_scope(self):
        status = BudgetService.status_for(
            self._budget(BudgetScope.APPLICATION, application=self.application)
        )
        self.assertEqual(status["spend"], Decimal("10.00"))

    def test_team_scope_resolves_through_the_application(self):
        status = BudgetService.status_for(self._budget(BudgetScope.TEAM, team=self.team))
        self.assertEqual(status["spend"], Decimal("10.00"))

    def test_a_scoped_budget_with_no_target_matches_nothing(self):
        status = BudgetService.status_for(self._budget(BudgetScope.AGENT))
        self.assertEqual(status["spend"], Decimal("0"))

    def test_spend_outside_the_period_is_excluded(self):
        self.make_execution(cost="99.00", started_at=timezone.now() - timedelta(days=90))
        status = BudgetService.status_for(self._budget(BudgetScope.ORGANIZATION))
        self.assertEqual(status["spend"], Decimal("15.00"))


class BudgetAlertTests(CostFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.budget = Budget.objects.create(
            organization=self.organization,
            name="Monthly",
            scope=BudgetScope.ORGANIZATION,
            amount=Decimal("100.00"),
            alert_thresholds=[50, 75, 90, 100],
            # Threshold behaviour only; forecast alerts are tested separately.
            forecast_alerts=False,
        )

    def test_default_thresholds_are_applied(self):
        budget = Budget.objects.create(
            organization=self.organization, name="Defaults", amount=Decimal("10")
        )
        self.assertEqual(budget.thresholds, [50, 75, 90, 100])

    def test_no_alert_below_the_first_threshold(self):
        self.make_execution(cost="10.00")
        self.assertEqual(BudgetService.evaluate(self.budget), [])

    def test_crossing_thresholds_raises_alerts(self):
        self.make_execution(cost="80.00")
        raised = BudgetService.evaluate(self.budget)
        self.assertEqual(sorted(a.threshold for a in raised), [50, 75])

    def test_alerts_are_not_repeated_in_the_same_period(self):
        self.make_execution(cost="80.00")
        BudgetService.evaluate(self.budget)
        self.assertEqual(BudgetService.evaluate(self.budget), [])
        self.assertEqual(BudgetAlert.objects.count(), 2)

    def test_further_spend_raises_only_the_new_threshold(self):
        self.make_execution(cost="80.00")
        BudgetService.evaluate(self.budget)
        self.make_execution(cost="25.00")

        raised = BudgetService.evaluate(self.budget)
        self.assertEqual(sorted(a.threshold for a in raised), [90, 100])

    def test_status_reports_over_budget(self):
        self.make_execution(cost="150.00")
        status = BudgetService.status_for(self.budget)
        self.assertTrue(status["is_over"])
        self.assertEqual(status["remaining"], Decimal("-50.00"))

    def test_alert_severity_scales_with_the_threshold(self):
        self.make_execution(cost="120.00")
        alerts = {a.threshold: a for a in BudgetService.evaluate(self.budget)}
        self.assertEqual(alerts[100].severity, "critical")
        self.assertEqual(alerts[90].severity, "high")
        self.assertEqual(alerts[50].severity, "medium")


class CostAnalyticsTests(CostFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        for _ in range(4):
            self.make_execution(cost="2.50", input_tokens=1000, output_tokens=500)
        self.make_execution(cost="1.00", status=ExecutionStatusChoices.FAILED)

    def test_summary_totals(self):
        summary = CostAnalyticsService(self.organization).summary()
        self.assertEqual(summary["today"], Decimal("11.00"))
        self.assertEqual(summary["window_runs"], 5)

    def test_average_per_successful_excludes_failures(self):
        summary = CostAnalyticsService(self.organization).summary()
        self.assertEqual(summary["avg_per_successful"], Decimal("2.50"))

    def test_breakdown_by_provider(self):
        rows = CostAnalyticsService(self.organization).by("provider", "provider__name")
        self.assertEqual(rows[0]["label"], "Mock Provider")
        self.assertEqual(rows[0]["cost"], Decimal("11.00"))

    def test_breakdown_by_team(self):
        rows = CostAnalyticsService(self.organization).by_team()
        self.assertEqual(rows[0]["label"], "Platform")

    def test_daily_series_covers_the_whole_window(self):
        series = CostAnalyticsService(self.organization, days=14).daily_series()
        self.assertEqual(len(series["labels"]), 14)
        self.assertEqual(len(series["cost"]), 14)

    def test_projection_is_zero_without_spend(self):
        empty = Organization.objects.create(name="Empty Co")
        self.assertEqual(CostAnalyticsService(empty).summary()["projected_month"], Decimal("0"))


class RollupTests(CostFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.make_execution(cost="3.00", input_tokens=100, output_tokens=50)
        self.make_execution(cost="2.00", input_tokens=200, output_tokens=100)

    def test_a_day_is_aggregated(self):
        RollupService.rebuild_day(self.organization, timezone.now().date())
        rollup = DailyUsageRollup.objects.get()

        self.assertEqual(rollup.executions, 2)
        self.assertEqual(rollup.total_tokens, 450)
        self.assertEqual(rollup.cost, Decimal("5.00"))

    def test_rebuilding_replaces_rather_than_duplicates(self):
        today = timezone.now().date()
        RollupService.rebuild_day(self.organization, today)
        RollupService.rebuild_day(self.organization, today)
        self.assertEqual(DailyUsageRollup.objects.count(), 1)


class CostViewTests(CostFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate("Cost View Org")
        self.user = User.objects.create_user(email="fin@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.user, role=Role.ORG_ADMIN, is_default=True
        )
        self.client.force_login(self.user)
        self.make_execution(cost="7.50")

        self.budget = Budget.objects.create(
            organization=self.organization, name="Ops", amount=Decimal("10.00")
        )
        BudgetService.evaluate(self.budget)

    def test_the_cost_dashboard_renders(self):
        response = self.client.get(reverse("costs:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary"]["today"], Decimal("7.50"))

    def test_the_budgets_page_shows_utilisation(self):
        response = self.client.get(reverse("costs:budgets"))
        self.assertEqual(response.status_code, 200)
        state = response.context["budget_states"][0]
        self.assertEqual(state["percentage"], Decimal("75"))

    def test_an_alert_can_be_acknowledged(self):
        alert = BudgetAlert.objects.first()
        self.client.post(reverse("costs:acknowledge_alert", args=[alert.pk]))
        alert.refresh_from_db()
        self.assertTrue(alert.is_acknowledged)

    def test_a_viewer_cannot_acknowledge(self):
        viewer = User.objects.create_user(email="v@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=viewer, role=Role.VIEWER, is_default=True
        )
        self.client.force_login(viewer)

        alert = BudgetAlert.objects.first()
        self.client.post(reverse("costs:acknowledge_alert", args=[alert.pk]))
        alert.refresh_from_db()
        self.assertFalse(alert.is_acknowledged)

    def test_costs_are_scoped_to_the_active_organization(self):
        other = Organization.objects.create(name="Rival Costs")
        provider = AIProvider.objects.create(organization=other, name="Rival Provider")
        Execution.objects.create(
            organization=other, external_id="rival", provider=provider, cost=Decimal("999")
        )
        response = self.client.get(reverse("costs:dashboard"))
        self.assertEqual(response.context["summary"]["today"], Decimal("7.50"))
