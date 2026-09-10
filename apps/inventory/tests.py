from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.accounts.roles import Role
from apps.agents.models import AIAgent, AutonomyLevel
from apps.applications_registry.models import AIApplication
from apps.core.constants import RiskLevel
from apps.models_registry.models import AIModel
from apps.organizations.models import Organization, OrganizationMembership
from apps.providers.models import AIProvider

User = get_user_model()


class InventoryAccessControlTests(TestCase):
    def setUp(self):
        self.alpha = Organization.objects.create(name="Alpha Holdings")
        self.beta = Organization.objects.create(name="Beta Holdings")

        self.user = User.objects.create_user(email="viewer@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.alpha, user=self.user, role=Role.VIEWER, is_default=True
        )

        self.alpha_provider = AIProvider.objects.create(organization=self.alpha, name="Alpha Provider")
        self.beta_provider = AIProvider.objects.create(organization=self.beta, name="Beta Provider")

    def test_anonymous_users_are_redirected_to_the_login_page(self):
        response = self.client.get(reverse("inventory:providers"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_a_list_shows_only_the_active_organization(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("inventory:providers"))

        self.assertContains(response, "Alpha Provider")
        self.assertNotContains(response, "Beta Provider")

    def test_a_foreign_record_returns_404_not_a_leak(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("inventory:provider_detail", args=[self.beta_provider.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_an_own_record_renders(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("inventory:provider_detail", args=[self.alpha_provider.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Alpha Provider")


class InventorySearchAndFilterTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Search Org")
        self.user = User.objects.create_user(email="search@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.user, role=Role.VIEWER, is_default=True
        )
        self.client.force_login(self.user)

        provider = AIProvider.objects.create(organization=self.organization, name="Prov")
        self.critical = AIAgent.objects.create(
            organization=self.organization,
            name="Fraud Detection Agent",
            agent_id="fraud",
            provider=provider,
            risk_level=RiskLevel.CRITICAL,
            autonomy_level=AutonomyLevel.AUTONOMOUS,
        )
        self.benign = AIAgent.objects.create(
            organization=self.organization,
            name="Knowledge Assistant",
            agent_id="knowledge",
            provider=provider,
            risk_level=RiskLevel.LOW,
        )

    def test_search_narrows_the_result_set(self):
        response = self.client.get(reverse("inventory:agents"), {"q": "Fraud"})
        self.assertContains(response, "Fraud Detection Agent")
        self.assertNotContains(response, "Knowledge Assistant")

    def test_filtering_by_risk_level(self):
        response = self.client.get(reverse("inventory:agents"), {"risk": RiskLevel.CRITICAL})
        self.assertContains(response, "Fraud Detection Agent")
        self.assertNotContains(response, "Knowledge Assistant")

    def test_an_empty_search_returns_everything(self):
        response = self.client.get(reverse("inventory:agents"), {"q": ""})
        self.assertContains(response, "Fraud Detection Agent")
        self.assertContains(response, "Knowledge Assistant")

    def test_the_inventory_index_reports_counts(self):
        response = self.client.get(reverse("inventory:index"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["counts"]["agents"], 2)


class DashboardTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Dash Org")
        self.user = User.objects.create_user(email="dash@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.user, role=Role.ORG_ADMIN, is_default=True
        )
        self.client.force_login(self.user)

        self.provider = AIProvider.objects.create(
            organization=self.organization, name="Degraded Provider", health_status="degraded"
        )
        AIModel.objects.create(
            organization=self.organization,
            provider=self.provider,
            model_name="Shadow Model",
            model_id="shadow-1",
            approved_for_use=False,
        )

    def test_the_dashboard_renders_with_metrics(self):
        response = self.client.get(reverse("dashboard:home"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["data"].metrics)

    def test_the_attention_panel_surfaces_real_findings(self):
        response = self.client.get(reverse("dashboard:home"))
        titles = " ".join(item.title for item in response.context["data"].attention)

        self.assertIn("degraded", titles)
        self.assertIn("unapproved model", titles)

    def test_every_dashboard_metric_is_backed_by_a_real_module(self):
        """Costs, risks and approvals are built, so nothing is a placeholder."""
        response = self.client.get(reverse("dashboard:home"))
        metrics = response.context["data"].metrics

        self.assertTrue(metrics)
        self.assertEqual([m.label for m in metrics if not m.available], [])

    def test_an_unavailable_metric_would_render_a_dash_not_a_zero(self):
        """The convention that keeps 'no data' from reading as 'no problems'."""
        from apps.dashboard.services import Metric

        placeholder = Metric("Example", "—", available=False)
        self.assertNotEqual(placeholder.value, 0)
        self.assertEqual(placeholder.value, "—")

    def test_the_control_tower_landscape_renders(self):
        response = self.client.get(reverse("dashboard:control_tower"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Degraded Provider")

    def test_a_user_without_an_organization_gets_a_clear_empty_state(self):
        orphan = User.objects.create_user(email="orphan2@example.com", password="pw-12345")
        self.client.force_login(orphan)

        response = self.client.get(reverse("dashboard:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not a member of any organization")


class SeedDemoCommandTests(TestCase):
    def test_the_seed_command_produces_a_populated_organization(self):
        call_command("seed_demo", verbosity=0)

        organization = Organization.objects.get(name="Northwind Financial Group")
        self.assertEqual(AIProvider.objects.for_organization(organization).count(), 5)
        self.assertEqual(AIModel.objects.for_organization(organization).count(), 10)
        self.assertEqual(AIAgent.objects.for_organization(organization).count(), 8)
        self.assertEqual(AIApplication.objects.for_organization(organization).count(), 5)
        self.assertEqual(organization.memberships.count(), 8)
        self.assertEqual(organization.teams.count(), 4)

    def test_the_seed_command_is_idempotent(self):
        call_command("seed_demo", verbosity=0)
        call_command("seed_demo", verbosity=0)

        organization = Organization.objects.get(name="Northwind Financial Group")
        self.assertEqual(AIAgent.objects.for_organization(organization).count(), 8)
        self.assertEqual(Organization.objects.filter(name="Northwind Financial Group").count(), 1)

    def test_seeded_applications_are_linked_to_agents_models_and_providers(self):
        call_command("seed_demo", verbosity=0)

        application = AIApplication.objects.get(name="Customer Support Portal")
        self.assertTrue(application.agents.exists())
        self.assertTrue(application.ai_models.exists())
        self.assertTrue(application.providers.exists())

    def test_the_seeded_estate_contains_a_governance_finding(self):
        call_command("seed_demo", verbosity=0)

        agents = AIAgent.objects.filter(name="Fraud Detection Agent")
        self.assertTrue(agents.exists())
        self.assertTrue(agents.first().has_excessive_autonomy)
