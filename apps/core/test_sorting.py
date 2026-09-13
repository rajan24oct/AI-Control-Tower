"""Column sorting on list pages: the whole result set, in the order a reader expects."""

import re

from django.contrib.auth import get_user_model
from django.db.models import Count
from django.test import RequestFactory, TestCase
from django.urls import reverse

from apps.accounts.roles import Role
from apps.agents.models import AIAgent
from apps.core.constants import HealthStatus, RiskLevel
from apps.core.views import SortableListMixin
from apps.models_registry.models import AIModel
from apps.organizations.models import Organization, OrganizationMembership
from apps.providers.models import AIProvider

User = get_user_model()


class SortableListPageTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Sorting Org")
        self.user = User.objects.create_user(email="sorter@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.user, role=Role.VIEWER, is_default=True
        )
        self.client.force_login(self.user)

    def names(self, url, **params):
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200)
        return [item.name for item in response.context["object_list"]]

    def test_a_column_sorts_both_ways(self):
        for name in ("Bravo", "Alpha", "Charlie"):
            AIProvider.objects.create(organization=self.organization, name=name)
        url = reverse("inventory:providers")

        self.assertEqual(self.names(url, sort="provider"), ["Alpha", "Bravo", "Charlie"])
        self.assertEqual(self.names(url, sort="-provider"), ["Charlie", "Bravo", "Alpha"])

    def test_a_rating_sorts_by_meaning_not_spelling(self):
        for level in (RiskLevel.MODERATE, RiskLevel.CRITICAL, RiskLevel.LOW, RiskLevel.HIGH):
            AIAgent.objects.create(
                organization=self.organization, name=f"{level} agent", risk_level=level
            )

        self.assertEqual(
            self.names(reverse("inventory:agents"), sort="-risk"),
            ["critical agent", "high agent", "moderate agent", "low agent"],
        )

    def test_an_unknown_column_is_ignored(self):
        AIProvider.objects.create(organization=self.organization, name="Only")
        self.assertEqual(self.names(reverse("inventory:providers"), sort="-password"), ["Only"])

    def test_the_header_marks_the_sorted_column_and_offers_the_other_direction(self):
        AIProvider.objects.create(organization=self.organization, name="Only")
        response = self.client.get(
            reverse("inventory:providers"), {"sort": "provider", "status": "active"}
        )

        self.assertContains(response, 'aria-sort="ascending"')
        # The filter survives the new sort.
        self.assertContains(response, 'href="?sort=-provider&amp;status=active"')

    def test_changing_the_sort_returns_to_the_first_page(self):
        AIProvider.objects.create(organization=self.organization, name="Only")
        response = self.client.get(reverse("inventory:providers"), {"page": "1", "sort": "provider"})
        links = re.findall(r'class="ct-sort[^"]*" href="([^"]+)"', response.content.decode())

        self.assertTrue(links)
        self.assertFalse(any("page=" in link for link in links))


class SortExpressionTests(TestCase):
    """The mixin on its own: aggregate columns and ranked choices."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Expression Org")
        busy = AIProvider.objects.create(
            organization=self.organization, name="Busy", health_status=HealthStatus.UNHEALTHY
        )
        AIProvider.objects.create(
            organization=self.organization, name="Idle", health_status=HealthStatus.HEALTHY
        )
        for index in range(3):
            AIModel.objects.create(
                organization=self.organization, provider=busy,
                model_name=f"Model {index}", model_id=f"model-{index}",
            )
        self.providers = AIProvider.objects.filter(organization=self.organization)

    def sorted_names(self, sort, **fields):
        probe = SortableListMixin()
        probe.sort_fields = fields
        probe.request = RequestFactory().get("/", {"sort": sort})
        return [provider.name for provider in probe.apply_sort(self.providers)]

    def test_an_aggregate_column_sorts(self):
        # Django refuses to order by an aggregate it has not annotated.
        self.assertEqual(self.sorted_names("-models", models=Count("models")), ["Busy", "Idle"])
        self.assertEqual(self.sorted_names("models", models=Count("models")), ["Idle", "Busy"])

    def test_a_ranked_choice_sorts_in_rank_order(self):
        # Alphabetically "unhealthy" follows "healthy" too, so rank must decide.
        ranking = ("health_status", HealthStatus.values)
        self.assertEqual(self.sorted_names("health", health=ranking), ["Idle", "Busy"])
        self.assertEqual(self.sorted_names("-health", health=ranking), ["Busy", "Idle"])
