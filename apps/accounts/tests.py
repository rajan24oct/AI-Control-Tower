from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.roles import Perm, Role, role_has_permission
from apps.organizations.models import Organization, OrganizationMembership

User = get_user_model()


class UserModelTests(TestCase):
    def test_email_is_the_username_field(self):
        user = User.objects.create_user(email="a@example.com", password="pw-12345")
        self.assertEqual(user.get_username(), "a@example.com")
        self.assertTrue(user.check_password("pw-12345"))

    def test_creating_a_user_without_an_email_is_rejected(self):
        with self.assertRaises(ValueError):
            User.objects.create_user(email="", password="pw-12345")

    def test_superuser_flags(self):
        user = User.objects.create_superuser(email="root@example.com", password="pw-12345")
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)

    def test_initials_fall_back_to_the_email(self):
        named = User.objects.create_user(email="b@example.com", first_name="Ada", last_name="Lovelace")
        anonymous = User.objects.create_user(email="zz@example.com")
        self.assertEqual(named.initials, "AL")
        self.assertEqual(anonymous.initials, "ZZ")


class RolePermissionMatrixTests(TestCase):
    def test_viewer_can_read_but_not_manage(self):
        self.assertTrue(role_has_permission(Role.VIEWER, Perm.INVENTORY_VIEW))
        self.assertFalse(role_has_permission(Role.VIEWER, Perm.INVENTORY_MANAGE))

    def test_approver_can_decide_but_not_change_inventory(self):
        self.assertTrue(role_has_permission(Role.APPROVER, Perm.APPROVAL_DECIDE))
        self.assertFalse(role_has_permission(Role.APPROVER, Perm.AGENT_MANAGE))

    def test_governance_manager_approves_models_but_does_not_build_them(self):
        self.assertTrue(role_has_permission(Role.GOVERNANCE_MANAGER, Perm.MODEL_APPROVE))
        self.assertFalse(role_has_permission(Role.GOVERNANCE_MANAGER, Perm.AGENT_MANAGE))

    def test_org_admin_holds_every_permission(self):
        for permission in (Perm.AGENT_MANAGE, Perm.COST_MANAGE, Perm.AUDIT_VIEW, Perm.POLICY_MANAGE):
            self.assertTrue(role_has_permission(Role.ORG_ADMIN, permission))


class OrganizationScopedPermissionTests(TestCase):
    def setUp(self):
        self.alpha = Organization.objects.create(name="Alpha")
        self.beta = Organization.objects.create(name="Beta")
        self.user = User.objects.create_user(email="dual@example.com")
        OrganizationMembership.objects.create(
            organization=self.alpha, user=self.user, role=Role.ORG_ADMIN
        )
        OrganizationMembership.objects.create(
            organization=self.beta, user=self.user, role=Role.VIEWER
        )

    def test_permissions_differ_per_organization(self):
        self.assertTrue(self.user.has_org_permission(self.alpha, Perm.AGENT_MANAGE))
        self.assertFalse(self.user.has_org_permission(self.beta, Perm.AGENT_MANAGE))

    def test_no_permission_in_an_unrelated_organization(self):
        other = Organization.objects.create(name="Gamma")
        self.assertFalse(self.user.has_org_permission(other, Perm.INVENTORY_VIEW))

    def test_superuser_bypasses_membership(self):
        root = User.objects.create_superuser(email="root2@example.com", password="pw-12345")
        other = Organization.objects.create(name="Delta")
        self.assertTrue(root.has_org_permission(other, Perm.POLICY_MANAGE))

    def test_default_organization_prefers_the_flagged_membership(self):
        OrganizationMembership.objects.filter(
            user=self.user, organization=self.beta
        ).update(is_default=True)
        self.assertEqual(self.user.default_organization, self.beta)
