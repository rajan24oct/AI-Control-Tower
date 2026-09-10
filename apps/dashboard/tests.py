"""Tests for the executive Control Tower landscape (sections 32, 33, 34)."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.agents.models import AIAgent, AutonomyLevel
from apps.applications_registry.models import AIApplication
from apps.approvals.models import ApprovalRequest, ApprovalStatus
from apps.core.constants import HealthStatus, RiskLevel
from apps.dashboard.landscape import ControlTowerService, health_from_failure_rate
from apps.executions.models import Execution, ExecutionStatusChoices
from apps.models_registry.models import AIModel
from apps.organizations.models import Organization, OrganizationMembership, Team
from apps.providers.models import AIProvider
from apps.risks.models import Risk, RiskStatus

User = get_user_model()


class HealthDerivationTests(TestCase):
    def test_no_runs_is_unknown_not_healthy(self):
        """Absence of data must never read as absence of problems."""
        self.assertEqual(health_from_failure_rate(0, 0), HealthStatus.UNKNOWN)

    def test_a_clean_record_is_healthy(self):
        self.assertEqual(health_from_failure_rate(100, 0), HealthStatus.HEALTHY)

    def test_routine_failure_rates_stay_healthy(self):
        # 5% is normal for model calls and must not flag.
        self.assertEqual(health_from_failure_rate(100, 5), HealthStatus.HEALTHY)

    def test_the_degraded_band(self):
        self.assertEqual(health_from_failure_rate(100, 10), HealthStatus.DEGRADED)
        self.assertEqual(health_from_failure_rate(100, 24), HealthStatus.DEGRADED)

    def test_the_unhealthy_band(self):
        self.assertEqual(health_from_failure_rate(100, 25), HealthStatus.UNHEALTHY)
        self.assertEqual(health_from_failure_rate(4, 4), HealthStatus.UNHEALTHY)


class LandscapeFixtureMixin:
    def build_estate(self, name="Landscape Org"):
        self.organization = Organization.objects.create(name=name)
        self.team = Team.objects.create(organization=self.organization, name="Platform")
        self.provider = AIProvider.objects.create(
            organization=self.organization, name="Healthy Provider", adapter_key="mock",
            health_status=HealthStatus.HEALTHY, is_approved=True,
        )
        self.bad_provider = AIProvider.objects.create(
            organization=self.organization, name="Broken Provider", adapter_key="mock",
            health_status=HealthStatus.UNHEALTHY, is_approved=False,
        )
        self.model = AIModel.objects.create(
            organization=self.organization, provider=self.provider,
            model_name="Mock Large", model_id="mock-large", approved_for_use=True,
        )
        self.agent = AIAgent.objects.create(
            organization=self.organization, name="Support Agent", agent_id="support-agent",
            provider=self.provider, primary_model=self.model, team=self.team,
            risk_level=RiskLevel.MODERATE,
        )
        self.application = AIApplication.objects.create(
            organization=self.organization, name="Support Portal", team=self.team,
        )
        self.application.agents.add(self.agent)

    def make_executions(self, total=10, failed=0, cost="1.00"):
        for index in range(total):
            status = (
                ExecutionStatusChoices.FAILED
                if index < failed
                else ExecutionStatusChoices.COMPLETED
            )
            Execution.objects.create(
                organization=self.organization,
                external_id=f"exec-{Execution.objects.count() + 1}",
                agent=self.agent,
                application=self.application,
                provider=self.provider,
                model=self.model,
                status=status,
                cost=Decimal(cost),
                input_tokens=100,
                output_tokens=50,
            )


class LandscapeTests(LandscapeFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()

    def test_the_four_layers_are_present_and_ordered(self):
        layers = ControlTowerService(self.organization).build().layers
        self.assertEqual(
            [layer.key for layer in layers],
            ["applications", "agents", "models", "providers"],
        )

    def test_execution_rollups_land_on_the_right_node(self):
        self.make_executions(total=10, failed=1, cost="2.00")
        layers = {l.key: l for l in ControlTowerService(self.organization).build().layers}

        agent_node = layers["agents"].nodes[0]
        self.assertEqual(agent_node.runs, 10)
        self.assertEqual(agent_node.failed, 1)
        self.assertEqual(agent_node.cost, Decimal("20.00"))
        self.assertEqual(agent_node.tokens, 1500)

    def test_a_node_with_no_runs_reports_unknown_health(self):
        layers = {l.key: l for l in ControlTowerService(self.organization).build().layers}
        self.assertEqual(layers["agents"].nodes[0].health, HealthStatus.UNKNOWN)
        self.assertEqual(layers["agents"].nodes[0].runs, 0)

    def test_agent_health_is_derived_from_failures(self):
        self.make_executions(total=10, failed=4)
        layers = {l.key: l for l in ControlTowerService(self.organization).build().layers}
        self.assertEqual(layers["agents"].nodes[0].health, HealthStatus.UNHEALTHY)

    def test_a_provider_uses_its_own_probe_not_execution_outcomes(self):
        """A provider reports its own health; runs must not override it."""
        self.make_executions(total=10, failed=0)
        layers = {l.key: l for l in ControlTowerService(self.organization).build().layers}
        by_name = {node.name: node for node in layers["providers"].nodes}

        self.assertEqual(by_name["Healthy Provider"].health, HealthStatus.HEALTHY)
        self.assertEqual(by_name["Broken Provider"].health, HealthStatus.UNHEALTHY)

    def test_live_runs_are_counted(self):
        Execution.objects.create(
            organization=self.organization, external_id="live-1", agent=self.agent,
            status=ExecutionStatusChoices.RUNNING,
        )
        layers = {l.key: l for l in ControlTowerService(self.organization).build().layers}
        self.assertEqual(layers["agents"].nodes[0].live, 1)

    def test_open_risks_attach_to_their_asset(self):
        Risk.objects.create(
            organization=self.organization, title="Agent risk", agent=self.agent,
            status=RiskStatus.IDENTIFIED,
        )
        Risk.objects.create(
            organization=self.organization, title="Closed risk", agent=self.agent,
            status=RiskStatus.CLOSED,
        )
        layers = {l.key: l for l in ControlTowerService(self.organization).build().layers}
        self.assertEqual(layers["agents"].nodes[0].open_risks, 1)

    def test_pending_approvals_attach_to_their_agent(self):
        ApprovalRequest.objects.create(
            organization=self.organization, agent=self.agent,
            requested_action="Do a thing", status=ApprovalStatus.PENDING,
        )
        ApprovalRequest.objects.create(
            organization=self.organization, agent=self.agent,
            requested_action="Already done", status=ApprovalStatus.APPROVED,
        )
        layers = {l.key: l for l in ControlTowerService(self.organization).build().layers}
        self.assertEqual(layers["agents"].nodes[0].pending_approvals, 1)

    def test_governance_flags_are_surfaced(self):
        self.agent.autonomy_level = AutonomyLevel.AUTONOMOUS
        self.agent.requires_human_approval = False
        self.agent.risk_level = RiskLevel.CRITICAL
        self.agent.save()

        layers = {l.key: l for l in ControlTowerService(self.organization).build().layers}
        flags = layers["agents"].nodes[0].flags
        self.assertIn("Excessive autonomy", flags)
        self.assertIn("L4 autonomy", flags)

    def test_an_unapproved_provider_is_flagged(self):
        layers = {l.key: l for l in ControlTowerService(self.organization).build().layers}
        by_name = {node.name: node for node in layers["providers"].nodes}
        self.assertIn("Unapproved", by_name["Broken Provider"].flags)

    def test_needs_attention_reflects_real_signals(self):
        layers = {l.key: l for l in ControlTowerService(self.organization).build().layers}
        by_name = {node.name: node for node in layers["providers"].nodes}
        self.assertTrue(by_name["Broken Provider"].needs_attention)
        self.assertFalse(by_name["Healthy Provider"].needs_attention)

    def test_runs_outside_the_window_are_excluded(self):
        self.make_executions(total=5)
        Execution.objects.filter(external_id="exec-1").update(
            started_at=timezone.now() - timedelta(days=90)
        )
        layers = {l.key: l for l in ControlTowerService(self.organization).build().layers}
        self.assertEqual(layers["agents"].nodes[0].runs, 4)

    def test_nodes_link_to_their_records(self):
        layers = {l.key: l for l in ControlTowerService(self.organization).build().layers}
        node = layers["agents"].nodes[0]
        self.assertEqual(
            node.url, reverse("inventory:agent_detail", args=[self.agent.pk])
        )

    def test_the_landscape_is_organization_scoped(self):
        other = Organization.objects.create(name="Rival Landscape")
        AIAgent.objects.create(organization=other, name="Foreign Agent", agent_id="foreign")

        layers = {l.key: l for l in ControlTowerService(self.organization).build().layers}
        names = [node.name for node in layers["agents"].nodes]
        self.assertNotIn("Foreign Agent", names)

    def test_an_empty_organization_yields_an_empty_ecosystem(self):
        self.assertEqual(ControlTowerService(None).build().layers, [])


class EcosystemSummaryTests(LandscapeFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate("Summary Org")
        self.make_executions(total=20, failed=2, cost="0.50")

    def test_totals(self):
        summary = ControlTowerService(self.organization).build().summary
        self.assertEqual(summary["executions"], 20)
        self.assertEqual(summary["failed"], 2)
        self.assertEqual(summary["cost"], Decimal("10.00"))
        self.assertEqual(summary["failure_rate"], 10.0)

    def test_overall_health_takes_the_worst_provider(self):
        """A degraded provider degrades everything above it."""
        summary = ControlTowerService(self.organization).build().summary
        self.assertEqual(summary["overall_health"], HealthStatus.UNHEALTHY)
        self.assertEqual(summary["unhealthy_providers"], 1)

    def test_overall_health_is_healthy_when_all_providers_are(self):
        self.bad_provider.health_status = HealthStatus.HEALTHY
        self.bad_provider.save()

        summary = ControlTowerService(self.organization).build().summary
        self.assertEqual(summary["overall_health"], HealthStatus.HEALTHY)

    def test_risk_and_approval_counts(self):
        Risk.objects.create(
            organization=self.organization, title="Critical", likelihood=5, impact=5,
            status=RiskStatus.IDENTIFIED,
        )
        ApprovalRequest.objects.create(
            organization=self.organization, requested_action="Decide",
            status=ApprovalStatus.PENDING, risk_level=RiskLevel.CRITICAL,
        )
        summary = ControlTowerService(self.organization).build().summary

        self.assertEqual(summary["open_risks"], 1)
        self.assertEqual(summary["critical_risks"], 1)
        self.assertEqual(summary["pending_approvals"], 1)
        self.assertEqual(summary["critical_approvals"], 1)

    def test_asset_counts_are_reported(self):
        summary = ControlTowerService(self.organization).build().summary
        self.assertEqual(summary["counts"]["providers"], 2)
        self.assertEqual(summary["counts"]["agents"], 1)


class ControlTowerViewTests(LandscapeFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate("View Org")
        self.user = User.objects.create_user(email="exec@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.user, role=Role.ORG_ADMIN, is_default=True
        )
        self.client.force_login(self.user)
        self.make_executions(total=6, failed=3)

    def test_the_page_renders_with_the_ecosystem(self):
        response = self.client.get(reverse("dashboard:control_tower"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["ecosystem"].layers), 4)

    def test_indicators_appear_on_the_page(self):
        response = self.client.get(reverse("dashboard:control_tower"))
        self.assertContains(response, "Support Agent")
        self.assertContains(response, "ct-health-dot")
        self.assertContains(response, "Ecosystem Health")

    def test_the_summary_is_available_to_the_template(self):
        response = self.client.get(reverse("dashboard:control_tower"))
        self.assertEqual(response.context["summary"]["executions"], 6)

    def test_the_attention_panel_is_included(self):
        response = self.client.get(reverse("dashboard:control_tower"))
        self.assertIn("attention", response.context)

    def test_anonymous_users_are_redirected(self):
        self.client.logout()
        self.assertEqual(
            self.client.get(reverse("dashboard:control_tower")).status_code, 302
        )

    def test_a_user_without_an_organization_gets_an_empty_state(self):
        orphan = User.objects.create_user(email="orphan@example.com", password="pw-12345")
        self.client.force_login(orphan)

        response = self.client.get(reverse("dashboard:control_tower"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No organization selected")

    def test_query_count_does_not_grow_with_the_number_of_assets(self):
        """The rollups are grouped queries, so the page must not go N+1."""
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        url = reverse("dashboard:control_tower")
        # The first request writes the session, which costs extra queries;
        # measure warm so the comparison isolates asset count.
        self.client.get(url)

        with CaptureQueriesContext(connection) as small:
            self.client.get(url)
        baseline = len(small.captured_queries)

        for index in range(15):
            AIAgent.objects.create(
                organization=self.organization, name=f"Agent {index}",
                agent_id=f"agent-{index}", provider=self.provider,
            )

        with CaptureQueriesContext(connection) as large:
            self.client.get(url)

        self.assertEqual(len(large.captured_queries), baseline)
