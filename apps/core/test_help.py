"""The in-page help panel: every module has one, and none of it points nowhere."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.roles import Role
from apps.core.help import HELP, help_for
from apps.organizations.models import Organization, OrganizationMembership

User = get_user_model()


class HelpContentTests(TestCase):
    def test_every_related_link_resolves(self):
        for key, entry in HELP.items():
            for label, name in entry.get("related", []):
                with self.subTest(page=key, link=label):
                    reverse(name)

    def test_every_entry_has_the_core_sections(self):
        for key, entry in HELP.items():
            with self.subTest(page=key):
                self.assertTrue(entry["title"])
                self.assertTrue(entry["summary"])
                self.assertTrue(entry["practices"])

    def test_a_page_falls_back_to_its_module(self):
        self.assertEqual(help_for("routing", "simulator")["title"], "Routing Simulator")
        self.assertEqual(help_for("routing", "list")["title"], "Model Routing")
        self.assertIsNone(help_for("no_such_app", "list"))


class HelpPanelTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Help Org")
        self.user = User.objects.create_user(email="reader@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.user, role=Role.ORG_ADMIN, is_default=True
        )
        self.client.force_login(self.user)

    def test_gateway_pages_show_their_help_with_the_api_root(self):
        pages = {
            "gateway:overview": "AI Gateway",
            "routing:list": "Model Routing",
            "mcp_gateway:overview": "MCP Gateway",
            "a2a:overview": "A2A Gateway",
            "policies:list": "Control Policies",
        }
        for name, title in pages.items():
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'id="ct-help"')
                self.assertContains(response, "data-help-toggle")
                self.assertContains(response, f'id="ct-help-title">{title}<')
                self.assertNotContains(response, "{api}")

    def test_ajax_region_swaps_do_not_rebuild_the_panel(self):
        response = self.client.get(reverse("routing:list"), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertNotContains(response, 'id="ct-help"')
