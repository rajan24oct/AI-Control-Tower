"""Tests for audit integrity, coverage, access control and export."""

import json
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.urls import reverse

from apps.accounts.roles import Role
from apps.agents.models import AIAgent
from apps.audit.models import AuditAction, AuditLog, AuditLogError, AuditSource
from apps.audit.services import AuditService, diff, snapshot
from apps.organizations.models import Organization, OrganizationMembership
from apps.providers.models import AIProvider

User = get_user_model()


def tamper(entry, **fields):
    """Edit an audit row straight against the database.

    The ORM refuses every mutation route, so simulating a tampered row means
    going around it entirely -- which is exactly the threat the content hash
    exists to detect.
    """
    columns = ", ".join(f"{name} = %s" for name in fields)
    values = [
        json.dumps(value) if isinstance(value, (dict, list)) else value
        for value in fields.values()
    ]
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {AuditLog._meta.db_table} SET {columns} WHERE id = %s",
            [*values, entry.pk.hex if hasattr(entry.pk, "hex") else str(entry.pk)],
        )


class ImmutabilityGuardTests(TestCase):
    def setUp(self):
        self.entry = AuditService.log(action=AuditAction.CREATE, resource_type="x.Y")

    def test_instance_delete_is_refused(self):
        with self.assertRaises(AuditLogError):
            self.entry.delete()

    def test_bulk_delete_is_refused(self):
        """`queryset.delete()` never calls `Model.delete()`."""
        with self.assertRaises(AuditLogError):
            AuditLog.objects.all().delete()

    def test_bulk_update_is_refused(self):
        """`queryset.update()` skips `save()` entirely."""
        with self.assertRaises(AuditLogError):
            AuditLog.objects.all().update(action="forged")

    def test_the_entry_survives_every_attempt(self):
        for attempt in (
            lambda: self.entry.delete(),
            lambda: AuditLog.objects.all().delete(),
            lambda: AuditLog.objects.all().update(action="forged"),
        ):
            with self.assertRaises(AuditLogError):
                attempt()
        self.assertEqual(AuditLog.objects.count(), 1)


class DiffTests(TestCase):
    def test_only_changed_fields_survive(self):
        before, after = diff(
            {"name": "A", "region": "eu", "status": "active"},
            {"name": "B", "region": "eu", "status": "active"},
        )
        self.assertEqual(before, {"name": "A"})
        self.assertEqual(after, {"name": "B"})

    def test_added_and_removed_keys_are_included(self):
        before, after = diff({"a": 1}, {"b": 2})
        self.assertEqual(before, {"a": 1, "b": None})
        self.assertEqual(after, {"a": None, "b": 2})

    def test_identical_snapshots_produce_nothing(self):
        self.assertEqual(diff({"a": 1}, {"a": 1}), ({}, {}))

    def test_housekeeping_timestamps_are_not_reported_as_changes(self):
        """`updated_at` moves on every save and would pollute every entry."""
        before, after = diff(
            {"name": "A", "updated_at": "2026-01-01"},
            {"name": "A", "updated_at": "2026-09-09"},
        )
        self.assertEqual((before, after), ({}, {}))

    def test_none_inputs_are_tolerated(self):
        self.assertEqual(diff(None, None), ({}, {}))


class IntegrityTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Integrity Org")
        self.entry = AuditService.log(
            action=AuditAction.CONFIG_CHANGE,
            resource_type="providers.AIProvider",
            resource_id="abc",
            resource_label="A provider",
            organization=self.organization,
            new_value={"name": "After"},
        )

    def test_a_hash_is_written(self):
        self.assertTrue(self.entry.entry_hash)
        self.assertEqual(len(self.entry.entry_hash), 64)

    def test_a_fresh_entry_verifies(self):
        self.assertTrue(self.entry.verify())

    def test_the_hash_is_stable_across_reloads(self):
        reloaded = AuditLog.objects.get(pk=self.entry.pk)
        self.assertEqual(reloaded.entry_hash, reloaded.compute_hash())
        self.assertTrue(reloaded.verify())

    def test_editing_content_around_the_orm_is_detected(self):
        """The ORM blocks edits; a direct database write does not."""
        tamper(self.entry, action=AuditAction.DELETE)

        tampered = AuditLog.objects.get(pk=self.entry.pk)
        self.assertFalse(tampered.verify())

    def test_editing_a_value_payload_is_detected(self):
        tamper(self.entry, new_value={"name": "Forged"})
        self.assertFalse(AuditLog.objects.get(pk=self.entry.pk).verify())

    def test_editing_the_actor_label_is_detected(self):
        tamper(self.entry, actor_label="somebody else")
        self.assertFalse(AuditLog.objects.get(pk=self.entry.pk).verify())

    def test_two_entries_have_different_hashes(self):
        other = AuditService.log(
            action=AuditAction.CONFIG_CHANGE,
            resource_type="providers.AIProvider",
            resource_id="abc",
            resource_label="A provider",
            organization=self.organization,
            new_value={"name": "After"},
        )
        self.assertNotEqual(self.entry.entry_hash, other.entry_hash)

    def test_a_legacy_entry_without_a_hash_is_not_reported_as_tampered(self):
        tamper(self.entry, entry_hash="")
        legacy = AuditLog.objects.get(pk=self.entry.pk)

        self.assertFalse(legacy.is_verifiable)
        self.assertTrue(legacy.verify())

    def test_entries_remain_immutable_through_the_orm(self):
        self.entry.action = AuditAction.DELETE
        with self.assertRaises(AuditLogError):
            self.entry.save()
        with self.assertRaises(AuditLogError):
            self.entry.delete()


class VerifyCommandTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Verify Org")
        for index in range(3):
            AuditService.log(
                action=AuditAction.CREATE,
                resource_type="x.Y",
                resource_id=str(index),
                organization=self.organization,
            )

    def run_command(self, **kwargs):
        out = StringIO()
        call_command("verify_audit_log", stdout=out, **kwargs)
        return out.getvalue()

    def test_a_clean_trail_reports_no_modifications(self):
        output = self.run_command()
        self.assertIn("3 verified", output)
        self.assertIn("No modified entries found", output)

    def test_a_tampered_entry_is_reported(self):
        target = AuditLog.objects.first()
        tamper(target, action="forged")

        output = self.run_command()
        self.assertIn("1 MODIFIED", output)

    def test_fail_on_tamper_exits_non_zero(self):
        target = AuditLog.objects.first()
        tamper(target, action="forged")

        with self.assertRaises(SystemExit):
            self.run_command(fail_on_tamper=True)

    def test_legacy_entries_are_counted_separately(self):
        target = AuditLog.objects.first()
        tamper(target, entry_hash="")

        output = self.run_command()
        self.assertIn("1 written before hashing", output)
        self.assertIn("2 verified", output)


class AdminCoverageTests(TestCase):
    """Section 29's list of actions that must be captured."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Coverage Org")
        self.admin = User.objects.create_superuser(
            email="root@example.com", password="pw-12345"
        )
        self.client.force_login(self.admin)
        self.provider = AIProvider.objects.create(
            organization=self.organization, name="Before", adapter_key="mock"
        )

    def test_a_provider_change_through_the_admin_is_recorded(self):
        url = reverse("admin:providers_aiprovider_change", args=[self.provider.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        self.client.post(url, self._provider_form(name="After"), follow=True)

        entry = AuditLog.objects.filter(
            resource_type="providers.AIProvider", action=AuditAction.CONFIG_CHANGE
        ).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.old_value["name"], "Before")
        self.assertEqual(entry.new_value["name"], "After")
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual(entry.source, AuditSource.ADMIN)

    def test_only_changed_fields_are_recorded(self):
        url = reverse("admin:providers_aiprovider_change", args=[self.provider.pk])
        self.client.post(url, self._provider_form(name="Renamed"), follow=True)

        entry = AuditLog.objects.filter(action=AuditAction.CONFIG_CHANGE).first()
        self.assertEqual(set(entry.new_value), {"name", "slug"})

    def test_a_role_change_is_recorded_with_its_own_verb(self):
        user = User.objects.create_user(email="member@example.com")
        membership = OrganizationMembership.objects.create(
            organization=self.organization, user=user, role=Role.VIEWER
        )
        url = reverse("admin:organizations_organizationmembership_change", args=[membership.pk])
        self.client.post(
            url,
            {
                "organization": str(self.organization.pk),
                "user": str(user.pk),
                "role": Role.APPROVER,
            },
            follow=True,
        )

        entry = AuditLog.objects.filter(action=AuditAction.ROLE_CHANGE).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.old_value["role"], Role.VIEWER)
        self.assertEqual(entry.new_value["role"], Role.APPROVER)

    def test_a_deletion_is_recorded_before_it_happens(self):
        url = reverse("admin:providers_aiprovider_delete", args=[self.provider.pk])
        self.client.post(url, {"post": "yes"}, follow=True)

        entry = AuditLog.objects.filter(action=AuditAction.DELETE).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.old_value["name"], "Before")

    def _provider_form(self, **overrides):
        form = {
            "organization": str(self.organization.pk),
            "name": self.provider.name,
            "slug": self.provider.slug,
            "description": "",
            "provider_type": self.provider.provider_type,
            "adapter_key": self.provider.adapter_key,
            "endpoint": "",
            "region": "",
            "deployment": "",
            "options": "{}",
            "authentication_method": self.provider.authentication_method,
            "credential_ref": "",
            "status": self.provider.status,
        }
        form.update(overrides)
        if "name" in overrides:
            from django.utils.text import slugify

            form["slug"] = slugify(overrides["name"])
        return form


class AuditViewTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Audit View Org")
        self.other_org = Organization.objects.create(name="Rival Audit")

        self.auditor = self._member("auditor@example.com", Role.AUDITOR)
        self.developer = self._member("dev@example.com", Role.AI_DEVELOPER)

        self.entry = AuditService.log(
            action=AuditAction.CONFIG_CHANGE,
            resource_type="providers.AIProvider",
            resource_label="Visible provider",
            organization=self.organization,
            old_value={"name": "Old"},
            new_value={"name": "New"},
        )
        self.foreign = AuditService.log(
            action=AuditAction.CONFIG_CHANGE,
            resource_type="providers.AIProvider",
            resource_label="Foreign provider",
            organization=self.other_org,
        )

    def _member(self, email, role):
        user = User.objects.create_user(email=email, password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=user, role=role, is_default=True
        )
        return user

    def test_an_auditor_can_read_the_trail(self):
        self.client.force_login(self.auditor)
        response = self.client.get(reverse("audit:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Visible provider")

    def test_the_trail_is_organization_scoped(self):
        self.client.force_login(self.auditor)
        response = self.client.get(reverse("audit:list"))
        self.assertNotContains(response, "Foreign provider")

    def test_a_role_without_audit_view_is_refused(self):
        """A developer can build agents but must not read the trail."""
        self.client.force_login(self.developer)
        self.assertEqual(self.client.get(reverse("audit:list")).status_code, 403)

    def test_a_foreign_entry_returns_404(self):
        self.client.force_login(self.auditor)
        response = self.client.get(reverse("audit:detail", args=[self.foreign.pk]))
        self.assertEqual(response.status_code, 404)

    def test_the_detail_page_shows_the_change(self):
        self.client.force_login(self.auditor)
        response = self.client.get(reverse("audit:detail", args=[self.entry.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["verified"])
        self.assertEqual(response.context["changes"][0]["before"], "Old")
        self.assertEqual(response.context["changes"][0]["after"], "New")

    def test_a_modified_entry_is_flagged_in_the_ui(self):
        tamper(self.entry, action="forged")
        self.client.force_login(self.auditor)

        response = self.client.get(reverse("audit:detail", args=[self.entry.pk]))
        self.assertFalse(response.context["verified"])
        self.assertContains(response, "altered outside the platform")

    def test_filtering_by_action(self):
        AuditService.log(
            action=AuditAction.LOGIN, resource_type="accounts.User",
            resource_label="Someone signed in", organization=self.organization,
        )
        self.client.force_login(self.auditor)

        response = self.client.get(reverse("audit:list"), {"action": AuditAction.LOGIN})
        self.assertContains(response, "Someone signed in")
        self.assertNotContains(response, "Visible provider")

    def test_filter_options_are_deduplicated(self):
        """Default ordering leaks created_at into the SELECT and breaks DISTINCT."""
        for _ in range(3):
            AuditService.log(
                action=AuditAction.LOGIN, resource_type="accounts.User",
                organization=self.organization,
            )
        self.client.force_login(self.auditor)
        response = self.client.get(reverse("audit:list"))

        actions = response.context["actions"]
        resources = response.context["resource_types"]
        self.assertEqual(len(actions), len(set(actions)))
        self.assertEqual(len(resources), len(set(resources)))
        self.assertEqual(actions.count(AuditAction.LOGIN), 1)

    def test_filter_options_are_organization_scoped(self):
        self.client.force_login(self.auditor)
        response = self.client.get(reverse("audit:list"))
        # The rival org's entry shares a resource type, but nothing unique to
        # it should leak into the options.
        self.assertNotIn("accounts.User", response.context["resource_types"])

    def test_search_matches_the_resource_label(self):
        self.client.force_login(self.auditor)
        response = self.client.get(reverse("audit:list"), {"q": "Visible"})
        self.assertContains(response, "Visible provider")

    def test_anonymous_users_are_redirected(self):
        self.assertEqual(self.client.get(reverse("audit:list")).status_code, 302)


class AuditExportTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Export Org")
        self.other_org = Organization.objects.create(name="Rival Export")
        self.auditor = User.objects.create_user(email="ex@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.auditor,
            role=Role.AUDITOR, is_default=True,
        )
        AuditService.log(
            action=AuditAction.APPROVE, resource_type="approvals.ApprovalRequest",
            resource_label="APR-0001", organization=self.organization,
        )
        AuditService.log(
            action=AuditAction.APPROVE, resource_type="approvals.ApprovalRequest",
            resource_label="THEIRS", organization=self.other_org,
        )
        self.client.force_login(self.auditor)

    def export(self, **params):
        response = self.client.get(reverse("audit:export"), params)
        return response, response.content.decode("utf-8")

    def test_the_export_is_a_csv_attachment(self):
        response, _ = self.export()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("attachment; filename=", response["Content-Disposition"])

    def test_it_contains_the_entries_and_a_header(self):
        _, body = self.export()
        self.assertIn("timestamp,organization,actor", body)
        self.assertIn("APR-0001", body)

    def test_it_is_organization_scoped(self):
        _, body = self.export()
        self.assertNotIn("THEIRS", body)

    def test_it_honours_the_active_filters(self):
        AuditService.log(
            action=AuditAction.LOGIN, resource_type="accounts.User",
            resource_label="A-LOGIN", organization=self.organization,
        )
        _, body = self.export(action=AuditAction.APPROVE)

        self.assertIn("APR-0001", body)
        self.assertNotIn("A-LOGIN", body)

    def test_it_reports_integrity_per_row(self):
        _, body = self.export()
        self.assertIn("verified", body)

    def test_a_role_without_audit_view_cannot_export(self):
        developer = User.objects.create_user(email="dev2@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=developer,
            role=Role.AI_DEVELOPER, is_default=True,
        )
        self.client.force_login(developer)
        self.assertEqual(self.client.get(reverse("audit:export")).status_code, 403)
