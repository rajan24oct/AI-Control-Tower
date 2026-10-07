"""AI FinOps: attribution, forecasts, developer tools, alert rules, the cost
explorer and optimization recommendations."""

import io
import json
import os
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.audit.models import AuditLog
from apps.core.constants import RiskLevel
from apps.costs.alerting import CostAlertService
from apps.costs.devtools import DevToolCostService, DevToolSyncService
from apps.costs.explorer import CostExplorerService
from apps.costs.forms import BudgetForm
from apps.costs.models import (
    AlertMetric,
    AlertWindow,
    Budget,
    BudgetAlert,
    BudgetAlertKind,
    BudgetScope,
    CostAlertEvent,
    CostAlertRule,
    CostRecommendation,
    CostSource,
    DevToolSeat,
    DevToolSubscription,
    DevToolUsage,
    RecommendationKind,
    RecommendationStatus,
    SeatPlan,
)
from apps.costs.optimization import OptimizationService
from apps.costs.services import BudgetHealth, BudgetService
from apps.costs.tests import CostFixtureMixin
from apps.executions.models import ExecutionStatusChoices
from apps.models_registry.models import AIModel
from apps.notifications.models import Notification, NotificationCategory
from apps.organizations.models import OrganizationMembership, Team, TeamMembership
from integrations.base.config import AdapterConfig
from integrations.base.exceptions import AdapterConfigurationError
from integrations.devtools.github_copilot import GitHubCopilotAdapter, UsageCallLimitReached

User = get_user_model()


def at(day: date, hour: int = 12):
    return timezone.make_aware(datetime.combine(day, time(hour)))


class FinOpsFixtureMixin(CostFixtureMixin):
    def build_finops(self):
        self.build_estate("FinOps Org")
        self.team.department = "Operations"
        self.team.save()
        self.application.department = "Claims"
        self.application.save()
        self.manager = self.member("manager@example.com", Role.ORG_ADMIN, first_name="Mona", last_name="Ng")

    def member(self, email, role=Role.VIEWER, team=None, **names):
        user = User.objects.create_user(email=email, password="pw-12345", **names)
        OrganizationMembership.objects.create(organization=self.organization, user=user, role=role, is_default=True)
        if team is not None:
            TeamMembership.objects.create(team=team, user=user)
        return user

    def subscription(self, **kwargs):
        defaults = {"organization": self.organization, "name": "Copilot", "account": "acme",
                    "adapter_key": "mock_github_copilot"}
        defaults.update(kwargs)
        return DevToolSubscription.objects.create(**defaults)

    def seat(self, subscription, login, plan=SeatPlan.BUSINESS, assigned_on=date(2026, 1, 1), **kwargs):
        return DevToolSeat.objects.create(
            organization=self.organization, subscription=subscription, login=login, plan=plan,
            assigned_on=assigned_on, **kwargs,
        )

    def usage(self, seat, day, requests):
        return DevToolUsage.objects.create(
            organization=self.organization, seat=seat, date=day, premium_requests=Decimal(requests)
        )


# -- Attribution and budget scopes ------------------------------------------------------


class ScopeAttributionTests(FinOpsFixtureMixin, TestCase):
    ON = date(2026, 9, 20)

    def setUp(self):
        self.build_finops()
        self.make_execution(cost="4.00", started_at=at(date(2026, 9, 10)))
        # A run that served no application: attributed through its agent's team.
        self.agent.team = self.team
        self.agent.save()
        self.make_execution(cost="1.00", application=None, started_at=at(date(2026, 9, 11)))

    def budget(self, **kwargs):
        defaults = {"organization": self.organization, "name": "B", "amount": Decimal("100"), "forecast_alerts": False}
        defaults.update(kwargs)
        return Budget.objects.create(**defaults)

    def test_a_department_counts_its_applications_first(self):
        claims = self.budget(name="Claims", scope=BudgetScope.DEPARTMENT, department="claims")
        operations = self.budget(name="Ops", scope=BudgetScope.DEPARTMENT, department="Operations")
        self.assertEqual(BudgetService.status_for(claims, self.ON)["spend"], Decimal("4.00"))
        # Only the run with no application falls back to the agent's team department.
        self.assertEqual(BudgetService.status_for(operations, self.ON)["spend"], Decimal("1.00"))

    def test_a_department_budget_without_a_department_counts_nothing(self):
        budget = self.budget(scope=BudgetScope.DEPARTMENT, department="")
        self.assertEqual(BudgetService.status_for(budget, self.ON)["spend"], Decimal("0"))

    def test_an_individual_budget_counts_that_persons_runs(self):
        person = self.member("dev@example.com")
        self.make_execution(cost="2.50", user=person, started_at=at(date(2026, 9, 12)))
        budget = self.budget(scope=BudgetScope.USER, user=person)
        self.assertEqual(BudgetService.status_for(budget, self.ON)["spend"], Decimal("2.50"))

    def test_the_gateway_never_holds_individual_or_developer_tool_budgets(self):
        person = self.member("dev@example.com")
        self.assertFalse(self.budget(name="P", scope=BudgetScope.USER, user=person).applies_to_agent(self.agent))
        self.assertFalse(self.budget(name="D", cost_source=CostSource.DEVELOPER_TOOLS).applies_to_agent(self.agent))
        self.assertTrue(
            self.budget(name="Dept", scope=BudgetScope.DEPARTMENT, department="Operations").applies_to_agent(self.agent)
        )


# -- Forecasts and health -----------------------------------------------------------------


class ForecastTests(FinOpsFixtureMixin, TestCase):
    def setUp(self):
        self.build_finops()
        self.budget = Budget.objects.create(
            organization=self.organization, name="Monthly", amount=Decimal("100"), owner=self.manager,
            alert_thresholds=[50, 75, 90, 100],
        )

    def test_spend_on_pace_to_pass_the_limit_is_at_risk(self):
        # 40 spent in the first 10 days of a 30-day month projects to 120.
        self.make_execution(cost="40", started_at=at(date(2026, 9, 5)))
        state = BudgetService.status_for(self.budget, date(2026, 9, 10))
        self.assertEqual(state["forecast"], Decimal("120.00"))
        self.assertEqual(state["health"], BudgetHealth.AT_RISK)

    def test_a_forecast_alert_is_raised_once_per_period(self):
        self.make_execution(cost="40", started_at=at(date(2026, 9, 5)))
        raised = BudgetService.evaluate(self.budget, date(2026, 9, 10))
        self.assertEqual([a.kind for a in raised], [BudgetAlertKind.FORECAST])
        self.assertEqual(BudgetService.evaluate(self.budget, date(2026, 9, 11)), [])
        self.assertEqual(BudgetAlert.objects.filter(kind=BudgetAlertKind.FORECAST).count(), 1)

    def test_no_forecast_this_early_in_the_period(self):
        self.make_execution(cost="20", started_at=at(date(2026, 9, 1)))
        state = BudgetService.status_for(self.budget, date(2026, 9, 1))
        self.assertFalse(state["forecast_ready"])
        self.assertEqual(BudgetService.evaluate(self.budget, date(2026, 9, 1)), [])

    def test_an_over_budget_period_raises_thresholds_not_a_forecast(self):
        self.make_execution(cost="110", started_at=at(date(2026, 9, 5)))
        raised = BudgetService.evaluate(self.budget, date(2026, 9, 10))
        self.assertTrue(all(a.kind == BudgetAlertKind.THRESHOLD for a in raised))
        self.assertEqual(BudgetService.status_for(self.budget, date(2026, 9, 10))["health"], BudgetHealth.OVER)

    def test_a_forecast_is_not_cost_drift_but_overspend_is(self):
        from apps.controlplane.services import ControlPlane

        budget = Budget.objects.create(
            organization=self.organization, name="Agent", scope=BudgetScope.AGENT, agent=self.agent,
            amount=Decimal("100"),
        )
        self.make_execution(cost="40", started_at=at(date(2026, 9, 5)))
        BudgetService.evaluate(budget, date(2026, 9, 10))
        self.assertTrue(BudgetAlert.objects.filter(budget=budget, kind=BudgetAlertKind.FORECAST).exists())
        self.assertEqual(ControlPlane.observe_budgets(self.organization), 0)

        self.make_execution(cost="70", started_at=at(date(2026, 9, 11)))
        BudgetService.evaluate(budget, date(2026, 9, 12))
        self.assertEqual(ControlPlane.observe_budgets(self.organization), 1)

    def test_budget_alerts_reach_the_owner_team_lead_and_extra_recipients(self):
        lead = self.member("lead@example.com")
        extra = self.member("finance@example.com")
        self.team.lead = lead
        self.team.save()
        budget = Budget.objects.create(
            organization=self.organization, name="Team", scope=BudgetScope.TEAM, team=self.team,
            amount=Decimal("10"), owner=self.manager, notify_team_lead=True, forecast_alerts=False,
        )
        budget.extra_recipients.add(extra)
        self.make_execution(cost="6", started_at=at(date(2026, 9, 5)))
        BudgetService.evaluate(budget, date(2026, 9, 10))
        told = set(Notification.objects.filter(category=NotificationCategory.BUDGET_ALERT)
                   .values_list("recipient__email", flat=True))
        self.assertEqual(told, {"manager@example.com", "lead@example.com", "finance@example.com"})


# -- Developer tools ---------------------------------------------------------------------


class DevToolCostTests(FinOpsFixtureMixin, TestCase):
    def setUp(self):
        self.build_finops()
        self.sub = self.subscription()
        self.seat_ = self.seat(self.sub, "octocat")

    def test_the_licence_is_prorated_by_day(self):
        cost = DevToolCostService.costs(DevToolSeat.objects.all(), date(2026, 9, 1), date(2026, 9, 11))[0]
        # 19 a month = 228 a year, over 10 of 365 days.
        self.assertEqual(cost.license, Decimal("6.25"))

    def test_premium_requests_cost_only_past_the_allowance(self):
        self.usage(self.seat_, date(2026, 9, 2), 250)
        self.usage(self.seat_, date(2026, 9, 3), 100)  # crosses the 300 allowance by 50
        cost = DevToolCostService.costs(DevToolSeat.objects.all(), date(2026, 9, 1), date(2026, 10, 1))[0]
        self.assertEqual(cost.overage_requests, Decimal("50"))
        self.assertEqual(cost.premium_cost, Decimal("2.00"))

    def test_a_window_is_charged_only_for_overage_inside_it(self):
        self.usage(self.seat_, date(2026, 9, 2), 290)  # before the window, within allowance
        self.usage(self.seat_, date(2026, 9, 20), 30)  # 20 of these pass the 300 allowance
        cost = DevToolCostService.costs(DevToolSeat.objects.all(), date(2026, 9, 15), date(2026, 10, 1))[0]
        self.assertEqual((cost.premium_requests, cost.overage_requests), (Decimal("30"), Decimal("20")))

    def test_the_allowance_resets_each_month(self):
        self.usage(self.seat_, date(2026, 8, 30), 400)
        self.usage(self.seat_, date(2026, 9, 2), 100)
        cost = DevToolCostService.costs(DevToolSeat.objects.all(), date(2026, 9, 1), date(2026, 10, 1))[0]
        self.assertEqual(cost.overage_requests, Decimal("0"))

    def test_a_developer_tool_budget_counts_seats_in_its_team(self):
        self.seat_.team = self.team
        self.seat_.save()
        self.seat(self.sub, "other")  # no team: not counted
        budget = Budget.objects.create(
            organization=self.organization, name="Copilot", scope=BudgetScope.TEAM, team=self.team,
            cost_source=CostSource.DEVELOPER_TOOLS, amount=Decimal("100"),
        )
        state = BudgetService.status_for(budget, date(2026, 9, 10))
        self.assertEqual(state["runtime_spend"], Decimal("0"))
        self.assertEqual(state["devtools_spend"], Decimal("6.25"))


class DevToolSyncTests(FinOpsFixtureMixin, TestCase):
    def setUp(self):
        self.build_finops()
        self.team.slug = "platform"
        self.team.save()
        self.dev = self.member("dev@example.com")
        self.sub = self.subscription(options={"seats": [
            {"login": "devcat", "email": "dev@example.com", "plan": "enterprise", "profile": "heavy"},
            {"login": "teamcat", "github_team": "platform", "profile": "regular"},
            {"login": "idlecat", "profile": "idle", "idle_days": 45},
        ]})

    def test_seats_and_usage_are_imported_and_attributed(self):
        result = DevToolSyncService.sync(self.sub, days=10)
        self.assertTrue(result.ok, result.message)
        seats = {seat.login: seat for seat in DevToolSeat.objects.all()}
        self.assertEqual(seats["devcat"].user, self.dev)
        self.assertEqual(seats["devcat"].plan, SeatPlan.ENTERPRISE)
        self.assertEqual(seats["teamcat"].team, self.team)
        self.assertGreater(DevToolUsage.objects.filter(seat=seats["devcat"]).count(), 0)
        self.assertFalse(DevToolUsage.objects.filter(seat=seats["idlecat"]).exists())

    def test_a_seat_gone_from_github_is_marked_removed(self):
        DevToolSyncService.sync(self.sub, days=2)
        self.sub.options = {"seats": self.sub.options["seats"][:2]}
        self.sub.save()
        result = DevToolSyncService.sync(self.sub, days=2)
        self.assertEqual(result.seats_removed, 1)
        self.assertFalse(DevToolSeat.objects.get(login="idlecat").is_active)

    def test_a_hand_made_mapping_survives_a_sync(self):
        DevToolSyncService.sync(self.sub, days=2)
        other = self.member("someone@example.com")
        DevToolSeat.objects.filter(login="devcat").update(user=other)
        DevToolSyncService.sync(self.sub, days=2)
        self.assertEqual(DevToolSeat.objects.get(login="devcat").user, other)

    def test_a_failing_integration_is_reported_not_raised(self):
        self.sub.adapter_key = "github_copilot"
        self.sub.credential_ref = "FINOPS_TEST_TOKEN_UNSET"
        self.sub.save()
        result = DevToolSyncService.sync(self.sub)
        self.assertFalse(result.ok)
        self.sub.refresh_from_db()
        self.assertIn("FINOPS_TEST_TOKEN_UNSET", self.sub.last_sync_message)


class GitHubCopilotAdapterTests(TestCase):
    def adapter(self, **options):
        config = AdapterConfig(key="github_copilot", credential_ref="GH_TEST_TOKEN", options=options)
        return GitHubCopilotAdapter(config)

    def respond(self, *bodies):
        responses = iter(bodies)

        def urlopen(request, timeout=None):
            self.requests.append(request)
            return io.BytesIO(json.dumps(next(responses)).encode())

        self.requests = []
        return mock.patch("integrations.devtools.github_copilot.urllib.request.urlopen", side_effect=urlopen)

    @mock.patch.dict(os.environ, {"GH_TEST_TOKEN": "ghp_test"})
    def test_seats_are_read_and_parsed(self):
        body = {"total_seats": 1, "seats": [{
            "created_at": "2026-03-01T10:00:00Z", "plan_type": "enterprise",
            "last_activity_at": "2026-09-30T08:00:00Z", "last_activity_editor": "vscode/1.93",
            "pending_cancellation_date": None,
            "assignee": {"login": "octocat"}, "assigning_team": {"slug": "platform"},
        }]}
        with self.respond(body):
            seats = self.adapter().list_seats("acme")
        self.assertEqual(len(seats), 1)
        seat = seats[0]
        self.assertEqual((seat.login, seat.plan, seat.assigning_team), ("octocat", "enterprise", "platform"))
        self.assertEqual(seat.assigned_on, date(2026, 3, 1))
        self.assertEqual(seat.last_activity_at, datetime(2026, 9, 30, 8, tzinfo=dt_timezone.utc))
        request = self.requests[0]
        self.assertIn("/orgs/acme/copilot/billing/seats", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer ghp_test")

    @mock.patch.dict(os.environ, {"GH_TEST_TOKEN": "ghp_test"})
    def test_premium_requests_are_summed_per_user_and_day(self):
        body = {"usageItems": [
            {"sku": "Copilot Premium Request", "model": "claude-sonnet-4", "grossQuantity": 12},
            {"sku": "Copilot Premium Request", "model": "gpt-5", "grossQuantity": 3.5},
            {"sku": "Copilot Business", "grossQuantity": 1},
        ]}
        with self.respond(body):
            rows = self.adapter().get_seat_usage("acme", ["octocat"], date(2026, 9, 1), date(2026, 9, 2))
        self.assertEqual(rows[0].premium_requests, Decimal("15.5"))
        self.assertEqual(rows[0].by_model, {"claude-sonnet-4": 12.0, "gpt-5": 3.5})
        self.assertIn("user=octocat", self.requests[0].full_url)
        self.assertIn("day=1", self.requests[0].full_url)

    @mock.patch.dict(os.environ, {"GH_TEST_TOKEN": "ghp_test"})
    def test_usage_reads_are_bounded(self):
        with self.assertRaises(UsageCallLimitReached):
            self.adapter(max_usage_calls=5).get_seat_usage("acme", ["a", "b"], date(2026, 9, 1), date(2026, 9, 4))

    def test_a_missing_token_is_a_configuration_error(self):
        os.environ.pop("GH_TEST_TOKEN", None)
        with self.assertRaises(AdapterConfigurationError):
            self.adapter().list_seats("acme")


# -- Alert rules -----------------------------------------------------------------------------


class CostAlertRuleTests(FinOpsFixtureMixin, TestCase):
    def setUp(self):
        self.build_finops()
        self.now = timezone.now()

    def rule(self, **kwargs):
        defaults = {"organization": self.organization, "name": "Rule", "metric": AlertMetric.SPEND,
                    "window_hours": AlertWindow.DAY, "threshold": Decimal("5")}
        defaults.update(kwargs)
        return CostAlertRule.objects.create(**defaults)

    def test_a_level_rule_fires_then_stays_quiet(self):
        watcher = self.member("watcher@example.com")
        rule = self.rule(owner=watcher)
        self.make_execution(cost="6", started_at=self.now - timedelta(hours=2))
        event = CostAlertService.evaluate(rule, self.now)
        self.assertIsNotNone(event)
        self.assertIn("USD 6.00", event.summary)
        self.assertIsNone(CostAlertService.evaluate(rule, self.now + timedelta(minutes=15)))
        told = set(Notification.objects.filter(category=NotificationCategory.COST_ALERT)
                   .values_list("recipient__email", flat=True))
        self.assertEqual(told, {"manager@example.com", "watcher@example.com"})

    def test_spend_outside_the_window_does_not_count(self):
        rule = self.rule()
        self.make_execution(cost="6", started_at=self.now - timedelta(hours=30))
        self.assertIsNone(CostAlertService.evaluate(rule, self.now))

    def test_a_spike_is_measured_against_the_baseline(self):
        rule = self.rule(metric=AlertMetric.SPEND_INCREASE, threshold=Decimal("100"))
        for days in range(1, 5):
            self.make_execution(cost="1", started_at=self.now - timedelta(days=days, hours=1))
        self.make_execution(cost="3", started_at=self.now - timedelta(hours=1))
        event = CostAlertService.evaluate(rule, self.now)
        self.assertEqual(event.value, Decimal("200"))

    def test_a_spike_needs_a_baseline(self):
        rule = self.rule(metric=AlertMetric.SPEND_INCREASE, threshold=Decimal("50"))
        self.make_execution(cost="3", started_at=self.now - timedelta(hours=1))
        self.assertIsNone(CostAlertService.evaluate(rule, self.now))
        rule.refresh_from_db()
        self.assertIsNone(rule.last_value)

    def test_the_minimum_spend_suppresses_small_windows(self):
        rule = self.rule(metric=AlertMetric.COST_PER_RUN, threshold=Decimal("0.5"), minimum_spend=Decimal("10"))
        self.make_execution(cost="1", started_at=self.now - timedelta(hours=1))
        self.assertIsNone(CostAlertService.evaluate(rule, self.now))

    def test_a_rule_scoped_to_an_agent_ignores_others(self):
        rule = self.rule(scope=BudgetScope.AGENT, agent=self.agent, metric=AlertMetric.FAILED_SPEND, threshold=Decimal("1"))
        self.make_execution(cost="4", status=ExecutionStatusChoices.FAILED, agent=None,
                            started_at=self.now - timedelta(hours=1))
        self.assertIsNone(CostAlertService.evaluate(rule, self.now))
        self.make_execution(cost="2", status=ExecutionStatusChoices.FAILED, started_at=self.now - timedelta(hours=1))
        self.assertEqual(CostAlertService.evaluate(rule, self.now).value, Decimal("2"))


# -- Explorer ---------------------------------------------------------------------------------


class CostExplorerTests(FinOpsFixtureMixin, TestCase):
    def setUp(self):
        self.build_finops()
        self.now = timezone.now()
        self.other_team = Team.objects.create(organization=self.organization, name="Risk", department="Governance")
        self.make_execution(cost="3", started_at=self.now - timedelta(days=1))
        self.make_execution(cost="1", application=None, started_at=self.now - timedelta(days=2))
        self.make_execution(cost="2", started_at=self.now - timedelta(days=35))  # previous period

    def test_spend_groups_by_department(self):
        data = CostExplorerService(self.organization, days=30, now=self.now).breakdown("department")
        rows = {row["label"]: row for row in data["rows"]}
        self.assertEqual(rows["Claims"]["cost"], 3.0)
        self.assertEqual(rows["Unattributed"]["cost"], 1.0)  # no application, agent has no team
        self.assertEqual(rows["Claims"]["change_pct"], 50.0)
        self.assertEqual(data["next"], "team")

    def test_a_drill_down_narrows_and_names_the_path(self):
        data = CostExplorerService(
            self.organization, days=30, filters={"department": "Claims"}, now=self.now
        ).breakdown("team")
        self.assertEqual(data["totals"]["cost"], 3.0)
        self.assertEqual([c["label"] for c in data["breadcrumbs"]], ["Claims"])
        self.assertEqual(data["next"], "agent")

    def test_the_unattributed_slice_can_be_drilled(self):
        data = CostExplorerService(
            self.organization, days=30, filters={"department": "__none__"}, now=self.now
        ).breakdown("agent")
        self.assertEqual(data["totals"]["cost"], 1.0)

    def test_the_data_endpoint_answers_json_and_csv(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("costs:explorer_data"), {"dim": "model", "metric": "runs"})
        self.assertEqual(response.json()["rows"][0]["runs"], 2)
        response = self.client.get(reverse("costs:explorer_data"), {"dim": "model", "format": "csv"})
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("Mock Large", response.content.decode())


# -- Optimization -----------------------------------------------------------------------------


class OptimizationTests(FinOpsFixtureMixin, TestCase):
    def setUp(self):
        self.build_finops()
        self.now = timezone.now()
        self.model.model_family = "mock"
        self.model.approved_for_use = True
        self.model.save()
        self.agent.risk_level = RiskLevel.LOW
        self.agent.save()
        self.cheap = AIModel.objects.create(
            organization=self.organization, provider=self.provider, model_name="Mock Small",
            model_id="mock-small", model_family="mock", approved_for_use=True,
            input_cost_per_1k=Decimal("0.0003"), output_cost_per_1k=Decimal("0.0015"),
        )
        for index in range(10):
            self.make_execution(cost="2", input_tokens=4000, output_tokens=200, total_tokens=4200,
                                started_at=self.now - timedelta(days=index + 1))

    def kinds(self):
        return set(CostRecommendation.objects.values_list("kind", flat=True))

    def test_a_cheaper_approved_model_in_the_family_is_suggested(self):
        OptimizationService(self.organization, now=self.now).run()
        rec = CostRecommendation.objects.get(kind=RecommendationKind.MODEL_RIGHTSIZING)
        self.assertEqual(rec.suggested_model, self.cheap)
        # 20 spent, alternative costs 10% as much, 80% of a low-risk agent's traffic moves.
        self.assertEqual(rec.estimated_monthly_savings, Decimal("14.40"))

    def test_an_input_heavy_agent_gets_a_caching_suggestion(self):
        OptimizationService(self.organization, now=self.now).run()
        self.assertIn(RecommendationKind.PROMPT_CACHING, self.kinds())

    def test_failure_spend_is_flagged(self):
        self.make_execution(cost="3", status=ExecutionStatusChoices.FAILED, started_at=self.now - timedelta(days=1))
        OptimizationService(self.organization, now=self.now).run()
        rec = CostRecommendation.objects.get(kind=RecommendationKind.FAILURE_WASTE)
        self.assertEqual(rec.estimated_monthly_savings, Decimal("2.10"))

    def test_idle_and_underused_seats_are_found(self):
        sub = self.subscription()
        self.seat(sub, "idle", team=self.team, last_activity_at=self.now - timedelta(days=60))
        self.seat(sub, "never", team=self.team)
        busy = self.seat(sub, "light-enterprise", plan=SeatPlan.ENTERPRISE, last_activity_at=self.now)
        self.usage(busy, self.now.date(), 20)
        OptimizationService(self.organization, now=self.now).run()
        idle = CostRecommendation.objects.get(kind=RecommendationKind.IDLE_SEATS)
        self.assertEqual(idle.estimated_monthly_savings, Decimal("38.00"))
        self.assertEqual(idle.team, self.team)
        downgrade = CostRecommendation.objects.get(kind=RecommendationKind.SEAT_DOWNGRADE)
        self.assertEqual(downgrade.estimated_monthly_savings, Decimal("20.00"))

    def test_heavy_business_users_are_told_to_upgrade(self):
        sub = self.subscription()
        heavy = self.seat(sub, "heavy", last_activity_at=self.now)
        # Business: 19 + 900 over x 0.04 = 55. Enterprise: 39 + 200 over x 0.04 = 47.
        self.usage(heavy, self.now.date(), 1200)
        OptimizationService(self.organization, now=self.now).run()
        rec = CostRecommendation.objects.get(kind=RecommendationKind.SEAT_UPGRADE)
        self.assertEqual(rec.estimated_monthly_savings, Decimal("8.00"))

    def test_rerunning_refreshes_rather_than_duplicates_and_a_dismissal_sticks(self):
        service = OptimizationService(self.organization, now=self.now)
        service.run()
        count = CostRecommendation.objects.count()
        CostRecommendation.objects.filter(kind=RecommendationKind.MODEL_RIGHTSIZING).update(
            status=RecommendationStatus.DISMISSED
        )
        result = service.run()
        self.assertEqual(result["created"], 0)
        self.assertEqual(CostRecommendation.objects.count(), count)
        self.assertEqual(
            CostRecommendation.objects.get(kind=RecommendationKind.MODEL_RIGHTSIZING).status,
            RecommendationStatus.DISMISSED,
        )

    def test_a_recommendation_that_no_longer_applies_is_resolved(self):
        OptimizationService(self.organization, now=self.now).run()
        self.cheap.approved_for_use = False
        self.cheap.save()
        OptimizationService(self.organization, now=self.now).run()
        self.assertEqual(
            CostRecommendation.objects.get(kind=RecommendationKind.MODEL_RIGHTSIZING).status,
            RecommendationStatus.RESOLVED,
        )

    def test_unbudgeted_spend_is_a_governance_finding(self):
        OptimizationService(self.organization, now=self.now).run()
        self.assertIn(RecommendationKind.UNBUDGETED_SPEND, self.kinds())
        Budget.objects.create(organization=self.organization, name="Agent", scope=BudgetScope.AGENT,
                              agent=self.agent, amount=Decimal("50"))
        OptimizationService(self.organization, now=self.now).run()
        self.assertEqual(
            CostRecommendation.objects.get(kind=RecommendationKind.UNBUDGETED_SPEND).status,
            RecommendationStatus.RESOLVED,
        )


# -- Forms and views ----------------------------------------------------------------------------


class BudgetFormTests(FinOpsFixtureMixin, TestCase):
    def setUp(self):
        self.build_finops()

    def form(self, **data):
        payload = {"name": "B", "scope": "organization", "amount": "10", "currency": "usd", "period": "monthly",
                   "starts_on": "2026-09-01", "enforcement": "advisory", "status": "active"}
        payload.update(data)
        return BudgetForm(payload, instance=Budget(organization=self.organization), organization=self.organization)

    def test_departments_are_offered_from_the_registry(self):
        choices = [value for value, _ in self.form().fields["department"].choices]
        self.assertEqual(choices, ["", "Claims", "Operations"])

    def test_developer_tool_spend_needs_a_people_scope(self):
        form = self.form(cost_source="developer_tools", scope="agent", agent=self.agent.pk)
        self.assertFalse(form.is_valid())
        self.assertIn("scope", form.errors)

    def test_developer_tool_budgets_cannot_hard_stop(self):
        form = self.form(cost_source="developer_tools", enforcement="hard_stop")
        self.assertFalse(form.is_valid())
        self.assertIn("enforcement", form.errors)

    def test_cost_source_defaults_to_runtime(self):
        form = self.form()
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["cost_source"], CostSource.RUNTIME)


class FinOpsViewTests(FinOpsFixtureMixin, TestCase):
    def setUp(self):
        self.build_finops()
        self.client.force_login(self.manager)
        self.make_execution(cost="5")
        sub = self.subscription()
        self.seat_ = self.seat(sub, "octocat", team=self.team, last_activity_at=timezone.now() - timedelta(days=40))
        self.budget = Budget.objects.create(organization=self.organization, name="Ops", amount=Decimal("10"))
        CostAlertRule.objects.create(organization=self.organization, name="Spend", threshold=Decimal("1"))

    def test_every_finops_page_renders(self):
        for name, args in [
            ("costs:dashboard", []), ("costs:explorer", []), ("costs:developer_tools", []),
            ("costs:governance", []), ("costs:optimization", []), ("costs:budget_detail", [self.budget.pk]),
            ("costs:rule_create", []), ("costs:subscription_create", []), ("costs:seat_edit", [self.seat_.pk]),
        ]:
            with self.subTest(name):
                self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)

    def test_developer_tools_lists_individuals_by_team(self):
        response = self.client.get(reverse("costs:developer_tools"))
        data = json.loads(response.context["data_json"])
        self.assertEqual(data["teams"][0]["team"], self.team.name)
        self.assertEqual(data["individuals"][0]["login"], "octocat")
        self.assertEqual(response.context["kpis"]["idle"], 1)

    def test_checking_now_raises_rule_alerts(self):
        self.client.post(reverse("costs:evaluate_now"))
        self.assertTrue(CostAlertEvent.objects.exists())

    def test_a_decision_is_recorded_and_audited(self):
        OptimizationService(self.organization).run()
        rec = CostRecommendation.objects.get(kind=RecommendationKind.IDLE_SEATS)
        self.client.post(reverse("costs:recommendation_decide", args=[rec.pk]),
                         {"decision": ["", "implement"], "note": "Removed in GitHub"})
        rec.refresh_from_db()
        self.assertEqual((rec.status, rec.decided_by), (RecommendationStatus.IMPLEMENTED, self.manager))
        self.assertTrue(AuditLog.objects.filter(resource_id=str(rec.pk)).exists())

    def test_a_viewer_cannot_act(self):
        viewer = self.member("viewer@example.com")
        self.client.force_login(viewer)
        self.assertEqual(self.client.post(reverse("costs:evaluate_now")).status_code, 302)
        self.assertFalse(CostAlertEvent.objects.exists())
        self.assertEqual(self.client.post(reverse("costs:optimization_run"),
                                          HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code, 403)
        self.assertEqual(self.client.get(reverse("costs:rule_create")).status_code, 403)
