"""Tests for the approval and policy-check APIs (sections 18, 19, 20)."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.roles import Role
from apps.agents.models import AIAgent, AutonomyLevel
from apps.api.models import APIKey, ServiceAccount
from apps.approvals.models import ApprovalRequest, ApprovalRule, ApprovalStatus, TriggerAction
from apps.approvals.services import ApprovalContext, ApprovalService
from apps.core.constants import LifecycleStatus, RiskLevel
from apps.executions.models import Execution, ExecutionStatusChoices
from apps.executions.services import ExecutionService
from apps.models_registry.models import AIModel
from apps.organizations.models import Organization, OrganizationMembership
from apps.policies.models import Policy, PolicyEffect, PolicyType
from apps.providers.models import AIProvider

User = get_user_model()


class ApprovalAPIMixin:
    def build_estate(self):
        self.client = APIClient()
        self.organization = Organization.objects.create(name="Approval API Org")
        self.provider = AIProvider.objects.create(
            organization=self.organization, name="Mock", adapter_key="mock", is_approved=True
        )
        self.model = AIModel.objects.create(
            organization=self.organization, provider=self.provider,
            model_name="Mock Large", model_id="mock-large", approved_for_use=True,
        )
        self.agent = AIAgent.objects.create(
            organization=self.organization,
            name="API Agent",
            agent_id="api-agent",
            provider=self.provider,
            primary_model=self.model,
            autonomy_level=AutonomyLevel.AUTONOMOUS,
            restricted_tools=["send_email"],
        )
        self.account = ServiceAccount.objects.create(
            organization=self.organization, name="engine", role=Role.AI_DEVELOPER
        )
        _, self.raw_key = APIKey.generate(self.account)

    def post(self, url, data):
        return self.client.post(url, data, format="json", HTTP_X_API_KEY=self.raw_key)

    def get(self, url):
        return self.client.get(url, HTTP_X_API_KEY=self.raw_key)


class PolicyCheckAPITests(ApprovalAPIMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.url = reverse("api:policy-check")
        # Approval rules reach the policy endpoint through the Human Approval
        # Policy, which is one of the seeded defaults. Without it the rules
        # still govern the Approval Centre but are not a policy decision.
        Policy.objects.create(
            organization=self.organization,
            name="Human approval rules",
            policy_type=PolicyType.HUMAN_APPROVAL,
            effect=PolicyEffect.REQUIRE_APPROVAL,
            status=LifecycleStatus.ACTIVE,
        )

    def test_no_matching_rule_means_no_approval_needed(self):
        response = self.post(self.url, {"agent": "api-agent", "action": "read"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["allowed"])
        self.assertFalse(body["requires_approval"])
        self.assertEqual(body["policy_ids"], [])

    def test_a_matching_rule_surfaces_through_the_human_approval_policy(self):
        ApprovalRule.objects.create(
            organization=self.organization, name="High autonomy",
            min_autonomy_level=3, severity=RiskLevel.HIGH, status=LifecycleStatus.ACTIVE,
        )
        response = self.post(self.url, {"agent": "api-agent"})
        body = response.json()

        self.assertTrue(body["requires_approval"])
        self.assertTrue(body["allowed"])
        # The decision is attributed to the policy; the reason names the rule
        # that fired inside it.
        self.assertEqual(body["policy_names"], ["Human approval rules"])
        self.assertIn("High autonomy", body["reason"])

    def test_a_restricted_tool_is_caught(self):
        ApprovalRule.objects.create(
            organization=self.organization, name="Restricted tool",
            match_restricted_tool=True, status=LifecycleStatus.ACTIVE,
        )
        allowed = self.post(self.url, {"agent": "api-agent", "tool": "web_search"}).json()
        blocked = self.post(self.url, {"agent": "api-agent", "tool": "send_email"}).json()

        self.assertFalse(allowed["requires_approval"])
        self.assertTrue(blocked["requires_approval"])

    def test_cost_threshold_is_evaluated(self):
        ApprovalRule.objects.create(
            organization=self.organization, name="Expensive",
            cost_threshold=Decimal("5"), status=LifecycleStatus.ACTIVE,
        )
        response = self.post(self.url, {"agent": "api-agent", "estimated_cost": "12.50"})
        self.assertTrue(response.json()["requires_approval"])

    def test_an_unknown_agent_is_tolerated(self):
        response = self.post(self.url, {"agent": "not-a-real-agent"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["requires_approval"])

    def test_it_requires_authentication(self):
        self.assertEqual(
            APIClient().post(self.url, {"agent": "api-agent"}, format="json").status_code, 401
        )


class ApprovalRequestAPITests(ApprovalAPIMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.list_url = reverse("api:approval-request-list")
        self.execution = ExecutionService.record(
            organization=self.organization,
            external_id="run-approval-api",
            agent="api-agent",
            status=ExecutionStatusChoices.RUNNING,
        )

    def test_an_engine_can_raise_a_request(self):
        response = self.post(
            self.list_url,
            {
                "requested_action": "Send a customer email",
                "agent": "api-agent",
                "execution_id": "run-approval-api",
                "reason": "External communication attempted.",
                "action": TriggerAction.EXTERNAL_COMMUNICATION,
            },
        )
        self.assertEqual(response.status_code, 201)

        body = response.json()
        self.assertEqual(body["requested_action"], "Send a customer email")
        self.assertEqual(body["status"], ApprovalStatus.PENDING)
        self.assertEqual(body["agent_name"], "API Agent")
        self.assertEqual(body["execution_id"], "run-approval-api")

    def test_raising_a_request_pauses_the_execution(self):
        self.post(
            self.list_url,
            {"requested_action": "Pause me", "execution_id": "run-approval-api"},
        )
        self.execution.refresh_from_db()
        self.assertEqual(self.execution.status, ExecutionStatusChoices.WAITING_FOR_APPROVAL)

    def test_the_submitting_system_is_recorded(self):
        self.post(self.list_url, {"requested_action": "Track me", "agent": "api-agent"})
        self.assertEqual(ApprovalRequest.objects.get().requested_by_system, "engine")

    def test_matched_rules_are_captured_on_the_request(self):
        ApprovalRule.objects.create(
            organization=self.organization, name="High autonomy",
            min_autonomy_level=3, status=LifecycleStatus.ACTIVE,
        )
        response = self.post(
            self.list_url, {"requested_action": "Gated", "agent": "api-agent"}
        )
        self.assertEqual(response.json()["matched_rules"][0]["name"], "High autonomy")

    def test_a_missing_action_is_rejected(self):
        self.assertEqual(self.post(self.list_url, {"agent": "api-agent"}).status_code, 400)

    def test_an_engine_can_poll_for_the_decision(self):
        created = self.post(
            self.list_url, {"requested_action": "Poll me", "agent": "api-agent"}
        ).json()
        status_url = reverse("api:approval-request-status", args=[created["id"]])

        pending = self.get(status_url).json()
        self.assertEqual(pending["status"], ApprovalStatus.PENDING)
        self.assertFalse(pending["decided"])
        self.assertFalse(pending["approved"])

        approver = User.objects.create_user(email="a@example.com")
        ApprovalService.approve(
            ApprovalRequest.objects.get(pk=created["id"]), user=approver, notes="Go ahead"
        )

        decided = self.get(status_url).json()
        self.assertTrue(decided["decided"])
        self.assertTrue(decided["approved"])
        self.assertEqual(decided["decision_notes"], "Go ahead")

    def test_requests_are_scoped_to_the_callers_organization(self):
        other = Organization.objects.create(name="Rival")
        ApprovalRequest.objects.create(organization=other, requested_action="Theirs")
        self.post(self.list_url, {"requested_action": "Mine", "agent": "api-agent"})

        actions = [row["requested_action"] for row in self.get(self.list_url).json()["results"]]
        self.assertEqual(actions, ["Mine"])

    def test_a_foreign_request_is_not_retrievable(self):
        other = Organization.objects.create(name="Rival Two")
        foreign = ApprovalRequest.objects.create(organization=other, requested_action="Theirs")
        url = reverse("api:approval-request-detail", args=[foreign.pk])
        self.assertEqual(self.get(url).status_code, 404)


class ApprovalAPIPermissionTests(ApprovalAPIMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.list_url = reverse("api:approval-request-list")

    def _login(self, role):
        user = User.objects.create_user(email=f"{role}@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=user, role=role, is_default=True
        )
        client = APIClient()
        client.force_login(user)
        return client

    def test_a_viewer_can_read_requests(self):
        self.assertEqual(self._login(Role.VIEWER).get(self.list_url).status_code, 200)

    def test_a_viewer_cannot_raise_a_request(self):
        response = self._login(Role.VIEWER).post(
            self.list_url, {"requested_action": "Nope"}, format="json"
        )
        self.assertEqual(response.status_code, 403)

    def test_a_developer_can_raise_a_request(self):
        response = self._login(Role.AI_DEVELOPER).post(
            self.list_url, {"requested_action": "Allowed", "agent": "api-agent"}, format="json"
        )
        self.assertEqual(response.status_code, 201)
