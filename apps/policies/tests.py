from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.accounts.roles import Role
from apps.agents.models import AIAgent, AutonomyLevel
from apps.applications_registry.models import AIApplication
from apps.approvals.models import ApprovalRule
from apps.core.constants import DataClassification, Environment, LifecycleStatus, RiskLevel
from apps.executions.models import Execution, ExecutionStatusChoices
from apps.models_registry.models import AIModel
from apps.organizations.models import Organization, OrganizationMembership
from apps.policies.models import Policy, PolicyEffect, PolicyType, PolicyViolation
from apps.policies.services import PolicyContext, PolicyEngine
from apps.providers.models import AIProvider
from integrations.base.events import EventType

User = get_user_model()


class PolicyFixtureMixin:
    def build_estate(self, name="Policy Org"):
        self.organization = Organization.objects.create(name=name)
        self.provider = AIProvider.objects.create(
            organization=self.organization, name="Approved Provider", adapter_key="mock",
            slug="approved-provider", is_approved=True, region="eu-west-1",
        )
        self.rogue_provider = AIProvider.objects.create(
            organization=self.organization, name="Sandbox Provider", adapter_key="mock",
            slug="sandbox-provider", is_approved=False, region="us-east-1",
        )
        self.model = AIModel.objects.create(
            organization=self.organization, provider=self.provider,
            model_name="Approved Model", model_id="approved-1", approved_for_use=True,
        )
        self.shadow_model = AIModel.objects.create(
            organization=self.organization, provider=self.provider,
            model_name="Shadow Model", model_id="shadow-1", approved_for_use=False,
        )
        self.agent = AIAgent.objects.create(
            organization=self.organization, name="Agent", agent_id="agent",
            provider=self.provider, primary_model=self.model,
            environment=Environment.PRODUCTION, autonomy_level=AutonomyLevel.RECOMMEND,
            data_classification=DataClassification.INTERNAL, risk_level=RiskLevel.MODERATE,
        )
        self.application = AIApplication.objects.create(
            organization=self.organization, name="App"
        )
        self.application.agents.add(self.agent)

    def policy(self, policy_type, effect=PolicyEffect.DENY, **kwargs):
        defaults = {
            "organization": self.organization,
            "name": f"{policy_type}-{Policy.objects.count()}",
            "policy_type": policy_type,
            "effect": effect,
            "status": LifecycleStatus.ACTIVE,
        }
        defaults.update(kwargs)
        return Policy.objects.create(**defaults)

    def evaluate(self, **context_kwargs):
        return PolicyEngine.evaluate(self.organization, PolicyContext(**context_kwargs))


class PolicyConfigurationTests(PolicyFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()

    def test_unknown_settings_are_rejected(self):
        """A typo must not silently disable a control."""
        policy = Policy(
            organization=self.organization, name="Typo",
            policy_type=PolicyType.COST_LIMIT,
            configuration={"max_cost_per_exection": "5"},
        )
        with self.assertRaises(ValidationError) as ctx:
            policy.full_clean()
        self.assertIn("configuration", ctx.exception.error_dict)

    def test_known_settings_are_accepted(self):
        policy = Policy(
            organization=self.organization, name="Fine",
            policy_type=PolicyType.COST_LIMIT,
            configuration={"max_cost_per_execution": "5"},
            effect=PolicyEffect.DENY,
        )
        policy.full_clean()

    def test_summary_is_readable(self):
        policy = self.policy(
            PolicyType.TOOL_ACCESS, configuration={"blocked_tools": ["a", "b"]}
        )
        self.assertIn("blocked tools: a, b", policy.summary)

    def test_an_unconfigured_policy_describes_what_it_checks(self):
        """The summary must never just echo the type name back."""
        policy = self.policy(PolicyType.MODEL_APPROVAL, configuration={})
        self.assertEqual(policy.summary, "model must be approved for use")
        self.assertNotEqual(policy.summary, policy.get_policy_type_display())


class ModelApprovalPolicyTests(PolicyFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.policy(PolicyType.MODEL_APPROVAL, effect=PolicyEffect.DENY)

    def test_an_approved_model_passes(self):
        decision = self.evaluate(agent=self.agent, model=self.model)
        self.assertTrue(decision.allowed)

    def test_an_unapproved_model_is_denied(self):
        decision = self.evaluate(agent=self.agent, model=self.shadow_model)
        self.assertFalse(decision.allowed)
        self.assertIn("not approved for use", decision.reason)

    def test_the_agents_own_model_is_used_when_none_is_given(self):
        self.agent.primary_model = self.shadow_model
        self.agent.save()
        self.assertFalse(self.evaluate(agent=self.agent).allowed)

    def test_a_blocked_risk_classification_is_caught(self):
        Policy.objects.all().delete()
        self.policy(
            PolicyType.MODEL_APPROVAL,
            configuration={"blocked_risk_classifications": [RiskLevel.HIGH]},
        )
        self.model.risk_classification = RiskLevel.HIGH
        self.model.save()
        self.assertFalse(self.evaluate(model=self.model).allowed)


class ProviderPolicyTests(PolicyFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()

    def test_require_approved(self):
        self.policy(PolicyType.PROVIDER, configuration={"require_approved": True})
        self.assertTrue(self.evaluate(provider=self.provider).allowed)
        self.assertFalse(self.evaluate(provider=self.rogue_provider).allowed)

    def test_a_blocked_provider(self):
        self.policy(
            PolicyType.PROVIDER, configuration={"blocked_providers": ["sandbox-provider"]}
        )
        self.assertFalse(self.evaluate(provider=self.rogue_provider).allowed)
        self.assertTrue(self.evaluate(provider=self.provider).allowed)

    def test_an_allow_list_excludes_everything_else(self):
        self.policy(
            PolicyType.PROVIDER, configuration={"allowed_providers": ["approved-provider"]}
        )
        self.assertTrue(self.evaluate(provider=self.provider).allowed)
        self.assertFalse(self.evaluate(provider=self.rogue_provider).allowed)

    def test_region_restrictions(self):
        self.policy(PolicyType.PROVIDER, configuration={"allowed_regions": ["eu-west-1"]})
        self.assertTrue(self.evaluate(provider=self.provider).allowed)
        self.assertFalse(self.evaluate(provider=self.rogue_provider).allowed)

    def test_no_provider_in_context_means_no_verdict(self):
        self.policy(PolicyType.PROVIDER, configuration={"require_approved": True})
        self.assertTrue(self.evaluate().allowed)


class DataClassificationPolicyTests(PolicyFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.policy(
            PolicyType.DATA_CLASSIFICATION,
            configuration={
                "classifications": [DataClassification.RESTRICTED],
                "require_approved_provider": True,
            },
        )

    def test_restricted_data_to_an_unapproved_provider_is_denied(self):
        decision = self.evaluate(
            provider=self.rogue_provider, data_classification=DataClassification.RESTRICTED
        )
        self.assertFalse(decision.allowed)

    def test_restricted_data_to_an_approved_provider_passes(self):
        decision = self.evaluate(
            provider=self.provider, data_classification=DataClassification.RESTRICTED
        )
        self.assertTrue(decision.allowed)

    def test_a_classification_outside_the_policy_is_ignored(self):
        decision = self.evaluate(
            provider=self.rogue_provider, data_classification=DataClassification.PUBLIC
        )
        self.assertTrue(decision.allowed)

    def test_the_environment_filter_narrows_scope(self):
        Policy.objects.all().delete()
        self.policy(
            PolicyType.DATA_CLASSIFICATION,
            configuration={
                "classifications": [DataClassification.RESTRICTED],
                "environments": [Environment.PRODUCTION],
                "require_approved_provider": True,
            },
        )
        blocked = self.evaluate(
            provider=self.rogue_provider,
            data_classification=DataClassification.RESTRICTED,
            environment=Environment.PRODUCTION,
        )
        ignored = self.evaluate(
            provider=self.rogue_provider,
            data_classification=DataClassification.RESTRICTED,
            environment=Environment.DEVELOPMENT,
        )
        self.assertFalse(blocked.allowed)
        self.assertTrue(ignored.allowed)


class AgentAutonomyPolicyTests(PolicyFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.policy(
            PolicyType.AGENT_AUTONOMY,
            effect=PolicyEffect.REQUIRE_APPROVAL,
            configuration={
                "max_autonomy_level": 3,
                "environments": [Environment.PRODUCTION],
            },
        )

    def test_within_the_ceiling_passes(self):
        decision = self.evaluate(agent=self.agent)
        self.assertFalse(decision.requires_approval)

    def test_above_the_ceiling_requires_approval(self):
        self.agent.autonomy_level = AutonomyLevel.AUTONOMOUS
        self.agent.save()

        decision = self.evaluate(agent=self.agent)
        self.assertTrue(decision.requires_approval)
        self.assertTrue(decision.allowed)
        self.assertIn("above the permitted maximum", decision.reason)

    def test_a_non_production_environment_is_out_of_scope(self):
        self.agent.autonomy_level = AutonomyLevel.AUTONOMOUS
        self.agent.environment = Environment.DEVELOPMENT
        self.agent.save()
        self.assertFalse(self.evaluate(agent=self.agent).requires_approval)


class CostLimitPolicyTests(PolicyFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.policy(
            PolicyType.COST_LIMIT,
            effect=PolicyEffect.REQUIRE_APPROVAL,
            configuration={"max_cost_per_execution": "5.00"},
        )

    def test_below_the_ceiling(self):
        self.assertFalse(self.evaluate(estimated_cost=Decimal("1")).requires_approval)

    def test_at_the_ceiling(self):
        self.assertTrue(self.evaluate(estimated_cost=Decimal("5")).requires_approval)

    def test_above_the_ceiling(self):
        self.assertTrue(self.evaluate(estimated_cost=Decimal("50")).requires_approval)

    def test_a_malformed_cost_does_not_crash_the_check(self):
        self.assertFalse(self.evaluate(estimated_cost="not-a-number").requires_approval)


class ToolAccessPolicyTests(PolicyFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.policy(
            PolicyType.TOOL_ACCESS,
            configuration={"blocked_tools": ["transfer_funds", "delete_account"]},
        )

    def test_a_prohibited_tool_is_denied(self):
        decision = self.evaluate(agent=self.agent, tool="transfer_funds")
        self.assertFalse(decision.allowed)
        self.assertIn("prohibited", decision.reason)

    def test_an_ordinary_tool_passes(self):
        self.assertTrue(self.evaluate(agent=self.agent, tool="web_search").allowed)

    def test_no_tool_means_no_verdict(self):
        self.assertTrue(self.evaluate(agent=self.agent).allowed)


class HumanApprovalPolicyTests(PolicyFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.policy(PolicyType.HUMAN_APPROVAL, effect=PolicyEffect.REQUIRE_APPROVAL)

    def test_it_delegates_to_the_approval_rules(self):
        self.assertFalse(self.evaluate(agent=self.agent).requires_approval)

        ApprovalRule.objects.create(
            organization=self.organization, name="High autonomy",
            min_autonomy_level=0, status=LifecycleStatus.ACTIVE,
        )
        decision = self.evaluate(agent=self.agent)
        self.assertTrue(decision.requires_approval)
        self.assertIn("High autonomy", decision.reason)


class PolicyEngineTests(PolicyFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()

    def test_no_policies_means_allowed(self):
        decision = self.evaluate(agent=self.agent)
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.requires_approval)
        self.assertEqual(decision.matches, [])

    def test_inactive_policies_are_ignored(self):
        self.policy(PolicyType.TOOL_ACCESS, status=LifecycleStatus.INACTIVE,
                    configuration={"blocked_tools": ["x"]})
        self.assertTrue(self.evaluate(tool="x").allowed)

    def test_policies_do_not_leak_across_organizations(self):
        other = Organization.objects.create(name="Rival Policies")
        Policy.objects.create(
            organization=other, name="Theirs", policy_type=PolicyType.TOOL_ACCESS,
            effect=PolicyEffect.DENY, configuration={"blocked_tools": ["x"]},
            status=LifecycleStatus.ACTIVE,
        )
        self.assertTrue(self.evaluate(tool="x").allowed)

    def test_a_denial_overrides_an_approval_requirement(self):
        self.policy(PolicyType.TOOL_ACCESS, configuration={"blocked_tools": ["x"]})
        self.policy(
            PolicyType.COST_LIMIT, effect=PolicyEffect.REQUIRE_APPROVAL,
            configuration={"max_cost_per_execution": "1"},
        )
        decision = self.evaluate(tool="x", estimated_cost=Decimal("9"))

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.requires_approval)
        self.assertEqual(len(decision.matches), 2)

    def test_severity_is_the_highest_matched(self):
        self.policy(PolicyType.TOOL_ACCESS, severity=RiskLevel.LOW,
                    configuration={"blocked_tools": ["x"]})
        self.policy(PolicyType.COST_LIMIT, effect=PolicyEffect.WARN,
                    severity=RiskLevel.CRITICAL,
                    configuration={"max_cost_per_execution": "1"})

        decision = self.evaluate(tool="x", estimated_cost=Decimal("9"))
        self.assertEqual(decision.severity, RiskLevel.CRITICAL)

    def test_a_warning_does_not_block(self):
        self.policy(PolicyType.TOOL_ACCESS, effect=PolicyEffect.WARN,
                    configuration={"blocked_tools": ["x"]})
        decision = self.evaluate(tool="x")

        self.assertTrue(decision.allowed)
        self.assertFalse(decision.requires_approval)
        self.assertEqual(len(decision.warnings), 1)

    def test_a_broken_policy_does_not_break_the_whole_check(self):
        """One malformed policy must not hide every other verdict."""
        broken = self.policy(PolicyType.AGENT_AUTONOMY,
                             configuration={"max_autonomy_level": "not-a-number"})
        self.policy(PolicyType.TOOL_ACCESS, configuration={"blocked_tools": ["x"]})
        self.agent.autonomy_level = AutonomyLevel.AUTONOMOUS
        self.agent.save()

        decision = self.evaluate(agent=self.agent, tool="x")
        self.assertFalse(decision.allowed)
        self.assertNotIn(broken.name, decision.policy_names)

    def test_the_payload_matches_the_documented_contract(self):
        self.policy(PolicyType.TOOL_ACCESS, configuration={"blocked_tools": ["x"]})
        payload = self.evaluate(tool="x").as_payload()

        for key in ("allowed", "requires_approval", "policy_ids", "reason", "severity"):
            self.assertIn(key, payload)
        self.assertFalse(payload["allowed"])


class PolicyRecordingTests(PolicyFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.blocking = self.policy(
            PolicyType.TOOL_ACCESS, configuration={"blocked_tools": ["transfer_funds"]}
        )
        self.execution = Execution.objects.create(
            organization=self.organization, external_id="run-policy", agent=self.agent,
            status=ExecutionStatusChoices.RUNNING,
        )

    def test_a_denial_is_recorded(self):
        PolicyEngine.check(
            self.organization,
            PolicyContext(agent=self.agent, tool="transfer_funds"),
            requested_action="Move money",
        )
        violation = PolicyViolation.objects.get()

        self.assertEqual(violation.policy, self.blocking)
        self.assertEqual(violation.effect, PolicyEffect.DENY)
        self.assertEqual(violation.requested_action, "Move money")
        self.assertEqual(violation.agent, self.agent)

    def test_a_pass_records_nothing(self):
        PolicyEngine.check(self.organization, PolicyContext(agent=self.agent, tool="web_search"))
        self.assertEqual(PolicyViolation.objects.count(), 0)

    def test_approval_routing_is_not_recorded_as_a_violation(self):
        """Routing to a human is the system working, not a breach."""
        Policy.objects.all().delete()
        self.policy(
            PolicyType.COST_LIMIT, effect=PolicyEffect.REQUIRE_APPROVAL,
            configuration={"max_cost_per_execution": "1"},
        )
        decision = PolicyEngine.check(
            self.organization, PolicyContext(agent=self.agent, estimated_cost=Decimal("5"))
        )
        self.assertTrue(decision.requires_approval)
        self.assertEqual(PolicyViolation.objects.count(), 0)

    def test_a_denial_marks_the_execution_timeline(self):
        PolicyEngine.check(
            self.organization,
            PolicyContext(agent=self.agent, tool="transfer_funds", execution=self.execution),
        )
        types = set(self.execution.events.values_list("event_type", flat=True))
        self.assertIn(str(EventType.POLICY_VIOLATION), types)

    def test_recording_can_be_skipped(self):
        PolicyEngine.check(
            self.organization,
            PolicyContext(agent=self.agent, tool="transfer_funds"),
            record=False,
        )
        self.assertEqual(PolicyViolation.objects.count(), 0)


class PolicyViewTests(PolicyFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate("Policy View Org")
        self.manager = User.objects.create_user(email="gov@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.manager,
            role=Role.GOVERNANCE_MANAGER, is_default=True,
        )
        self.viewer = User.objects.create_user(email="peek@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.viewer, role=Role.VIEWER, is_default=True
        )
        self.blocking = self.policy(
            PolicyType.TOOL_ACCESS, name="No fund transfers",
            configuration={"blocked_tools": ["transfer_funds"]},
        )
        PolicyEngine.check(
            self.organization, PolicyContext(agent=self.agent, tool="transfer_funds")
        )

    def test_the_list_renders_with_counts(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("policies:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No fund transfers")
        self.assertEqual(response.context["counts"]["blocking"], 1)
        self.assertEqual(response.context["counts"]["open_violations"], 1)

    def test_the_detail_page_shows_recorded_activity(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("policies:detail", args=[self.blocking.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "prohibited")

    def test_a_foreign_policy_returns_404(self):
        other = Organization.objects.create(name="Rival View")
        foreign = Policy.objects.create(
            organization=other, name="Theirs", policy_type=PolicyType.TOOL_ACCESS,
            effect=PolicyEffect.DENY,
        )
        self.client.force_login(self.manager)
        self.assertEqual(
            self.client.get(reverse("policies:detail", args=[foreign.pk])).status_code, 404
        )

    def test_a_governance_manager_can_acknowledge(self):
        self.client.force_login(self.manager)
        violation = PolicyViolation.objects.first()
        self.client.post(reverse("policies:acknowledge_violation", args=[violation.pk]))

        violation.refresh_from_db()
        self.assertTrue(violation.is_acknowledged)

    def test_a_viewer_cannot_acknowledge(self):
        self.client.force_login(self.viewer)
        violation = PolicyViolation.objects.first()
        self.client.post(reverse("policies:acknowledge_violation", args=[violation.pk]))

        violation.refresh_from_db()
        self.assertFalse(violation.is_acknowledged)

    def test_a_viewer_sees_no_management_controls(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("policies:list"))
        self.assertFalse(response.context["can_manage"])

    def test_unacknowledged_denials_reach_the_attention_panel(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("dashboard:home"))
        titles = " ".join(item.title for item in response.context["data"].attention)
        self.assertIn("policy denial", titles)
