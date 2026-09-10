from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.roles import Role
from apps.core.constants import LifecycleStatus
from apps.organizations.middleware import SESSION_KEY
from apps.organizations.models import Organization, OrganizationMembership, Team
from apps.providers.models import AIProvider

User = get_user_model()


class OrganizationModelTests(TestCase):
    def test_slug_is_derived_from_the_name(self):
        organization = Organization.objects.create(name="Northwind Financial")
        self.assertEqual(organization.slug, "northwind-financial")

    def test_a_user_can_belong_to_several_organizations(self):
        user = User.objects.create_user(email="multi@example.com")
        for name in ("One", "Two"):
            OrganizationMembership.objects.create(
                organization=Organization.objects.create(name=name), user=user, role=Role.VIEWER
            )
        self.assertEqual(user.organizations.count(), 2)

    def test_membership_is_unique_per_user_and_organization(self):
        from django.db import IntegrityError

        organization = Organization.objects.create(name="Solo")
        user = User.objects.create_user(email="once@example.com")
        OrganizationMembership.objects.create(organization=organization, user=user, role=Role.VIEWER)
        with self.assertRaises(IntegrityError):
            OrganizationMembership.objects.create(
                organization=organization, user=user, role=Role.ORG_ADMIN
            )


class OrganizationIsolationTests(TestCase):
    """Isolation must hold in the queryset, not just in the UI (section 27)."""

    def setUp(self):
        self.alpha = Organization.objects.create(name="Alpha Corp")
        self.beta = Organization.objects.create(name="Beta Corp")

        self.alpha_user = User.objects.create_user(email="alpha@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.alpha, user=self.alpha_user, role=Role.ORG_ADMIN
        )

        self.alpha_provider = AIProvider.objects.create(organization=self.alpha, name="Alpha Provider")
        self.beta_provider = AIProvider.objects.create(organization=self.beta, name="Beta Provider")

    def test_for_organization_returns_only_that_tenant(self):
        results = AIProvider.objects.for_organization(self.alpha)
        self.assertIn(self.alpha_provider, results)
        self.assertNotIn(self.beta_provider, results)

    def test_for_user_spans_only_the_users_memberships(self):
        results = AIProvider.objects.for_user(self.alpha_user)
        self.assertEqual(list(results), [self.alpha_provider])

    def test_anonymous_users_see_nothing(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertEqual(AIProvider.objects.for_user(AnonymousUser()).count(), 0)

    def test_superuser_sees_every_tenant(self):
        root = User.objects.create_superuser(email="root3@example.com", password="pw-12345")
        self.assertEqual(AIProvider.objects.for_user(root).count(), 2)


class ActiveOrganizationMiddlewareTests(TestCase):
    def setUp(self):
        self.alpha = Organization.objects.create(name="Alpha Ltd")
        self.beta = Organization.objects.create(name="Beta Ltd")
        self.user = User.objects.create_user(email="member@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.alpha, user=self.user, role=Role.ORG_ADMIN, is_default=True
        )
        self.client.force_login(self.user)

    def test_the_default_organization_is_attached_to_the_request(self):
        response = self.client.get(reverse("dashboard:home"))
        self.assertEqual(response.wsgi_request.organization, self.alpha)

    def test_a_session_pointing_at_a_foreign_organization_is_discarded(self):
        session = self.client.session
        session[SESSION_KEY] = str(self.beta.pk)
        session.save()

        response = self.client.get(reverse("dashboard:home"))
        self.assertEqual(response.wsgi_request.organization, self.alpha)

    def test_switching_to_an_organization_the_user_does_not_belong_to_is_ignored(self):
        self.client.post(
            reverse("dashboard:switch_organization"), {"organization_id": str(self.beta.pk)}
        )
        response = self.client.get(reverse("dashboard:home"))
        self.assertEqual(response.wsgi_request.organization, self.alpha)


class TeamTests(TestCase):
    def test_team_slugs_are_unique_within_an_organization(self):
        from django.db import IntegrityError

        organization = Organization.objects.create(name="Team Org")
        Team.objects.create(organization=organization, name="Platform")
        with self.assertRaises(IntegrityError):
            Team.objects.create(organization=organization, name="Platform")

    def test_the_same_team_name_may_exist_in_another_organization(self):
        first = Organization.objects.create(name="Org One")
        second = Organization.objects.create(name="Org Two")
        Team.objects.create(organization=first, name="Platform")
        team = Team.objects.create(organization=second, name="Platform")
        self.assertEqual(team.status, LifecycleStatus.ACTIVE)
