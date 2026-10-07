"""The agent kill-switch (IRGF agent chain link 06) and the Agent Card."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from apps.a2a.models import A2AAccessMode, A2AAgentCard, A2ATask, DeliveryStatus
from apps.accounts.roles import Role
from apps.agents.models import AIAgent, AutonomyLevel, validate_action_limits
from apps.agents.services import KillSwitch, KillSwitchError
from apps.api.models import APIKey, ServiceAccount
from apps.audit.models import AuditAction, AuditLog
from apps.core.constants import Environment, LifecycleStatus
from apps.gateway.messaging import MessageBlocked, MessageRequest, MessagingService
from apps.gateway.services import AgentInactive, AgentSuspended, GatewayRequest, GatewayService
from apps.mcp_gateway.services import MCPGateway
from apps.notifications.models import Notification, NotificationCategory
from apps.organizations.models import Organization, OrganizationMembership

User = get_user_model()


class KillSwitchFixture(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Kill Switch Org")
        self.developer = self.member("dev@example.com", Role.AI_DEVELOPER)
        self.agent = AIAgent.objects.create(
            organization=self.organization, name="Payout Agent", agent_id="payout-agent",
            environment=Environment.PRODUCTION, owner=self.developer,
        )

    def member(self, email, role):
        user = User.objects.create_user(email=email, password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=user, role=role, is_default=True
        )
        return user


class KillSwitchServiceTests(KillSwitchFixture):
    def test_a_reason_is_required(self):
        with self.assertRaises(KillSwitchError):
            KillSwitch.suspend(self.agent, actor=self.developer, reason="  ")

    def test_suspend_and_resume_are_audited(self):
        KillSwitch.suspend(self.agent, actor=self.developer, reason="Payouts outside limits.")
        self.agent.refresh_from_db()
        self.assertTrue(self.agent.is_suspended)
        self.assertEqual(self.agent.suspended_by, self.developer)

        KillSwitch.resume(self.agent, actor=self.developer, note="Limit bug fixed.")
        self.agent.refresh_from_db()
        self.assertFalse(self.agent.is_suspended)
        actions = set(AuditLog.objects.values_list("action", flat=True))
        self.assertTrue({AuditAction.AGENT_SUSPEND, AuditAction.AGENT_RESUME} <= actions)

    def test_a_service_account_may_suspend_but_only_a_person_may_resume(self):
        monitor = ServiceAccount.objects.create(
            organization=self.organization, name="anomaly-monitor", role=Role.AI_DEVELOPER
        )
        KillSwitch.suspend(self.agent, actor=monitor, reason="Anomaly score 0.97.")
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.suspended_by_label, "anomaly-monitor (service account)")
        self.assertIsNone(self.agent.suspended_by)
        with self.assertRaises(KillSwitchError):
            KillSwitch.resume(self.agent, actor=monitor)

    def test_suspending_twice_is_refused(self):
        KillSwitch.suspend(self.agent, actor=self.developer, reason="First.")
        with self.assertRaises(KillSwitchError):
            KillSwitch.suspend(self.agent, actor=self.developer, reason="Second.")

    def test_the_agents_managers_are_told(self):
        KillSwitch.suspend(self.agent, actor=self.developer, reason="Payouts outside limits.")
        self.assertTrue(
            Notification.objects.filter(category=NotificationCategory.AGENT_SUSPENDED).exists()
        )


class KillSwitchEnforcementTests(KillSwitchFixture):
    def suspend(self):
        KillSwitch.suspend(self.agent, actor=self.developer, reason="Payouts outside limits.")
        self.agent.refresh_from_db()

    def test_the_ai_gateway_refuses_a_suspended_agent(self):
        self.suspend()
        with self.assertRaises(AgentSuspended):
            GatewayService.complete(self.organization, GatewayRequest(agent=self.agent, prompt="Hi"))

    def test_a_stream_is_refused_before_the_first_byte(self):
        self.suspend()
        with self.assertRaises(AgentSuspended):
            next(GatewayService.stream(self.organization, GatewayRequest(agent=self.agent, prompt="Hi")))

    def test_a_retired_agent_is_refused(self):
        self.agent.status = LifecycleStatus.RETIRED
        self.agent.save()
        with self.assertRaises(AgentInactive):
            GatewayService.complete(self.organization, GatewayRequest(agent=self.agent, prompt="Hi"))

    def test_messages_from_a_suspended_agent_are_blocked_and_kept(self):
        recipient = AIAgent.objects.create(
            organization=self.organization, name="Ledger Agent", agent_id="ledger-agent"
        )
        A2AAgentCard.objects.create(
            organization=self.organization, agent=recipient, is_approved=True, access_mode=A2AAccessMode.OPEN,
        )
        self.suspend()
        with self.assertRaises(MessageBlocked) as caught:
            MessagingService.send(
                self.organization,
                MessageRequest(sender=self.agent, recipient=recipient, body="Pay 400"),
            )
        self.assertIn("kill-switch", caught.exception.message)
        self.assertTrue(
            A2ATask.objects.filter(sender=self.agent, status=DeliveryStatus.BLOCKED).exists()
        )

    def test_the_mcp_gateway_refuses_a_suspended_agent_before_anything_else(self):
        self.suspend()
        # The kill-switch is the first access check, ahead of any server or grant.
        reason = MCPGateway._access_denial(None, None, self.agent, None)
        self.assertIn("suspended by its kill-switch", reason)


class KillSwitchViewTests(KillSwitchFixture):
    def test_the_agent_page_suspends_and_resumes(self):
        self.client.force_login(self.developer)
        self.client.post(
            reverse("inventory:agent_suspend", args=[self.agent.pk]),
            {"reason": "Payouts outside limits."},
        )
        self.agent.refresh_from_db()
        self.assertTrue(self.agent.is_suspended)

        page = self.client.get(reverse("inventory:agent_detail", args=[self.agent.pk]))
        self.assertContains(page, "Suspended by its kill-switch")

        self.client.post(reverse("inventory:agent_resume", args=[self.agent.pk]), {"note": "Fixed."})
        self.agent.refresh_from_db()
        self.assertFalse(self.agent.is_suspended)

    def test_a_viewer_cannot_use_the_kill_switch(self):
        viewer = self.member("viewer@example.com", Role.VIEWER)
        self.client.force_login(viewer)
        # AJAX, so the refusal comes back as a status rather than a flash message.
        response = self.client.post(
            reverse("inventory:agent_suspend", args=[self.agent.pk]), {"reason": "No."},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 403)
        self.agent.refresh_from_db()
        self.assertFalse(self.agent.is_suspended)


class KillSwitchApiTests(KillSwitchFixture):
    def setUp(self):
        super().setUp()
        self.monitor = ServiceAccount.objects.create(
            organization=self.organization, name="anomaly-monitor", role=Role.AI_DEVELOPER
        )
        _, self.key = APIKey.generate(self.monitor)

    def test_a_machine_can_suspend_over_the_api(self):
        response = self.client.post(
            reverse("api:agent-suspend", args=[self.agent.pk]),
            {"reason": "Anomaly score 0.97."},
            content_type="application/json",
            HTTP_X_API_KEY=self.key,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["is_suspended"])

    def test_a_machine_cannot_resume(self):
        KillSwitch.suspend(self.agent, actor=self.monitor, reason="Anomaly.")
        response = self.client.post(
            reverse("api:agent-resume", args=[self.agent.pk]), {},
            content_type="application/json", HTTP_X_API_KEY=self.key,
        )
        self.assertEqual(response.status_code, 403)
        self.agent.refresh_from_db()
        self.assertTrue(self.agent.is_suspended)

    def test_a_person_can_resume_over_the_api(self):
        KillSwitch.suspend(self.agent, actor=self.monitor, reason="Anomaly.")
        self.client.force_login(self.developer)
        response = self.client.post(
            reverse("api:agent-resume", args=[self.agent.pk]), {"note": "Checked."},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()["is_suspended"])


class AgentCardTests(SimpleTestCase):
    def test_action_limits_reject_unknown_names(self):
        with self.assertRaises(ValidationError):
            validate_action_limits({"max_spend": 100})
        with self.assertRaises(ValidationError):
            validate_action_limits({"max_actions_per_hour": 0})
        validate_action_limits(
            {"max_value_per_action": "500", "max_actions_per_hour": 30, "currency": "USD",
             "allowed_counterparties": ["ACME Bank"]}
        )

    def test_an_acting_agent_lists_what_its_card_lacks(self):
        agent = AIAgent(name="Payout Agent", autonomy_level=AutonomyLevel.EXECUTE_WITH_APPROVAL)
        self.assertTrue(agent.acts)
        self.assertEqual(len(agent.agent_card_gaps()), 7)

    def test_an_advisory_agent_needs_no_card(self):
        self.assertFalse(AIAgent(name="Researcher", autonomy_level=AutonomyLevel.ADVISORY).acts)
