"""Onboarding in the web UI: every registry and governance record, without the admin."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.roles import Role
from apps.agents.models import AIAgent
from apps.applications_registry.models import AIApplication
from apps.approvals.models import ApprovalRule
from apps.audit.models import AuditAction, AuditLog, AuditSource
from apps.costs.models import Budget
from apps.mcp_gateway.models import MCPAccessGrant, MCPServer
from apps.models_registry.models import AIModel
from apps.organizations.models import Organization, OrganizationMembership
from apps.policies.models import Policy
from apps.providers.models import AIProvider
from apps.risks.models import Risk
from apps.routing.models import RoutingPolicy

User = get_user_model()


class OnboardingTestCase(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Acme Insurance")
        self.other = Organization.objects.create(name="Other Corp")
        self.admin = self.member("admin@acme.example", Role.ORG_ADMIN)
        self.client.force_login(self.admin)
        self.provider = AIProvider.objects.create(
            organization=self.org, name="Acme Cloud", adapter_key="mock", is_approved=True
        )
        self.model = AIModel.objects.create(
            organization=self.org, provider=self.provider, model_name="Acme LLM", model_id="acme-llm",
            approved_for_use=True,
        )

    def member(self, email, role, organization=None):
        user = User.objects.create_user(email=email, password="pw-12345")
        OrganizationMembership.objects.create(
            organization=organization or self.org, user=user, role=role, is_default=True
        )
        return user

    def as_role(self, role):
        user = self.member(f"{role}@acme.example", role)
        self.client.force_login(user)
        return user

    def audited(self, action, model) -> AuditLog | None:
        return AuditLog.objects.filter(
            action=action, resource_type=model._meta.label, organization=self.org
        ).order_by("-created_at").first()

    def agent(self, name, **extra):
        return AIAgent.objects.create(
            organization=self.org, name=name, agent_id=name.lower().replace(" ", "-"),
            provider=self.provider, primary_model=self.model, **extra,
        )


class HubAndNavigationTests(OnboardingTestCase):
    def test_the_hub_walks_the_steps_with_create_buttons_for_an_administrator(self):
        response = self.client.get(reverse("onboarding:hub"))
        self.assertEqual(response.status_code, 200)
        for title in ("Platform providers", "Agents", "MCP servers", "Classify and authorize"):
            self.assertContains(response, title)
        self.assertContains(response, reverse("inventory:agent_create"))
        self.assertContains(response, reverse("policies:create"))

    def test_a_viewer_sees_the_hub_but_no_way_to_add(self):
        self.as_role(Role.VIEWER)
        response = self.client.get(reverse("onboarding:hub"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse("inventory:agent_create"))
        self.assertContains(response, "can view the estate but not add to it")

    def test_the_header_offers_only_what_the_role_may_create(self):
        self.as_role(Role.AI_DEVELOPER)
        response = self.client.get(reverse("onboarding:hub"))
        self.assertContains(response, reverse("inventory:agent_create"))
        self.assertNotContains(response, reverse("routing:create"))
        self.assertNotContains(response, reverse("policies:create"))

    def test_detail_pages_edit_in_the_app_not_the_admin(self):
        agent = self.agent("Claims Helper")
        response = self.client.get(reverse("inventory:agent_detail", args=[agent.pk]))
        self.assertContains(response, reverse("inventory:agent_edit", args=[agent.pk]))
        self.assertNotContains(response, "/admin/agents/aiagent/")


class ProviderTests(OnboardingTestCase):
    PAYLOAD = {
        "name": "Northwind Inference", "provider_type": "custom_rest", "adapter_key": "mock",
        "authentication_method": "api_key", "credential_ref": "NORTHWIND_KEY", "status": "active", "options": "",
    }

    def test_onboarding_a_provider_records_it_and_audits_it_as_a_ui_change(self):
        response = self.client.post(reverse("inventory:provider_create"), self.PAYLOAD)
        provider = AIProvider.objects.get(organization=self.org, name="Northwind Inference")
        self.assertRedirects(response, reverse("inventory:provider_detail", args=[provider.pk]), fetch_redirect_response=False)
        self.assertFalse(provider.is_approved)
        entry = self.audited(AuditAction.CREATE, AIProvider)
        self.assertEqual(entry.source, AuditSource.UI)
        self.assertEqual(entry.actor, self.admin)

    def test_a_pasted_secret_is_refused_as_a_credential_reference(self):
        response = self.client.post(reverse("inventory:provider_create"), {**self.PAYLOAD, "credential_ref": "sk-live-1234"})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "never the credential itself", status_code=400)
        self.assertFalse(AIProvider.objects.filter(name="Northwind Inference").exists())

    def test_names_are_unique_within_an_organization_only(self):
        AIProvider.objects.create(organization=self.other, name="Northwind Inference")
        self.assertEqual(self.client.post(reverse("inventory:provider_create"), self.PAYLOAD).status_code, 302)
        response = self.client.post(reverse("inventory:provider_create"), self.PAYLOAD)
        self.assertContains(response, "already uses this name", status_code=400)

    def test_a_viewer_cannot_open_the_form(self):
        self.as_role(Role.VIEWER)
        self.assertEqual(self.client.get(reverse("inventory:provider_create")).status_code, 403)

    def test_approval_is_a_governance_decision_not_the_registrars(self):
        provider = AIProvider.objects.create(organization=self.org, name="Pending", adapter_key="mock")
        url = reverse("inventory:provider_approval", args=[provider.pk])
        self.as_role(Role.PLATFORM_ADMIN)
        self.client.post(url, {"decision": "approve"})
        provider.refresh_from_db()
        self.assertFalse(provider.is_approved)

        self.as_role(Role.GOVERNANCE_MANAGER)
        self.client.post(url, {"decision": "approve"})
        provider.refresh_from_db()
        self.assertTrue(provider.is_approved)
        self.assertIsNotNone(self.audited(AuditAction.APPROVE, AIProvider))

    def test_a_health_check_runs_from_the_page(self):
        self.client.post(reverse("inventory:provider_health", args=[self.provider.pk]))
        self.provider.refresh_from_db()
        self.assertIsNotNone(self.provider.last_health_check)


class ModelTests(OnboardingTestCase):
    def payload(self, **overrides):
        return {
            "provider": self.provider.pk, "model_name": "Acme Mini", "model_id": "acme-mini", "model_type": "chat",
            "version": "1", "capabilities": ["supports_chat"], "input_cost_per_1k": "0.0010",
            "output_cost_per_1k": "0.0020", "currency": "usd", "risk_classification": "low", "status": "active",
            **overrides,
        }

    def test_a_model_is_registered_unapproved_with_its_prices(self):
        self.client.post(reverse("inventory:model_create"), self.payload())
        model = AIModel.objects.get(organization=self.org, model_id="acme-mini")
        self.assertFalse(model.approved_for_use)
        self.assertEqual(model.currency, "USD")
        self.assertEqual(model.capabilities, ["supports_chat"])

    def test_another_organizations_provider_cannot_be_chosen(self):
        foreign = AIProvider.objects.create(organization=self.other, name="Foreign")
        response = self.client.post(reverse("inventory:model_create"), self.payload(provider=foreign.pk))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(AIModel.objects.filter(model_id="acme-mini").exists())

    def test_the_same_model_id_and_version_cannot_be_registered_twice(self):
        response = self.client.post(reverse("inventory:model_create"), self.payload(model_id="acme-llm", version=""))
        self.assertContains(response, "already has this model ID", status_code=400)

    def test_approving_for_use_needs_model_approve(self):
        model = AIModel.objects.create(organization=self.org, provider=self.provider, model_name="New", model_id="new")
        url = reverse("inventory:model_approval", args=[model.pk])
        self.as_role(Role.AI_DEVELOPER)
        self.assertEqual(self.client.post(url, {"decision": "approve"}).status_code, 302)
        model.refresh_from_db()
        self.assertFalse(model.approved_for_use)

        self.as_role(Role.GOVERNANCE_MANAGER)
        self.client.post(url, {"decision": "approve"})
        model.refresh_from_db()
        self.assertTrue(model.approved_for_use)
        self.assertIsNotNone(self.audited(AuditAction.MODEL_APPROVAL, AIModel))


class AgentTests(OnboardingTestCase):
    def payload(self, **overrides):
        return {
            "name": "Claims Helper", "agent_id": "", "framework": "langgraph", "adapter_key": "mock",
            "provider": self.provider.pk, "primary_model": self.model.pk, "environment": "production",
            "risk_level": "moderate", "autonomy_level": "2", "data_classification": "confidential",
            "requires_human_approval": "on", "allowed_tools": "claims_db, policy_lookup",
            "restricted_tools": "approve_payout", "limit_value_per_action": "100", "limit_actions_per_hour": "400",
            "limit_currency": "usd", "kill_switch_triggers": ["action_limit_breach"], "status": "active",
            "options": "", **overrides,
        }

    def test_an_agent_is_onboarded_with_its_agent_card(self):
        response = self.client.post(reverse("inventory:agent_create"), self.payload())
        agent = AIAgent.objects.get(organization=self.org, name="Claims Helper")
        self.assertRedirects(response, reverse("inventory:agent_detail", args=[agent.pk]), fetch_redirect_response=False)
        self.assertEqual(agent.agent_id, "claims-helper")
        self.assertEqual(agent.allowed_tools, ["claims_db", "policy_lookup"])
        self.assertEqual(
            agent.action_limits, {"max_value_per_action": 100, "max_actions_per_hour": 400, "currency": "USD"}
        )
        self.assertEqual(agent.kill_switch_triggers, ["action_limit_breach"])

    def test_a_tool_cannot_be_both_allowed_and_restricted(self):
        response = self.client.post(reverse("inventory:agent_create"), self.payload(restricted_tools="claims_db"))
        self.assertContains(response, "Both allowed and restricted", status_code=400)

    def test_the_primary_model_must_come_from_the_agents_provider(self):
        elsewhere = AIProvider.objects.create(organization=self.org, name="Elsewhere", adapter_key="mock")
        response = self.client.post(reverse("inventory:agent_create"), self.payload(provider=elsewhere.pk))
        self.assertContains(response, "is served by", status_code=400)

    def test_an_agent_id_already_reported_against_is_refused(self):
        self.agent("Claims Helper")
        response = self.client.post(reverse("inventory:agent_create"), self.payload())
        self.assertContains(response, "already reports as", status_code=400)

    def test_the_agent_id_cannot_change_and_an_edit_is_audited_as_a_diff(self):
        agent = self.agent("Claims Helper")
        self.client.post(
            reverse("inventory:agent_edit", args=[agent.pk]),
            self.payload(agent_id="renamed", autonomy_level="3"),
        )
        agent.refresh_from_db()
        self.assertEqual(agent.agent_id, "claims-helper")
        self.assertEqual(agent.autonomy_level, 3)
        entry = self.audited(AuditAction.CONFIG_CHANGE, AIAgent)
        self.assertEqual(entry.new_value["autonomy_level"], 3)
        self.assertNotIn("agent_id", entry.new_value)

    def test_an_ai_developer_may_onboard_agents_but_not_providers(self):
        self.as_role(Role.AI_DEVELOPER)
        self.assertEqual(self.client.get(reverse("inventory:agent_create")).status_code, 200)
        self.assertEqual(self.client.get(reverse("inventory:provider_create")).status_code, 403)


class ApplicationTests(OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.front, self.back, self.outsider = self.agent("Front"), self.agent("Back"), self.agent("Outsider")

    def payload(self, **overrides):
        return {
            "name": "Claims Portal", "environment": "production", "criticality": "high",
            "data_classification": "confidential", "risk_level": "moderate", "status": "active",
            "agents": [self.front.pk, self.back.pk], "link_dependencies": "on",
            "delegation_0_from": "front", "delegation_0_to": "back", "delegation_0_authority": "caller",
            **overrides,
        }

    def test_an_application_links_its_agents_their_dependencies_and_its_delegations(self):
        self.client.post(reverse("inventory:application_create"), self.payload())
        application = AIApplication.objects.get(organization=self.org, name="Claims Portal")
        self.assertEqual(set(application.agents.all()), {self.front, self.back})
        self.assertIn(self.model, application.ai_models.all())
        self.assertIn(self.provider, application.providers.all())
        self.assertEqual(application.delegations, [{"from": "front", "to": "back", "authority": "caller"}])

    def test_a_delegation_can_only_name_the_applications_own_agents(self):
        response = self.client.post(reverse("inventory:application_create"), self.payload(delegation_0_to="outsider"))
        self.assertContains(response, "Not an agent of this application", status_code=400)


class GatewayAndGovernanceTests(OnboardingTestCase):
    def test_a_routing_policy_cannot_allow_and_block_the_same_provider(self):
        payload = {
            "name": "Cheap chat", "strategy": "cost", "priority": "50", "status": "active", "model_type": "chat",
            "fallback_depth": "2", "require_approved_models": "on",
            "allowed_providers": [self.provider.pk], "blocked_providers": [self.provider.pk],
        }
        response = self.client.post(reverse("routing:create"), payload)
        self.assertContains(response, "Both allowed and blocked", status_code=400)
        del payload["blocked_providers"]
        self.client.post(reverse("routing:create"), payload)
        policy = RoutingPolicy.objects.get(organization=self.org, name="Cheap chat")
        self.assertEqual(list(policy.allowed_providers.all()), [self.provider])

    def test_routing_is_the_platform_teams_call(self):
        self.as_role(Role.AI_DEVELOPER)
        self.assertEqual(self.client.get(reverse("routing:create")).status_code, 403)

    def policy_payload(self, **overrides):
        return {
            "name": "No payouts at Tier 3-4", "policy_type": "tool_access", "effect": "deny", "severity": "high",
            "priority": "20", "status": "active", "reference": "EU AI Act Art. 14",
            "configuration": '{"blocked_tools": ["approve_payout"], "environments": ["production"]}',
            "tiers": ["3", "4"], **overrides,
        }

    def test_a_control_policy_carries_its_settings_and_tiers(self):
        self.client.post(reverse("policies:create"), self.policy_payload())
        policy = Policy.objects.get(organization=self.org, name="No payouts at Tier 3-4")
        self.assertEqual(
            policy.configuration,
            {"blocked_tools": ["approve_payout"], "environments": ["production"], "tiers": [3, 4]},
        )
        self.assertEqual(policy.reference, "EU AI Act Art. 14")

    def test_settings_another_type_owns_are_refused(self):
        response = self.client.post(reverse("policies:create"), self.policy_payload(configuration='{"max_autonomy_level": 3}'))
        self.assertContains(response, "Unknown setting", status_code=400)

    def test_a_wrongly_typed_setting_is_refused_before_it_reaches_the_gateways(self):
        response = self.client.post(
            reverse("policies:create"),
            self.policy_payload(policy_type="agent_autonomy", configuration='{"max_autonomy_level": "three"}'),
        )
        self.assertContains(response, "is a whole number", status_code=400)

    def test_editing_a_policy_is_audited_as_a_policy_change(self):
        self.client.post(reverse("policies:create"), self.policy_payload())
        policy = Policy.objects.get(organization=self.org, name="No payouts at Tier 3-4")
        self.client.post(reverse("policies:edit", args=[policy.pk]), self.policy_payload(effect="warn"))
        entry = self.audited(AuditAction.POLICY_CHANGE, Policy)
        # What changed, and when the policy's substance last moved -- nothing else.
        self.assertEqual((entry.old_value["effect"], entry.new_value["effect"]), ("deny", "warn"))
        self.assertEqual(set(entry.new_value), {"effect", "content_changed_at"})

    def test_an_approval_rule_needs_a_condition(self):
        payload = {"name": "Payouts need a person", "severity": "high", "priority": "10", "status": "active"}
        response = self.client.post(reverse("approvals:rule_create"), payload)
        self.assertContains(response, "Set at least one condition", status_code=400)
        self.client.post(reverse("approvals:rule_create"), {**payload, "actions": ["financial_transaction"], "tiers": ["4"]})
        rule = ApprovalRule.objects.get(organization=self.org, name="Payouts need a person")
        self.assertEqual((rule.actions, rule.tiers), (["financial_transaction"], [4]))

    def test_inactive_rules_stay_listed_so_they_can_be_switched_back_on(self):
        rule = ApprovalRule.objects.create(organization=self.org, name="Dormant rule", status="inactive", tools=["x"])
        response = self.client.get(reverse("approvals:centre"))
        self.assertContains(response, reverse("approvals:rule_edit", args=[rule.pk]))

    def test_a_risk_is_numbered_and_scored_and_residual_needs_both_scores(self):
        payload = {
            "title": "Mis-routed urgent claims", "risk_category": "operational", "status": "identified",
            "likelihood": "4", "impact": "4", "residual_likelihood": "2",
        }
        response = self.client.post(reverse("risks:create"), payload)
        self.assertContains(response, "on both likelihood and impact", status_code=400)
        self.client.post(reverse("risks:create"), {**payload, "residual_impact": "2"})
        risk = Risk.objects.get(organization=self.org, title="Mis-routed urgent claims")
        self.assertEqual((risk.risk_id, risk.inherent_risk_score, risk.residual_risk_score), ("RISK-0001", 16, 4))

    def test_the_risk_register_is_the_risk_functions(self):
        self.as_role(Role.AI_DEVELOPER)
        self.assertEqual(self.client.get(reverse("risks:create")).status_code, 403)
        self.as_role(Role.RISK_MANAGER)
        self.assertEqual(self.client.get(reverse("risks:create")).status_code, 200)

    def test_a_budget_counts_against_the_one_thing_its_scope_names(self):
        agent = self.agent("Claims Helper")
        payload = {
            "name": "Claims agent", "scope": "agent", "amount": "50", "currency": "usd", "period": "monthly",
            "starts_on": "2026-09-01", "enforcement": "advisory", "status": "active", "alert_thresholds": "",
        }
        response = self.client.post(reverse("costs:budget_create"), payload)
        self.assertContains(response, "Choose the agent", status_code=400)
        from apps.organizations.models import Team

        team = Team.objects.create(organization=self.org, name="Claims")
        self.client.post(reverse("costs:budget_create"), {**payload, "agent": agent.pk, "team": team.pk})
        budget = Budget.objects.get(organization=self.org, name="Claims agent")
        self.assertEqual((budget.agent, budget.team, budget.currency), (agent, None, "USD"))
        self.assertEqual(budget.alert_thresholds, [50, 75, 90, 100])


class MCPTests(OnboardingTestCase):
    PAYLOAD = {
        "name": "GitHub", "slug": "", "transport": "streamable_http", "endpoint": "", "auth_method": "bearer",
        "credential_ref": "GITHUB_MCP_TOKEN", "data_classification": "internal", "risk_level": "moderate",
        "access_mode": "restricted", "status": "active", "options": "",
    }

    def test_an_http_server_needs_an_endpoint_and_gets_a_prefix_from_its_name(self):
        response = self.client.post(reverse("mcp_gateway:server_create"), self.PAYLOAD)
        self.assertContains(response, "An HTTP transport needs an endpoint", status_code=400)
        self.client.post(reverse("mcp_gateway:server_create"), {**self.PAYLOAD, "endpoint": "https://mcp.example/github"})
        server = MCPServer.objects.get(organization=self.org, name="GitHub")
        self.assertEqual(server.slug, "github")
        self.assertFalse(server.is_approved)

    def test_the_tool_prefix_cannot_change_once_registered(self):
        server = MCPServer.objects.create(organization=self.org, name="GitHub", endpoint="https://mcp.example/github")
        self.client.post(
            reverse("mcp_gateway:server_edit", args=[server.pk]),
            {**self.PAYLOAD, "endpoint": "https://mcp.example/gh", "slug": "renamed"},
        )
        server.refresh_from_db()
        self.assertEqual((server.slug, server.endpoint), ("github", "https://mcp.example/gh"))

    def test_granting_and_revoking_access_is_governances_decision(self):
        server = MCPServer.objects.create(organization=self.org, name="GitHub", endpoint="https://mcp.example/github")
        agent = self.agent("Claims Helper")
        url = reverse("mcp_gateway:grant_create", args=[server.pk])

        self.as_role(Role.PLATFORM_ADMIN)
        self.assertEqual(self.client.get(url).status_code, 403)

        self.as_role(Role.GOVERNANCE_MANAGER)
        self.client.post(url, {"agent": agent.pk})
        grant = MCPAccessGrant.objects.get(server=server, agent=agent)
        self.assertEqual(grant.allowed_tools, [])

        self.client.post(reverse("mcp_gateway:grant_revoke", args=[grant.pk]))
        self.assertFalse(MCPAccessGrant.objects.filter(pk=grant.pk).exists())
        self.assertIsNotNone(self.audited(AuditAction.DELETE, MCPAccessGrant))

    def test_an_agent_from_another_organization_cannot_be_granted(self):
        server = MCPServer.objects.create(organization=self.org, name="GitHub", endpoint="https://mcp.example/github")
        foreign = AIAgent.objects.create(organization=self.other, name="Foreign", agent_id="foreign")
        response = self.client.post(reverse("mcp_gateway:grant_create", args=[server.pk]), {"agent": foreign.pk})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(MCPAccessGrant.objects.exists())
