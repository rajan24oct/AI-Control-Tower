"""Which agent a gateway caller may act as (IRGF agent chain link 02)."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.a2a.models import A2AAccessMode, A2AAgentCard
from apps.accounts.roles import Role
from apps.agents.models import AIAgent
from apps.api.models import APIKey, ServiceAccount
from apps.core.constants import Environment, HealthStatus, LifecycleStatus
from apps.executions.models import Execution
from apps.models_registry.models import AIModel, ModelType
from apps.organizations.models import Organization, OrganizationMembership
from apps.providers.models import AIProvider

User = get_user_model()


class IdentityBindingTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Identity Org")
        provider = AIProvider.objects.create(
            organization=self.organization, name="Mock", adapter_key="mock", is_approved=True,
            status=LifecycleStatus.ACTIVE, health_status=HealthStatus.HEALTHY,
        )
        AIModel.objects.create(
            organization=self.organization, provider=provider, model_name="Mock Large",
            model_id="mock-large", model_type=ModelType.CHAT, approved_for_use=True,
            status=LifecycleStatus.ACTIVE, context_window=128000,
            input_cost_per_1k=Decimal("0.003"), output_cost_per_1k=Decimal("0.015"),
        )
        self.support = self.agent("Support Agent", "support-agent")
        self.other = self.agent("Research Agent", "research-agent")
        self.claims = self.agent("Claims Agent", "claims-agent", risk_tier=3)

        self.runtime_key = self.key("shared-runtime")
        self.claims_key = self.key("claims-runtime", self.claims)
        self.multi_key = self.key("desk-runtime", self.support, self.other)

    def agent(self, name, agent_id, **extra):
        return AIAgent.objects.create(
            organization=self.organization, name=name, agent_id=agent_id,
            environment=Environment.PRODUCTION, **extra,
        )

    def key(self, name, *agents):
        account = ServiceAccount.objects.create(
            organization=self.organization, name=name, role=Role.AI_DEVELOPER
        )
        account.agents.set(agents)
        return APIKey.generate(account)[1]

    def chat(self, key, **payload):
        return self.client.post(
            reverse("api:gateway-chat"), {"prompt": "Summarise the position.", **payload},
            content_type="application/json", HTTP_X_API_KEY=key,
        )

    def assertRefused(self, response):
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"], "identity_not_bound")

    # -- AI Gateway ---------------------------------------------------------

    def test_an_unbound_key_still_acts_for_tier_1_2_agents(self):
        self.assertEqual(self.chat(self.runtime_key, agent="support-agent").status_code, 200)

    def test_an_unbound_key_is_refused_for_a_tier_3_agent(self):
        self.assertRefused(self.chat(self.runtime_key, agent="claims-agent"))

    def test_a_bound_key_acts_only_as_its_own_agents(self):
        self.assertEqual(self.chat(self.claims_key, agent="claims-agent").status_code, 200)
        self.assertRefused(self.chat(self.claims_key, agent="support-agent"))

    def test_a_key_bound_to_one_agent_acts_as_it_without_naming_it(self):
        self.assertEqual(self.chat(self.claims_key).status_code, 200)
        self.assertTrue(Execution.objects.filter(agent=self.claims).exists())

    def test_a_key_bound_to_several_agents_must_name_one(self):
        self.assertRefused(self.chat(self.multi_key))

    def test_a_bound_key_cannot_slip_through_with_an_unknown_agent(self):
        self.assertRefused(self.chat(self.claims_key, agent="ghost-agent"))

    def test_people_are_not_bound(self):
        person = User.objects.create_user(email="dev@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=person, role=Role.AI_DEVELOPER, is_default=True
        )
        self.client.force_login(person)
        response = self.client.post(
            reverse("api:gateway-chat"), {"prompt": "Hi", "agent": "claims-agent"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)

    # -- MCP Gateway and messaging ------------------------------------------

    def test_the_mcp_endpoint_applies_the_same_rule(self):
        url = reverse("api:mcp-endpoint-agent", args=["claims-agent"])
        body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        refused = self.client.post(
            url, body, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.runtime_key}",
        )
        self.assertEqual(refused.status_code, 403)
        self.assertIn("Tier 3", refused.json()["error"]["message"])

        served = self.client.post(
            url, body, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.claims_key}",
        )
        self.assertEqual(served.status_code, 200)

    def test_the_mcp_rest_catalog_applies_the_same_rule(self):
        url = reverse("api:mcp-tools") + "?agent=claims-agent"
        self.assertRefused(self.client.get(url, HTTP_X_API_KEY=self.runtime_key))
        self.assertEqual(self.client.get(url, HTTP_X_API_KEY=self.claims_key).status_code, 200)

    def test_messaging_checks_the_sending_agent(self):
        A2AAgentCard.objects.create(
            organization=self.organization, agent=self.support, is_approved=True,
            access_mode=A2AAccessMode.OPEN,
        )
        url = reverse("api:gateway-send-message")
        refused = self.client.post(
            url, {"sender": "support-agent", "recipient": "claims-agent", "body": "Hello"},
            content_type="application/json", HTTP_X_API_KEY=self.claims_key,
        )
        self.assertRefused(refused)
        sent = self.client.post(
            url, {"sender": "claims-agent", "recipient": "support-agent", "body": "Hello"},
            content_type="application/json", HTTP_X_API_KEY=self.claims_key,
        )
        self.assertEqual(sent.status_code, 201, sent.content)
