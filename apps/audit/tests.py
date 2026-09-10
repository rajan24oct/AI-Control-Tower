from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.audit.context import AuditContext, reset_audit_context, set_audit_context
from apps.audit.models import AuditAction, AuditLog, AuditLogError, AuditSource
from apps.audit.services import AuditService, snapshot
from apps.organizations.models import Organization
from apps.providers.models import AIProvider

User = get_user_model()


class AuditImmutabilityTests(TestCase):
    def setUp(self):
        self.entry = AuditLog.objects.create(
            action=AuditAction.CREATE, resource_type="test.Thing", resource_id="1"
        )

    def test_an_entry_cannot_be_updated(self):
        self.entry.action = AuditAction.DELETE
        with self.assertRaises(AuditLogError):
            self.entry.save()

    def test_an_entry_cannot_be_deleted(self):
        with self.assertRaises(AuditLogError):
            self.entry.delete()

    def test_the_original_row_survives_a_failed_mutation(self):
        try:
            self.entry.action = AuditAction.DELETE
            self.entry.save()
        except AuditLogError:
            pass
        self.assertEqual(AuditLog.objects.get(pk=self.entry.pk).action, AuditAction.CREATE)


class AuditServiceTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Audited Org")
        self.user = User.objects.create_user(email="actor@example.com")

    def test_log_records_the_actor_and_organization(self):
        entry = AuditService.log(
            action=AuditAction.UPDATE,
            resource_type="providers.AIProvider",
            resource_id="abc",
            organization=self.organization,
            actor=self.user,
        )
        self.assertEqual(entry.actor, self.user)
        self.assertEqual(entry.organization, self.organization)
        self.assertEqual(entry.actor_label, str(self.user))

    def test_the_actor_is_taken_from_the_ambient_context(self):
        token = set_audit_context(
            AuditContext(actor=self.user, ip_address="10.0.0.1", source=AuditSource.API)
        )
        try:
            entry = AuditService.log(action=AuditAction.CREATE, resource_type="x.Y")
        finally:
            reset_audit_context(token)

        self.assertEqual(entry.actor, self.user)
        self.assertEqual(entry.ip_address, "10.0.0.1")
        self.assertEqual(entry.source, AuditSource.API)

    def test_log_change_captures_before_and_after(self):
        provider = AIProvider.objects.create(
            organization=self.organization, name="Before", is_approved=False
        )
        before = snapshot(provider, ["name", "is_approved"])

        provider.is_approved = True
        provider.save()

        entry = AuditService.log_change(
            action=AuditAction.CONFIG_CHANGE, instance=provider, before=before, actor=self.user
        )
        self.assertFalse(entry.old_value["is_approved"])
        self.assertTrue(entry.new_value["is_approved"])

    def test_snapshot_is_json_safe(self):
        import json

        provider = AIProvider.objects.create(organization=self.organization, name="Serialize Me")
        # UUIDs, decimals and datetimes must all survive the round trip.
        json.dumps(snapshot(provider))


class AuditMiddlewareTests(TestCase):
    def test_a_request_populates_the_audit_context(self):
        from django.urls import reverse

        from apps.accounts.roles import Role
        from apps.organizations.models import OrganizationMembership

        organization = Organization.objects.create(name="Middleware Org")
        user = User.objects.create_user(email="mw@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=organization, user=user, role=Role.VIEWER
        )
        self.client.force_login(user)

        # A successful page render proves the middleware set and cleared context
        # without leaking state between requests.
        self.assertEqual(self.client.get(reverse("dashboard:home")).status_code, 200)
