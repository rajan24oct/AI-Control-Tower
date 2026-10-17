"""Modules switched on and off per organization by site administrators."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.roles import Role
from apps.audit.models import AuditLog
from apps.organizations.models import Organization, OrganizationMembership
from apps.organizations.modules import MODULES, is_enabled, module_states, set_module_states

User = get_user_model()


class ModuleTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Acme Insurance")
        self.admin = User.objects.create_user(email="admin@acme.example", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.org, user=self.admin, role=Role.ORG_ADMIN, is_default=True
        )
        self.root = User.objects.create_superuser(email="root@platform.example", password="pw-12345")
        OrganizationMembership.objects.create(organization=self.org, user=self.root, role=Role.ORG_ADMIN)

    def switch_off(self, *keys):
        set_module_states(self.org, {module.key: module.key not in keys for module in MODULES})
        self.org.save()

    def test_every_module_is_on_until_switched_off(self):
        self.assertTrue(all(module_states(self.org).values()))
        self.switch_off("mcp_gateway")
        self.org.refresh_from_db()
        self.assertFalse(is_enabled(self.org, "mcp_gateway"))
        self.assertTrue(is_enabled(self.org, "a2a_gateway"))

    def test_a_site_administrator_switches_modules_on_the_organization_form(self):
        self.client.force_login(self.root)
        form = self.client.get(reverse("access:organization_edit", args=[self.org.pk]))
        self.assertContains(form, 'name="module_irgf_agent"')

        keep = {f"module_{module.key}": "on" for module in MODULES if module.key not in {"finops", "irgf_agent"}}
        self.client.post(reverse("access:organization_edit", args=[self.org.pk]), {
            "name": self.org.name, "status": "active", **keep,
        })
        self.org.refresh_from_db()
        self.assertFalse(is_enabled(self.org, "finops"))
        self.assertFalse(is_enabled(self.org, "irgf_agent"))
        self.assertTrue(is_enabled(self.org, "ai_gateway"))
        entry = AuditLog.objects.filter(organization=self.org, resource_type=Organization._meta.label).latest("created_at")
        self.assertFalse(entry.new_value["settings"]["modules"]["finops"])

        detail = self.client.get(reverse("access:organization_detail", args=[self.org.pk]))
        self.assertContains(detail, "AI FinOps")

    def test_an_organization_admin_cannot_switch_modules(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("access:organization_edit", args=[self.org.pk]), {
            "name": self.org.name, "status": "active",
        })
        self.assertEqual(response.status_code, 403)
        self.org.refresh_from_db()
        self.assertTrue(all(module_states(self.org).values()))

    def test_a_switched_off_module_leaves_the_menu_and_its_pages(self):
        self.switch_off("mcp_gateway", "finops")
        self.client.force_login(self.admin)

        page = self.client.get(reverse("mcp_gateway:tools"))
        self.assertContains(page, "is not enabled for Acme Insurance", status_code=404)
        self.assertEqual(self.client.get(reverse("costs:dashboard")).status_code, 404)

        home = self.client.get(reverse("dashboard:home"))
        self.assertNotContains(home, reverse("mcp_gateway:tools"))
        self.assertNotContains(home, reverse("costs:budgets"))
        # The other gateways stay, inside the same menu.
        self.assertContains(home, reverse("a2a:skills"))
        self.assertContains(home, reverse("routing:list"))
        self.assertEqual(self.client.get(reverse("a2a:skills")).status_code, 200)

    def test_switching_every_gateway_off_removes_the_menu(self):
        self.switch_off("ai_gateway", "mcp_gateway", "a2a_gateway")
        self.client.force_login(self.admin)
        self.assertNotContains(self.client.get(reverse("dashboard:home")), ">Gateways</a>")

    def test_the_irgf_agent_can_be_switched_off(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("dashboard:home")), "data-assistant")
        self.switch_off("irgf_agent")
        self.assertNotContains(self.client.get(reverse("dashboard:home")), "data-assistant")
        self.assertEqual(self.client.post(reverse("assistant:ask"), {"question": "hi"}).status_code, 404)

    def test_the_help_panel_does_not_link_into_switched_off_modules(self):
        self.switch_off("policies")
        self.client.force_login(self.admin)
        page = self.client.get(reverse("gateway:overview"))
        self.assertContains(page, 'id="ct-help"')
        self.assertNotContains(page, reverse("policies:list"))
