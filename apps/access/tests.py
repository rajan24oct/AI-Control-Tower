"""People, teams and service accounts, managed in the web UI rather than the admin."""

from __future__ import annotations

import json
import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.agents.models import AIAgent
from apps.api.models import APIKey, ServiceAccount
from apps.audit.models import AuditAction, AuditLog
from apps.organizations.models import Organization, OrganizationMembership, Team, TeamMembership

User = get_user_model()
LINK = re.compile(r"https?://\S+/accounts/set-password/\S+/")


class AccessTestCase(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Acme Insurance")
        self.other = Organization.objects.create(name="Other Corp")
        self.admin = self.member("admin@acme.example", Role.ORG_ADMIN)
        self.client.force_login(self.admin)

    def member(self, email, role, organization=None, **extra):
        user = User.objects.create_user(email=email, password="pw-12345", **extra)
        OrganizationMembership.objects.create(
            organization=organization or self.org, user=user, role=role, is_default=True
        )
        return user

    def membership(self, user, organization=None):
        return OrganizationMembership.objects.get(organization=organization or self.org, user=user)

    def audited(self, action, model):
        return AuditLog.objects.filter(
            action=action, resource_type=model._meta.label, organization=self.org
        ).order_by("-created_at").first()


class MemberTests(AccessTestCase):
    def test_someone_new_is_invited_and_sets_their_own_password(self):
        response = self.client.post(reverse("access:member_create"), {
            "email": "New.Person@acme.example", "first_name": "New", "last_name": "Person",
            "role": Role.AI_DEVELOPER, "send_invitation": "on",
        })
        user = User.objects.get(email="new.person@acme.example")
        self.assertRedirects(response, reverse("access:member_edit", args=[self.membership(user).pk]), fetch_redirect_response=False)
        self.assertFalse(user.has_usable_password())
        self.assertEqual(self.membership(user).role, Role.AI_DEVELOPER)
        self.assertIsNotNone(self.audited(AuditAction.CREATE, OrganizationMembership))

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["new.person@acme.example"])
        link = LINK.search(mail.outbox[0].body).group(0)

        self.client.logout()
        form_url = self.client.get(link)["Location"]
        self.assertContains(self.client.get(form_url), "Set your password")
        response = self.client.post(form_url, {"new_password1": "Correct-Horse-9-battery", "new_password2": "Correct-Horse-9-battery"})
        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertTrue(self.client.login(email="new.person@acme.example", password="Correct-Horse-9-battery"))

    def test_a_used_or_forged_link_is_refused(self):
        response = self.client.get(reverse("accounts:set_password", kwargs={"uidb64": "Zm9v", "token": "nope"}))
        self.assertContains(response, "This link has expired")

    def test_someone_with_an_account_elsewhere_joins_without_an_invitation(self):
        elsewhere = self.member("elsewhere@other.example", Role.VIEWER, organization=self.other)
        response = self.client.post(reverse("access:member_create"), {
            "email": "elsewhere@other.example", "role": Role.AUDITOR, "send_invitation": "on",
        }, follow=True)
        self.assertEqual(self.membership(elsewhere).role, Role.AUDITOR)
        self.assertEqual(User.objects.filter(email="elsewhere@other.example").count(), 1)
        self.assertEqual(mail.outbox, [])
        self.assertContains(response, "sign in as usual")

    def test_a_member_cannot_be_added_twice(self):
        response = self.client.post(reverse("access:member_create"), {"email": "admin@acme.example", "role": Role.VIEWER})
        self.assertContains(response, "Already a member", status_code=400)

    def test_only_a_superuser_makes_super_administrators(self):
        response = self.client.post(reverse("access:member_create"), {"email": "boss@acme.example", "role": Role.SUPER_ADMIN})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(email="boss@acme.example").exists())

    def test_the_last_administrator_cannot_be_demoted_or_remove_themselves(self):
        url = reverse("access:member_edit", args=[self.membership(self.admin).pk])
        response = self.client.post(url, {"role": Role.AI_DEVELOPER})
        self.assertContains(response, "last administrator", status_code=400)
        self.client.post(reverse("access:member_remove", args=[self.membership(self.admin).pk]))
        self.assertEqual(self.membership(self.admin).role, Role.ORG_ADMIN)

    def test_a_role_change_is_audited_as_one_and_teams_follow_the_form(self):
        team = Team.objects.create(organization=self.org, name="Claims")
        developer = self.member("dev@acme.example", Role.AI_DEVELOPER, first_name="Dev")
        self.client.post(reverse("access:member_edit", args=[self.membership(developer).pk]), {
            "role": Role.RISK_MANAGER, "teams": [team.pk], "first_name": "Devi", "last_name": "", "job_title": "",
        })
        developer.refresh_from_db()
        self.assertEqual((self.membership(developer).role, developer.first_name), (Role.RISK_MANAGER, "Devi"))
        self.assertTrue(TeamMembership.objects.filter(team=team, user=developer).exists())
        entry = self.audited(AuditAction.ROLE_CHANGE, OrganizationMembership)
        self.assertEqual((entry.old_value["role"], entry.new_value["role"]), (Role.AI_DEVELOPER, Role.RISK_MANAGER))
        self.assertEqual(entry.new_value["teams"], ["Claims"])

    def test_someone_who_also_belongs_elsewhere_keeps_their_profile_and_password(self):
        shared = self.member("shared@acme.example", Role.VIEWER, first_name="Sam")
        OrganizationMembership.objects.create(organization=self.other, user=shared, role=Role.VIEWER)
        membership = self.membership(shared)
        self.client.post(reverse("access:member_edit", args=[membership.pk]), {"role": Role.VIEWER, "first_name": "Changed"})
        shared.refresh_from_db()
        self.assertEqual(shared.first_name, "Sam")
        self.client.post(reverse("access:member_password_link", args=[membership.pk]))
        self.assertEqual(mail.outbox, [])

    def test_removing_a_member_leaves_their_other_organizations_alone(self):
        shared = self.member("shared@acme.example", Role.VIEWER)
        OrganizationMembership.objects.create(organization=self.other, user=shared, role=Role.VIEWER)
        team = Team.objects.create(organization=self.org, name="Claims")
        TeamMembership.objects.create(team=team, user=shared)
        removed = self.membership(shared).pk
        self.client.post(reverse("access:member_remove", args=[removed]))
        self.assertFalse(OrganizationMembership.objects.filter(organization=self.org, user=shared).exists())
        self.assertTrue(OrganizationMembership.objects.filter(organization=self.other, user=shared).exists())
        self.assertFalse(TeamMembership.objects.filter(user=shared).exists())
        self.assertEqual(self.audited(AuditAction.DELETE, OrganizationMembership).resource_id, str(removed))

    def test_people_are_managed_only_by_holders_of_user_manage(self):
        for role in (Role.VIEWER, Role.GOVERNANCE_MANAGER, Role.PLATFORM_ADMIN):
            user = self.member(f"{role}@acme.example", role)
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("access:members")).status_code, 403)
            self.assertEqual(self.client.get(reverse("access:service_accounts")).status_code, 403)
            self.assertNotContains(self.client.get(reverse("dashboard:home")), reverse("access:members"))

    def test_the_profile_menu_opens_the_in_app_pages_not_the_admin(self):
        response = self.client.get(reverse("dashboard:home"))
        self.assertContains(response, reverse("access:members"))
        self.assertContains(response, reverse("access:service_accounts"))
        self.assertNotContains(response, "/admin/accounts/user/")


class TeamTests(AccessTestCase):
    def test_a_team_keeps_its_members_in_step_with_the_form(self):
        first, second = self.member("a@acme.example", Role.VIEWER), self.member("b@acme.example", Role.VIEWER)
        self.client.post(reverse("access:team_create"), {
            "name": "Claims", "department": "Operations", "status": "active", "members": [first.pk, second.pk],
        })
        team = Team.objects.get(organization=self.org, name="Claims")
        self.assertEqual(team.memberships.count(), 2)
        self.client.post(reverse("access:team_edit", args=[team.pk]), {
            "name": "Claims", "department": "Operations", "status": "active", "members": [first.pk],
        })
        self.assertEqual(list(team.memberships.values_list("user__email", flat=True)), ["a@acme.example"])
        entry = self.audited(AuditAction.CONFIG_CHANGE, Team)
        self.assertEqual(entry.new_value["members"], ["a@acme.example"])

    def test_someone_outside_the_organization_cannot_join_a_team(self):
        outsider = self.member("out@other.example", Role.VIEWER, organization=self.other)
        response = self.client.post(reverse("access:team_create"), {"name": "Claims", "status": "active", "members": [outsider.pk]})
        self.assertEqual(response.status_code, 400)

    def test_a_team_that_owns_something_is_retired_not_deleted(self):
        team = Team.objects.create(organization=self.org, name="Claims")
        agent = AIAgent.objects.create(organization=self.org, name="Triage", agent_id="triage", team=team)
        self.client.post(reverse("access:team_delete", args=[team.pk]))
        self.assertTrue(Team.objects.filter(pk=team.pk).exists())
        agent.team = None
        agent.save(update_fields=["team"])
        self.client.post(reverse("access:team_delete", args=[team.pk]))
        self.assertFalse(Team.objects.filter(pk=team.pk).exists())
        self.assertEqual(self.audited(AuditAction.DELETE, Team).resource_id, str(team.pk))


class ServiceAccountTests(AccessTestCase):
    def test_a_service_account_is_created_bound_to_its_agents(self):
        agent = AIAgent.objects.create(organization=self.org, name="Triage", agent_id="triage")
        response = self.client.post(reverse("access:service_account_create"), {
            "name": "claims-runtime", "external_system": "CrewAI", "role": Role.AI_DEVELOPER,
            "status": "active", "agents": [agent.pk],
        })
        account = ServiceAccount.objects.get(organization=self.org, name="claims-runtime")
        self.assertRedirects(response, reverse("access:service_account_detail", args=[account.pk]), fetch_redirect_response=False)
        self.assertEqual(list(account.agents.all()), [agent])

    def test_a_service_account_is_never_an_administrator(self):
        response = self.client.post(reverse("access:service_account_create"), {
            "name": "all-powerful", "role": Role.ORG_ADMIN, "status": "active",
        })
        self.assertEqual(response.status_code, 400)

    def test_a_key_is_shown_once_works_and_only_its_hash_is_kept(self):
        account = ServiceAccount.objects.create(organization=self.org, name="runtime")
        response = self.client.post(reverse("access:key_issue", args=[account.pk]), {"name": "worker", "expires_in": "30"})
        raw_key = response.context["raw_key"]
        self.assertContains(response, raw_key)
        self.assertEqual(response["Cache-Control"], "no-store")

        key = APIKey.objects.get(service_account=account)
        self.assertTrue(key.verify(raw_key.split("_", 2)[2]))
        self.assertAlmostEqual(key.expires_at, timezone.now() + timedelta(days=30), delta=timedelta(minutes=1))
        entry = self.audited(AuditAction.CREATE, APIKey)
        self.assertEqual(entry.new_value["prefix"], key.prefix)
        self.assertNotIn(raw_key.split("_", 2)[2], json.dumps(entry.new_value))
        self.assertNotContains(self.client.get(reverse("access:service_account_detail", args=[account.pk])), raw_key)

        self.client.logout()
        self.assertEqual(self.client.get("/api/v1/adapters/", HTTP_X_API_KEY=raw_key).status_code, 200)

    def test_a_revoked_key_is_refused(self):
        account = ServiceAccount.objects.create(organization=self.org, name="runtime")
        key, raw_key = APIKey.generate(account)
        self.client.post(reverse("access:key_revoke", args=[key.pk]))
        key.refresh_from_db()
        self.assertTrue(key.is_revoked)
        self.client.logout()
        self.assertIn(self.client.get("/api/v1/adapters/", HTTP_X_API_KEY=raw_key).status_code, (401, 403))

    def test_an_inactive_account_gets_no_keys(self):
        account = ServiceAccount.objects.create(organization=self.org, name="paused", status="inactive")
        self.client.post(reverse("access:key_issue", args=[account.pk]), {"name": "worker"})
        self.assertFalse(APIKey.objects.filter(service_account=account).exists())

    def test_another_organizations_key_cannot_be_revoked(self):
        foreign = ServiceAccount.objects.create(organization=self.other, name="theirs")
        key, _ = APIKey.generate(foreign)
        self.assertEqual(self.client.post(reverse("access:key_revoke", args=[key.pk])).status_code, 404)
        key.refresh_from_db()
        self.assertFalse(key.is_revoked)


class FirstOrganizationTests(TestCase):
    def test_a_fresh_installation_gets_its_first_organization_and_administrator(self):
        self.client.force_login(User.objects.create_superuser(email="root@platform.example", password="pw-12345"))
        self.assertContains(self.client.get(reverse("dashboard:home")), reverse("access:organization_create"))
        self.assertContains(self.client.get(reverse("access:organizations")), "No organizations yet")
        response = self.client.post(reverse("access:organization_create"), {
            "name": "Globex Health", "industry": "Healthcare", "status": "active",
            "admin_email": "Grace@Globex.example", "admin_first_name": "Grace", "admin_last_name": "Hopper",
            "admin_role": Role.ORG_ADMIN, "send_invitation": "on",
        })
        org = Organization.objects.get(name="Globex Health")
        self.assertRedirects(response, reverse("access:organization_detail", args=[org.pk]), fetch_redirect_response=False)
        self.assertEqual(org.slug, "globex-health")

        grace = User.objects.get(email="grace@globex.example")
        membership = OrganizationMembership.objects.get(organization=org, user=grace)
        self.assertEqual((membership.role, membership.is_default), (Role.ORG_ADMIN, True))
        self.assertFalse(grace.has_usable_password())
        self.assertEqual(mail.outbox[0].to, ["grace@globex.example"])
        self.assertIn("Globex Health", mail.outbox[0].subject)
        self.assertEqual(
            sorted(AuditLog.objects.filter(organization=org, action=AuditAction.CREATE).values_list("resource_type", flat=True)),
            [Organization._meta.label, OrganizationMembership._meta.label],
        )
        self.assertContains(self.client.get(reverse("access:organization_detail", args=[org.pk])), "grace@globex.example")

        # From here on, the organization's own administrator runs it.
        self.client.force_login(grace)
        self.assertEqual(self.client.get(reverse("access:members")).status_code, 200)


class SiteAdminTests(AccessTestCase):
    """Site administrators -- superusers -- create organizations and staff any of them."""

    def setUp(self):
        super().setUp()
        self.root = User.objects.create_superuser(email="root@platform.example", password="pw-12345")
        self.client.force_login(self.root)

    def test_an_organization_needs_a_unique_name_and_short_name(self):
        base = {"status": "active", "admin_email": "a@b.example", "admin_role": Role.ORG_ADMIN}
        response = self.client.post(reverse("access:organization_create"), {**base, "name": "ACME insurance"})
        self.assertContains(response, "already has this name", status_code=400)
        response = self.client.post(reverse("access:organization_create"), {
            **base, "name": "Acme Insurance Group", "slug": "acme-insurance",
        })
        self.assertContains(response, "already uses this short name", status_code=400)
        self.assertEqual(Organization.objects.count(), 2)

    def test_an_existing_account_runs_a_new_organization_without_an_invitation(self):
        response = self.client.post(reverse("access:organization_create"), {
            "name": "Initech", "status": "active", "admin_email": "admin@acme.example",
            "admin_role": Role.SUPER_ADMIN, "send_invitation": "on",
        }, follow=True)
        membership = OrganizationMembership.objects.get(organization__name="Initech", user=self.admin)
        self.assertEqual(membership.role, Role.SUPER_ADMIN)
        self.assertFalse(membership.is_default)  # they still land in Acme
        self.assertEqual(mail.outbox, [])
        self.assertContains(response, "sign in as usual")

    def test_only_site_administrators_reach_these_pages(self):
        pages = [
            reverse("access:organizations"), reverse("access:organization_create"),
            reverse("access:organization_detail", args=[self.org.pk]),
            reverse("access:organization_edit", args=[self.org.pk]),
            reverse("access:organization_member_create", args=[self.org.pk]),
            reverse("access:organization_member_edit", args=[self.org.pk, self.membership(self.admin).pk]),
        ]
        chief = self.member("chief@acme.example", Role.SUPER_ADMIN)
        for user in (self.admin, chief):
            self.client.force_login(user)
            for url in pages:
                self.assertEqual(self.client.get(url).status_code, 403, url)
            response = self.client.post(reverse("access:organization_create"), {
                "name": "Sneaky", "status": "active", "admin_email": "x@y.example", "admin_role": Role.ORG_ADMIN,
            })
            self.assertEqual(response.status_code, 403)
            self.assertNotContains(self.client.get(reverse("dashboard:home")), reverse("access:organizations"))
        self.assertFalse(Organization.objects.filter(name="Sneaky").exists())

    def test_the_profile_menu_offers_organizations_to_site_administrators(self):
        self.assertContains(self.client.get(reverse("dashboard:home")), reverse("access:organizations"))

    def test_organizations_without_an_administrator_are_flagged(self):
        response = self.client.get(reverse("access:organizations"))
        counts = {org.name: org.admin_count for org in response.context["organizations"]}
        self.assertEqual(counts, {"Acme Insurance": 1, "Other Corp": 0})
        self.assertContains(response, "No administrator")

    def test_a_site_administrator_staffs_any_organization(self):
        # Acme is the site administrator's active organization; they staff Other Corp.
        pat = self.member("pat@acme.example", Role.VIEWER)
        response = self.client.post(reverse("access:organization_member_create", args=[self.other.pk]), {
            "email": "pat@acme.example", "role": Role.SUPER_ADMIN, "is_default": "on",
        })
        membership = OrganizationMembership.objects.get(organization=self.other, user=pat)
        self.assertRedirects(
            response, reverse("access:organization_member_edit", args=[self.other.pk, membership.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual((membership.role, membership.is_default), (Role.SUPER_ADMIN, True))
        self.assertFalse(self.membership(pat).is_default)
        created = AuditLog.objects.get(
            organization=self.other, action=AuditAction.CREATE, resource_type=OrganizationMembership._meta.label
        )
        self.assertEqual(created.new_value["email"], "pat@acme.example")

        self.client.post(reverse("access:organization_member_create", args=[self.other.pk]), {
            "email": "quinn@other.example", "role": Role.VIEWER,
        })
        quinn = OrganizationMembership.objects.get(organization=self.other, user__email="quinn@other.example")
        self.client.post(reverse("access:organization_member_edit", args=[self.other.pk, quinn.pk]), {"role": Role.APPROVER})
        quinn.refresh_from_db()
        self.assertEqual(quinn.role, Role.APPROVER)
        self.assertTrue(AuditLog.objects.filter(organization=self.other, action=AuditAction.ROLE_CHANGE).exists())

        response = self.client.post(reverse("access:organization_member_remove", args=[self.other.pk, quinn.pk]))
        self.assertRedirects(response, reverse("access:organization_detail", args=[self.other.pk]), fetch_redirect_response=False)
        self.assertFalse(OrganizationMembership.objects.filter(pk=quinn.pk).exists())
        removed = AuditLog.objects.get(organization=self.other, action=AuditAction.DELETE)
        self.assertEqual(removed.resource_id, str(quinn.pk))

    def test_every_organization_keeps_an_administrator_under_site_administration_too(self):
        membership = self.membership(self.admin)
        url = reverse("access:organization_member_edit", args=[self.org.pk, membership.pk])
        self.assertContains(self.client.post(url, {"role": Role.VIEWER}), "last administrator", status_code=400)
        self.client.post(reverse("access:organization_member_remove", args=[self.org.pk, membership.pk]))
        self.assertTrue(OrganizationMembership.objects.filter(pk=membership.pk).exists())

    def test_a_membership_is_reached_only_through_its_own_organization(self):
        url = reverse("access:organization_member_edit", args=[self.other.pk, self.membership(self.admin).pk])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_editing_an_organization_is_audited_and_keeps_its_short_name(self):
        self.client.post(reverse("access:organization_edit", args=[self.org.pk]), {
            "name": "Acme Insurance plc", "slug": "renamed", "industry": "Insurance", "status": "active",
        })
        self.org.refresh_from_db()
        self.assertEqual((self.org.name, self.org.slug), ("Acme Insurance plc", "acme-insurance"))
        entry = AuditLog.objects.filter(organization=self.org, resource_type=Organization._meta.label).latest("created_at")
        self.assertEqual(entry.new_value["name"], "Acme Insurance plc")
