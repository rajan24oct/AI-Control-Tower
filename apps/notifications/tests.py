from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.agents.models import AIAgent
from apps.approvals.models import ApprovalRequest, ApprovalStatus
from apps.approvals.services import ApprovalContext, ApprovalService
from apps.core.constants import HealthStatus, LifecycleStatus, RiskLevel
from apps.costs.models import Budget, BudgetScope
from apps.costs.services import BudgetService
from apps.executions.models import Execution, ExecutionStatusChoices
from apps.models_registry.models import AIModel
from apps.notifications import emitters
from apps.notifications.models import (
    Channel,
    Notification,
    NotificationCategory,
    NotificationPreference,
    NotificationSeverity,
)
from apps.notifications.services import NotificationService
from apps.notifications.tasks import notify_expiring_approvals, notify_overdue_risk_reviews
from apps.organizations.models import Organization, OrganizationMembership
from apps.policies.models import Policy, PolicyEffect, PolicyType
from apps.policies.services import PolicyContext, PolicyEngine
from apps.providers.models import AIProvider
from apps.providers.services import ProviderHealthService
from apps.risks.models import Risk, RiskStatus

User = get_user_model()


class NotificationFixtureMixin:
    def build_estate(self, name="Notify Org"):
        self.organization = Organization.objects.create(name=name)

        self.approver = self._member("approver@example.com", Role.APPROVER)
        self.cost_manager = self._member("finops@example.com", Role.PLATFORM_ADMIN)
        self.governance = self._member("gov@example.com", Role.GOVERNANCE_MANAGER)
        self.viewer = self._member("viewer@example.com", Role.VIEWER)

        self.provider = AIProvider.objects.create(
            organization=self.organization, name="Mock Provider", adapter_key="mock",
            health_status=HealthStatus.HEALTHY, is_approved=True,
        )
        self.model = AIModel.objects.create(
            organization=self.organization, provider=self.provider,
            model_name="Mock Large", model_id="mock-large", approved_for_use=True,
        )
        self.agent = AIAgent.objects.create(
            organization=self.organization, name="Agent", agent_id="agent",
            provider=self.provider, primary_model=self.model,
        )

    def _member(self, email, role):
        user = User.objects.create_user(email=email, password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=user, role=role, is_default=True
        )
        return user


class RoutingTests(NotificationFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()

    def test_approval_notifications_go_to_those_who_can_decide(self):
        recipients = NotificationService.recipients_for(
            self.organization, NotificationCategory.APPROVAL_REQUESTED
        )
        self.assertIn(self.approver, recipients)
        self.assertIn(self.governance, recipients)
        self.assertNotIn(self.viewer, recipients)

    def test_budget_notifications_go_to_cost_managers(self):
        recipients = NotificationService.recipients_for(
            self.organization, NotificationCategory.BUDGET_ALERT
        )
        self.assertIn(self.cost_manager, recipients)
        self.assertNotIn(self.approver, recipients)

    def test_policy_notifications_go_to_governance(self):
        recipients = NotificationService.recipients_for(
            self.organization, NotificationCategory.POLICY_VIOLATION
        )
        self.assertIn(self.governance, recipients)
        self.assertNotIn(self.cost_manager, recipients)

    def test_routing_never_crosses_organizations(self):
        other = Organization.objects.create(name="Rival")
        outsider = User.objects.create_user(email="out@example.com")
        OrganizationMembership.objects.create(
            organization=other, user=outsider, role=Role.ORG_ADMIN
        )
        recipients = NotificationService.recipients_for(
            self.organization, NotificationCategory.APPROVAL_REQUESTED
        )
        self.assertNotIn(outsider, recipients)

    def test_inactive_users_are_not_notified(self):
        self.approver.is_active = False
        self.approver.save()
        recipients = NotificationService.recipients_for(
            self.organization, NotificationCategory.APPROVAL_REQUESTED
        )
        self.assertNotIn(self.approver, recipients)

    def test_no_organization_means_no_recipients(self):
        self.assertEqual(
            NotificationService.recipients_for(None, NotificationCategory.SYSTEM), []
        )


class NotifyTests(NotificationFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()

    def notify(self, **kwargs):
        defaults = {
            "organization": self.organization,
            "category": NotificationCategory.SYSTEM,
            "title": "Something happened",
            "recipients": [self.approver],
        }
        return NotificationService.notify(**{**defaults, **kwargs})

    def test_a_notification_is_created_for_each_recipient(self):
        created = self.notify(recipients=[self.approver, self.governance])
        self.assertEqual(len(created), 2)

    def test_duplicate_recipients_receive_one_notification(self):
        created = self.notify(recipients=[self.approver, self.approver])
        self.assertEqual(len(created), 1)

    def test_the_dedupe_key_prevents_repeats(self):
        self.notify(dedupe_key="thing:1")
        second = self.notify(dedupe_key="thing:1")

        self.assertEqual(second, [])
        self.assertEqual(Notification.objects.count(), 1)

    def test_different_dedupe_keys_both_land(self):
        self.notify(dedupe_key="thing:1")
        self.notify(dedupe_key="thing:2")
        self.assertEqual(Notification.objects.count(), 2)

    def test_an_empty_dedupe_key_does_not_collapse_notifications(self):
        """Not every notification is deduplicated; blanks must not collide."""
        self.notify()
        self.notify()
        self.assertEqual(Notification.objects.count(), 2)

    def test_two_recipients_can_share_a_dedupe_key(self):
        created = self.notify(
            recipients=[self.approver, self.governance], dedupe_key="shared:1"
        )
        self.assertEqual(len(created), 2)

    def test_no_recipients_is_a_no_op(self):
        self.assertEqual(self.notify(recipients=[]), [])

    def test_notifying_never_raises(self):
        """A broken notification must not break the thing that raised it."""
        self.assertEqual(self.notify(recipients=[object()]), [])


class ChannelTests(NotificationFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        mail.outbox = []

    def test_an_email_category_sends_email(self):
        NotificationService.notify(
            organization=self.organization,
            category=NotificationCategory.BUDGET_ALERT,
            title="Budget alert",
            recipients=[self.cost_manager],
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Budget alert", mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].to, [self.cost_manager.email])

    def test_a_non_email_category_stays_in_app(self):
        NotificationService.notify(
            organization=self.organization,
            category=NotificationCategory.PROVIDER_HEALTH,
            title="Provider degraded",
            recipients=[self.cost_manager],
        )
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(Notification.objects.count(), 1)

    def test_delivered_channels_are_recorded(self):
        created = NotificationService.notify(
            organization=self.organization,
            category=NotificationCategory.BUDGET_ALERT,
            title="Budget alert",
            recipients=[self.cost_manager],
        )
        created[0].refresh_from_db()
        self.assertIn(Channel.IN_APP, created[0].delivered_channels)
        self.assertIn(Channel.EMAIL, created[0].delivered_channels)

    def test_a_user_can_turn_email_off(self):
        preference = NotificationService.preference_for(self.cost_manager, self.organization)
        preference.set_channel(NotificationCategory.BUDGET_ALERT, Channel.EMAIL, False)
        preference.save()

        NotificationService.notify(
            organization=self.organization,
            category=NotificationCategory.BUDGET_ALERT,
            title="Budget alert",
            recipients=[self.cost_manager],
        )
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(Notification.objects.count(), 1)

    def test_a_user_can_silence_a_category_entirely(self):
        preference = NotificationService.preference_for(self.cost_manager, self.organization)
        preference.set_channel(NotificationCategory.BUDGET_ALERT, Channel.IN_APP, False)
        preference.set_channel(NotificationCategory.BUDGET_ALERT, Channel.EMAIL, False)
        preference.save()

        created = NotificationService.notify(
            organization=self.organization,
            category=NotificationCategory.BUDGET_ALERT,
            title="Budget alert",
            recipients=[self.cost_manager],
        )
        self.assertEqual(created, [])
        self.assertEqual(Notification.objects.count(), 0)

    def test_defaults_apply_without_a_saved_preference(self):
        preference = NotificationPreference(user=self.approver, organization=self.organization)
        self.assertTrue(preference.wants(NotificationCategory.APPROVAL_REQUESTED, Channel.EMAIL))
        self.assertFalse(preference.wants(NotificationCategory.PROVIDER_HEALTH, Channel.EMAIL))


class EmitterTests(NotificationFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        mail.outbox = []

    def test_a_budget_alert_notifies_cost_managers(self):
        budget = Budget.objects.create(
            organization=self.organization, name="Monthly", scope=BudgetScope.ORGANIZATION,
            amount=Decimal("10.00"),
        )
        Execution.objects.create(
            organization=self.organization, external_id="e1", provider=self.provider,
            cost=Decimal("9.00"), status=ExecutionStatusChoices.COMPLETED,
        )
        alerts = BudgetService.evaluate(budget)

        # Three thresholds were crossed and all three are recorded...
        self.assertEqual(sorted(a.threshold for a in alerts), [50, 75, 90])
        # ...but the person is told once, about the worst of them.
        notification = Notification.objects.get(recipient=self.cost_manager)
        self.assertEqual(notification.category, NotificationCategory.BUDGET_ALERT)
        self.assertIn("90%", notification.title)

    def test_a_repeat_budget_evaluation_does_not_re_notify(self):
        budget = Budget.objects.create(
            organization=self.organization, name="Monthly", scope=BudgetScope.ORGANIZATION,
            amount=Decimal("10.00"),
        )
        Execution.objects.create(
            organization=self.organization, external_id="e1", provider=self.provider,
            cost=Decimal("9.00"), status=ExecutionStatusChoices.COMPLETED,
        )
        BudgetService.evaluate(budget)
        before = Notification.objects.count()
        BudgetService.evaluate(budget)

        self.assertEqual(Notification.objects.count(), before)

    def test_a_policy_denial_notifies_governance(self):
        Policy.objects.create(
            organization=self.organization, name="Prohibited tools",
            policy_type=PolicyType.TOOL_ACCESS, effect=PolicyEffect.DENY,
            severity=RiskLevel.CRITICAL, status=LifecycleStatus.ACTIVE,
            configuration={"blocked_tools": ["transfer_funds"]},
        )
        PolicyEngine.check(
            self.organization,
            PolicyContext(agent=self.agent, tool="transfer_funds"),
            requested_action="Move money",
        )
        notification = Notification.objects.get(recipient=self.governance)

        self.assertEqual(notification.category, NotificationCategory.POLICY_VIOLATION)
        self.assertEqual(notification.severity, NotificationSeverity.CRITICAL)
        self.assertIn("blocked an action", notification.title)

    def test_an_approval_request_notifies_the_assigned_approver_only(self):
        ApprovalService.request(
            organization=self.organization,
            requested_action="Send an email",
            context=ApprovalContext(agent=self.agent),
            assigned_approver=self.approver,
        )
        recipients = set(Notification.objects.values_list("recipient", flat=True))
        self.assertEqual(recipients, {self.approver.pk})

    def test_an_unassigned_request_notifies_everyone_who_can_decide(self):
        ApprovalService.request(
            organization=self.organization,
            requested_action="Send an email",
            context=ApprovalContext(agent=self.agent),
        )
        recipients = set(Notification.objects.values_list("recipient", flat=True))
        self.assertIn(self.approver.pk, recipients)
        self.assertNotIn(self.viewer.pk, recipients)

    def test_a_decision_notifies_the_requester(self):
        request = ApprovalService.request(
            organization=self.organization,
            requested_action="Send an email",
            context=ApprovalContext(agent=self.agent),
            requested_by=self.viewer,
            assigned_approver=self.approver,
        )
        Notification.objects.all().delete()
        ApprovalService.approve(request, user=self.approver, notes="Fine")

        notification = Notification.objects.get(recipient=self.viewer)
        self.assertEqual(notification.category, NotificationCategory.APPROVAL_DECIDED)
        self.assertIn("approved", notification.title)

    def test_a_provider_health_change_notifies_platform_admins(self):
        self.provider.adapter_key = "does-not-exist"
        self.provider.save()
        ProviderHealthService.check(self.provider)

        notification = Notification.objects.get(recipient=self.cost_manager)
        self.assertEqual(notification.category, NotificationCategory.PROVIDER_HEALTH)
        self.assertIn("unhealthy", notification.title)

    def test_a_provider_that_stays_down_does_not_re_notify(self):
        self.provider.adapter_key = "does-not-exist"
        self.provider.save()
        ProviderHealthService.check(self.provider)
        before = Notification.objects.count()
        ProviderHealthService.check(self.provider)

        self.assertEqual(Notification.objects.count(), before)


class SweepTaskTests(NotificationFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()

    def test_overdue_risk_reviews_notify_their_owner(self):
        Risk.objects.create(
            organization=self.organization, title="Overdue risk", owner=self.governance,
            review_date=timezone.now().date() - timedelta(days=2),
            status=RiskStatus.MONITORING,
        )
        self.assertEqual(notify_overdue_risk_reviews(), 1)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.governance, category=NotificationCategory.RISK_REVIEW
            ).exists()
        )

    def test_a_risk_within_its_review_date_is_left_alone(self):
        Risk.objects.create(
            organization=self.organization, title="Fine", owner=self.governance,
            review_date=timezone.now().date() + timedelta(days=30),
            status=RiskStatus.MONITORING,
        )
        self.assertEqual(notify_overdue_risk_reviews(), 0)

    def test_approvals_near_expiry_are_chased(self):
        request = ApprovalService.request(
            organization=self.organization,
            requested_action="Decide me",
            context=ApprovalContext(agent=self.agent),
            assigned_approver=self.approver,
        )
        ApprovalRequest.objects.filter(pk=request.pk).update(
            expires_at=timezone.now() + timedelta(hours=1)
        )
        Notification.objects.all().delete()

        self.assertEqual(notify_expiring_approvals(), 1)
        self.assertIn("expires soon", Notification.objects.get().title)

    def test_an_approval_with_time_left_is_not_chased(self):
        request = ApprovalService.request(
            organization=self.organization,
            requested_action="Plenty of time",
            context=ApprovalContext(agent=self.agent),
            assigned_approver=self.approver,
        )
        ApprovalRequest.objects.filter(pk=request.pk).update(
            expires_at=timezone.now() + timedelta(days=2)
        )
        self.assertEqual(notify_expiring_approvals(), 0)

    def test_a_decided_approval_is_not_chased(self):
        request = ApprovalService.request(
            organization=self.organization,
            requested_action="Already done",
            context=ApprovalContext(agent=self.agent),
            assigned_approver=self.approver,
        )
        ApprovalService.approve(request, user=self.approver)
        ApprovalRequest.objects.filter(pk=request.pk).update(
            expires_at=timezone.now() + timedelta(hours=1)
        )
        self.assertEqual(notify_expiring_approvals(), 0)


class NotificationViewTests(NotificationFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.client.force_login(self.approver)
        self.mine = NotificationService.notify(
            organization=self.organization,
            category=NotificationCategory.SYSTEM,
            title="For me",
            recipients=[self.approver],
        )[0]
        self.theirs = NotificationService.notify(
            organization=self.organization,
            category=NotificationCategory.SYSTEM,
            title="For them",
            recipients=[self.governance],
        )[0]

    def test_the_inbox_shows_only_your_own(self):
        response = self.client.get(reverse("notifications:list"))
        self.assertContains(response, "For me")
        self.assertNotContains(response, "For them")

    def test_marking_one_read(self):
        self.client.post(reverse("notifications:mark_read", args=[self.mine.pk]))
        self.mine.refresh_from_db()
        self.assertTrue(self.mine.is_read)

    def test_you_cannot_mark_someone_elses_notification_read(self):
        self.client.post(reverse("notifications:mark_read", args=[self.theirs.pk]))
        self.theirs.refresh_from_db()
        self.assertFalse(self.theirs.is_read)

    def test_marking_all_read(self):
        self.client.post(reverse("notifications:mark_all_read"))
        self.mine.refresh_from_db()
        self.theirs.refresh_from_db()

        self.assertTrue(self.mine.is_read)
        self.assertFalse(self.theirs.is_read)

    def test_the_bell_reports_the_unread_count(self):
        response = self.client.get(reverse("notifications:bell"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["unread_count"], 1)

    def test_filtering_to_unread_only(self):
        NotificationService.mark_read(self.approver, [self.mine.pk])
        response = self.client.get(reverse("notifications:list"), {"unread": "1"})
        self.assertNotContains(response, "For me")

    def test_preferences_can_be_saved(self):
        response = self.client.post(
            reverse("notifications:preferences"),
            {f"{NotificationCategory.BUDGET_ALERT}:in_app": "on"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)

        preference = NotificationPreference.objects.get(
            user=self.approver, organization=self.organization
        )
        self.assertTrue(preference.wants(NotificationCategory.BUDGET_ALERT, Channel.IN_APP))
        # Unchecked boxes must actually turn things off.
        self.assertFalse(preference.wants(NotificationCategory.BUDGET_ALERT, Channel.EMAIL))

    def test_anonymous_users_are_redirected(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("notifications:list")).status_code, 302)
