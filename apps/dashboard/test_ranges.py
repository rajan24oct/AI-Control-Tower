"""Tests for the dashboard date-range filter and module status (section 7)."""

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.agents.models import AIAgent
from apps.core.constants import LifecycleStatus
from apps.dashboard.ranges import MAX_DAYS, DateRange
from apps.dashboard.services import DashboardService
from apps.executions.models import Execution, ExecutionStatusChoices
from apps.models_registry.models import AIModel
from apps.organizations.models import Organization, OrganizationMembership
from apps.policies.models import Policy, PolicyEffect, PolicyType, PolicyViolation
from apps.providers.models import AIProvider

User = get_user_model()
TODAY = date(2026, 9, 10)


class DateRangeParsingTests(TestCase):
    def test_the_default_is_thirty_days(self):
        window = DateRange.from_request({}, today=TODAY)
        self.assertEqual(window.key, "30")
        self.assertEqual(window.days, 30)
        self.assertEqual(window.end, TODAY)

    def test_a_preset_is_honoured(self):
        window = DateRange.from_request({"range": "7"}, today=TODAY)
        self.assertEqual(window.days, 7)
        self.assertEqual(window.start, date(2026, 9, 4))

    def test_today_is_a_single_day(self):
        window = DateRange.from_request({"range": "1"}, today=TODAY)
        self.assertTrue(window.is_single_day)
        self.assertEqual(window.days, 1)
        self.assertEqual(window.describe(), "today")

    def test_an_unknown_preset_falls_back_to_the_default(self):
        window = DateRange.from_request({"range": "nonsense"}, today=TODAY)
        self.assertEqual(window.key, "30")

    def test_explicit_dates_win_over_a_preset(self):
        window = DateRange.from_request(
            {"range": "7", "from": "2026-01-01", "to": "2026-01-31"}, today=TODAY
        )
        self.assertTrue(window.is_custom)
        self.assertEqual(window.days, 31)

    def test_reversed_dates_are_swapped_rather_than_rejected(self):
        window = DateRange.from_request(
            {"from": "2026-03-31", "to": "2026-03-01"}, today=TODAY
        )
        self.assertEqual(window.start, date(2026, 3, 1))
        self.assertEqual(window.end, date(2026, 3, 31))

    def test_only_a_start_date_runs_to_today(self):
        window = DateRange.from_request({"from": "2026-09-01"}, today=TODAY)
        self.assertEqual(window.end, TODAY)

    def test_an_unparseable_date_falls_back_instead_of_raising(self):
        """A broken filter must not break the dashboard."""
        window = DateRange.from_request({"from": "not-a-date"}, today=TODAY)
        self.assertEqual(window.key, "30")

    def test_an_excessive_range_is_clamped(self):
        window = DateRange.from_request(
            {"from": "2000-01-01", "to": "2026-09-10"}, today=TODAY
        )
        self.assertEqual(window.days, MAX_DAYS)
        self.assertEqual(window.end, TODAY)

    def test_each_day_covers_the_window(self):
        days = DateRange.preset("7", today=TODAY).each_day()
        self.assertEqual(len(days), 7)
        self.assertEqual(days[0], date(2026, 9, 4))
        self.assertEqual(days[-1], TODAY)


class DashboardRangeTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Range Org")
        self.user = User.objects.create_user(email="exec@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.user,
            role=Role.ORG_ADMIN, is_default=True,
        )
        self.client.force_login(self.user)

        self.provider = AIProvider.objects.create(
            organization=self.organization, name="Mock", adapter_key="mock"
        )
        self.model = AIModel.objects.create(
            organization=self.organization, provider=self.provider,
            model_name="Mock", model_id="mock-1",
        )
        self.agent = AIAgent.objects.create(
            organization=self.organization, name="Agent", agent_id="agent",
            provider=self.provider, primary_model=self.model,
        )

        self.make_execution("today-1", days_ago=0, cost="1.00")
        self.make_execution("recent-1", days_ago=3, cost="2.00")
        self.make_execution("older-1", days_ago=20, cost="4.00")
        self.make_execution("ancient-1", days_ago=200, cost="8.00")

    def make_execution(self, external_id, days_ago, cost):
        execution = Execution.objects.create(
            organization=self.organization, external_id=external_id,
            agent=self.agent, provider=self.provider, model=self.model,
            status=ExecutionStatusChoices.COMPLETED, cost=Decimal(cost),
            input_tokens=100, output_tokens=100,
        )
        # started_at has a default, so the backdating is a separate update.
        Execution.objects.filter(pk=execution.pk).update(
            started_at=timezone.now() - timedelta(days=days_ago)
        )
        return execution

    def metrics_for(self, **params):
        response = self.client.get(reverse("dashboard:home"), params)
        return {m.label: m.value for m in response.context["data"].metrics}

    def test_today_only_counts_todays_runs(self):
        self.assertEqual(self.metrics_for(range="1")["Executions"], "1")

    def test_seven_days_widens_the_window(self):
        self.assertEqual(self.metrics_for(range="7")["Executions"], "2")

    def test_thirty_days_widens_it_further(self):
        self.assertEqual(self.metrics_for(range="30")["Executions"], "3")

    def test_a_custom_range_is_applied(self):
        start = (timezone.now() - timedelta(days=250)).date()
        end = (timezone.now() - timedelta(days=150)).date()
        metrics = self.metrics_for(**{"from": start.isoformat(), "to": end.isoformat()})
        self.assertEqual(metrics["Executions"], "1")

    def test_cost_follows_the_range(self):
        self.assertEqual(self.metrics_for(range="1")["Cost"], "$1.00")
        self.assertEqual(self.metrics_for(range="7")["Cost"], "$3.00")

    def test_the_period_hint_describes_the_window(self):
        response = self.client.get(reverse("dashboard:home"), {"range": "7"})
        hints = {m.label: m.hint for m in response.context["data"].metrics}
        self.assertEqual(hints["Executions"], "last 7 days")

    def test_chart_buckets_match_the_range(self):
        response = self.client.get(reverse("dashboard:home"), {"range": "90"})
        charts = response.context["data"].charts
        self.assertEqual(len(charts["execution_volume"]["labels"]), 90)
        self.assertEqual(len(charts["spend_over_time"]["cost"]), 90)

    def test_the_range_is_exposed_to_the_template(self):
        response = self.client.get(reverse("dashboard:home"), {"range": "7"})
        self.assertEqual(response.context["date_range"].key, "7")
        self.assertContains(response, "Last 7 days")

    def test_a_malformed_range_still_renders(self):
        response = self.client.get(
            reverse("dashboard:home"), {"from": "garbage", "to": "worse"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["date_range"].key, "30")

    def test_the_attention_panel_is_not_range_filtered(self):
        """'Needs attention' is about now, not about a historical window."""
        Policy.objects.create(
            organization=self.organization, name="Blocker",
            policy_type=PolicyType.TOOL_ACCESS, effect=PolicyEffect.DENY,
            status=LifecycleStatus.ACTIVE, configuration={"blocked_tools": ["x"]},
        )
        PolicyViolation.objects.create(
            organization=self.organization, policy=Policy.objects.first(),
            effect=PolicyEffect.DENY, reason="blocked",
        )
        narrow = self.client.get(reverse("dashboard:home"), {"range": "1"})
        wide = self.client.get(reverse("dashboard:home"), {"range": "90"})

        self.assertEqual(
            [i.title for i in narrow.context["data"].attention],
            [i.title for i in wide.context["data"].attention],
        )


class ModuleStatusTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Modules Org")
        self.user = User.objects.create_user(email="mod@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.user,
            role=Role.ORG_ADMIN, is_default=True,
        )
        self.client.force_login(self.user)

    def test_every_module_is_reported(self):
        modules = DashboardService(self.organization).build().modules
        self.assertEqual(
            [m.name for m in modules],
            ["Policy engine", "Model routing", "AI gateway", "Evaluation centre"],
        )

    def test_nothing_is_still_marked_planned(self):
        """Kept as a check that this list stays honest, not as a milestone."""
        modules = DashboardService(self.organization).build().modules
        self.assertEqual([m.name for m in modules if m.state == "planned"], [])

    def test_the_policy_engine_is_live_and_linked(self):
        modules = {m.name: m for m in DashboardService(self.organization).build().modules}
        policy_engine = modules["Policy engine"]

        self.assertEqual(policy_engine.state, "live")
        self.assertEqual(policy_engine.url, reverse("policies:list"))

    def test_built_modules_are_live_and_linked(self):
        """Never imply a control is enforcing something when it does not exist."""
        modules = {m.name: m for m in DashboardService(self.organization).build().modules}

        for name in ("Policy engine", "Model routing", "AI gateway", "Evaluation centre"):
            with self.subTest(module=name):
                self.assertEqual(modules[name].state, "live")
                self.assertTrue(modules[name].url)

    def test_the_policy_engine_reports_its_active_policy_count(self):
        Policy.objects.create(
            organization=self.organization, name="One",
            policy_type=PolicyType.TOOL_ACCESS, effect=PolicyEffect.DENY,
            status=LifecycleStatus.ACTIVE,
        )
        Policy.objects.create(
            organization=self.organization, name="Retired",
            policy_type=PolicyType.TOOL_ACCESS, effect=PolicyEffect.DENY,
            status=LifecycleStatus.RETIRED,
        )
        modules = {m.name: m for m in DashboardService(self.organization).build().modules}
        self.assertIn("1 active policies", modules["Policy engine"].description)

    def test_the_modules_card_is_not_on_the_dashboard(self):
        # Removed by request; the per-module charts carry this information now.
        response = self.client.get(reverse("dashboard:home"))
        self.assertNotContains(response, "Platform Modules")

    def test_policy_metrics_appear_on_the_dashboard(self):
        response = self.client.get(reverse("dashboard:home"))
        labels = [m.label for m in response.context["data"].metrics]

        self.assertIn("Active Policies", labels)
        self.assertIn("Policy Denials", labels)
