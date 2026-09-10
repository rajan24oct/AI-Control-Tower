from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.roles import Role
from apps.api.models import APIKey, ServiceAccount
from apps.organizations.models import Organization, OrganizationMembership
from apps.providers.models import AIProvider

User = get_user_model()


class APIKeyModelTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Key Org")
        self.account = ServiceAccount.objects.create(
            organization=self.organization, name="engine", role=Role.AI_DEVELOPER
        )

    def test_only_a_hash_is_persisted(self):
        api_key, raw_key = APIKey.generate(self.account)
        self.assertNotIn(raw_key, api_key.key_hash)
        self.assertNotEqual(api_key.key_hash, raw_key)
        self.assertTrue(raw_key.startswith("act_"))

    def test_a_key_verifies_against_its_own_secret_only(self):
        api_key, raw_key = APIKey.generate(self.account)
        _, secret = APIKey.split_raw(raw_key)
        self.assertTrue(api_key.verify(secret))
        self.assertFalse(api_key.verify("wrong-secret"))

    def test_malformed_keys_are_rejected_by_the_parser(self):
        for candidate in ["", "nope", "act_only", "xyz_a_b"]:
            self.assertIsNone(APIKey.split_raw(candidate))

    def test_revocation_makes_a_key_unusable(self):
        api_key, _ = APIKey.generate(self.account)
        self.assertTrue(api_key.is_usable)
        api_key.revoke()
        self.assertFalse(api_key.is_usable)

    def test_expiry_makes_a_key_unusable(self):
        from datetime import timedelta

        from django.utils import timezone

        api_key, _ = APIKey.generate(
            self.account, expires_at=timezone.now() - timedelta(hours=1)
        )
        self.assertTrue(api_key.is_expired)
        self.assertFalse(api_key.is_usable)


class ServiceAccountAuthenticationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.organization = Organization.objects.create(name="Auth Org")
        self.other_organization = Organization.objects.create(name="Other Org")

        self.account = ServiceAccount.objects.create(
            organization=self.organization, name="langgraph", role=Role.AI_DEVELOPER
        )
        self.api_key, self.raw_key = APIKey.generate(self.account)

        self.provider = AIProvider.objects.create(organization=self.organization, name="Mine")
        self.foreign_provider = AIProvider.objects.create(
            organization=self.other_organization, name="Theirs"
        )
        self.url = reverse("api:provider-list")

    def test_an_unauthenticated_request_is_refused(self):
        # 401 rather than 403: the API advertises an Api-Key challenge.
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_a_valid_api_key_authenticates(self):
        response = self.client.get(self.url, HTTP_X_API_KEY=self.raw_key)
        self.assertEqual(response.status_code, 200)

    def test_the_authorization_header_form_also_works(self):
        response = self.client.get(self.url, HTTP_AUTHORIZATION=f"Api-Key {self.raw_key}")
        self.assertEqual(response.status_code, 200)

    def test_an_invalid_key_is_rejected(self):
        response = self.client.get(self.url, HTTP_X_API_KEY="act_deadbeef1234_wrong")
        self.assertEqual(response.status_code, 401)

    def test_a_revoked_key_is_rejected(self):
        self.api_key.revoke()
        self.assertEqual(self.client.get(self.url, HTTP_X_API_KEY=self.raw_key).status_code, 401)

    def test_a_service_account_sees_only_its_own_organization(self):
        response = self.client.get(self.url, HTTP_X_API_KEY=self.raw_key)
        names = [row["name"] for row in response.json()["results"]]
        self.assertEqual(names, ["Mine"])

    def test_a_foreign_record_is_not_retrievable_by_id(self):
        url = reverse("api:provider-detail", args=[self.foreign_provider.pk])
        response = self.client.get(url, HTTP_X_API_KEY=self.raw_key)
        self.assertEqual(response.status_code, 404)


class APIPermissionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.organization = Organization.objects.create(name="Perm Org")
        self.url = reverse("api:provider-list")

    def _user_with_role(self, email, role):
        user = User.objects.create_user(email=email, password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=user, role=role, is_default=True
        )
        return user

    def test_a_viewer_can_read(self):
        self.client.force_login(self._user_with_role("viewer@example.com", Role.VIEWER))
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_a_viewer_cannot_create(self):
        self.client.force_login(self._user_with_role("viewer2@example.com", Role.VIEWER))
        response = self.client.post(
            self.url, {"name": "New Provider", "organization": str(self.organization.pk)}
        )
        self.assertEqual(response.status_code, 403)

    def test_a_platform_admin_can_create(self):
        self.client.force_login(self._user_with_role("admin@example.com", Role.PLATFORM_ADMIN))
        response = self.client.post(
            self.url,
            {
                "name": "New Provider",
                "organization": str(self.organization.pk),
                "provider_type": "mock",
                "adapter_key": "mock",
                "authentication_method": "api_key",
                "status": "active",
            },
        )
        self.assertEqual(response.status_code, 201)

    def test_a_user_with_no_organization_is_refused(self):
        orphan = User.objects.create_user(email="orphan@example.com", password="pw-12345")
        self.client.force_login(orphan)
        self.assertEqual(self.client.get(self.url).status_code, 403)


class AdapterCatalogTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        organization = Organization.objects.create(name="Catalog Org")
        user = User.objects.create_user(email="cat@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=organization, user=user, role=Role.VIEWER, is_default=True
        )
        self.client.force_login(user)

    def test_the_catalog_lists_registered_adapters(self):
        response = self.client.get(reverse("api:adapter-catalog"))
        self.assertEqual(response.status_code, 200)

        payload = response.json()
        provider_keys = [entry["key"] for entry in payload["provider"]]
        self.assertIn("mock", provider_keys)
        self.assertIn("generic_rest", provider_keys)

    def test_health_is_public(self):
        APIClient().get(reverse("api:health"))
        response = APIClient().get(reverse("api:health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
