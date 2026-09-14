"""Policies scoped by IRGF tier, and the policy that requires an S2 classification."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.agents.models import AIAgent
from apps.classification.services import ClassificationService
from apps.core.constants import Environment
from apps.organizations.models import Organization
from apps.policies.models import Policy, PolicyEffect, PolicyType
from apps.policies.services import PolicyContext, PolicyEngine

User = get_user_model()

SCORES = {
    "d1_consequence": 2, "d2_autonomy": 2, "d3_reversibility": 2,
    "d4_exposure": 2, "d5_data_sensitivity": 2, "d5_model_uncertainty": 2,
}


class TierScopedPolicyTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Tier Policy Org")
        self.agent = AIAgent.objects.create(
            organization=self.organization, name="Payments Agent", agent_id="payments-agent",
            environment=Environment.PRODUCTION,
        )
        Policy.objects.create(
            organization=self.organization, name="No transfers at Tier 3-4",
            policy_type=PolicyType.TOOL_ACCESS, effect=PolicyEffect.DENY,
            configuration={"blocked_tools": ["transfer_funds"], "tiers": [3, 4]},
        )

    def verdict(self):
        return PolicyEngine.evaluate(
            self.organization,
            PolicyContext(agent=self.agent, tool="transfer_funds", environment=Environment.PRODUCTION),
        )

    def test_a_tier_scoped_policy_ignores_lower_tiers_and_unclassified_systems(self):
        self.assertTrue(self.verdict().allowed)
        self.agent.risk_tier = 2
        self.assertTrue(self.verdict().allowed)

    def test_it_applies_at_the_tiers_it_names(self):
        self.agent.risk_tier = 3
        self.assertFalse(self.verdict().allowed)

    def test_tiers_must_be_whole_numbers_from_one_to_four(self):
        policy = Policy(
            organization=self.organization, name="Bad", policy_type=PolicyType.TOOL_ACCESS,
            configuration={"blocked_tools": ["x"], "tiers": [5]},
        )
        with self.assertRaises(ValidationError):
            policy.clean()
        policy.configuration = {"blocked_tools": ["x"], "tiers": [3, 4]}
        policy.clean()


class RiskClassificationPolicyTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="S2 Policy Org")
        self.scorer = User.objects.create_user(email="scorer@example.com")
        self.risk = User.objects.create_user(email="risk@example.com")
        self.agent = AIAgent.objects.create(
            organization=self.organization, name="Claims Agent", agent_id="claims-agent",
            environment=Environment.PRODUCTION,
        )
        self.policy = Policy.objects.create(
            organization=self.organization, name="S2 before production",
            policy_type=PolicyType.RISK_CLASSIFICATION, effect=PolicyEffect.DENY,
            configuration={"environments": [Environment.PRODUCTION]},
        )

    def verdict(self):
        return PolicyEngine.evaluate(
            self.organization, PolicyContext(agent=self.agent, environment=Environment.PRODUCTION)
        )

    def test_an_unclassified_agent_is_refused(self):
        decision = self.verdict()
        self.assertFalse(decision.allowed)
        self.assertIn("no IRGF risk classification", decision.reason)

    def test_a_provisional_tier_is_not_enough_until_countersigned(self):
        record = ClassificationService.score(
            self.agent, scores=SCORES, rationale="x", actor=self.scorer
        )
        self.assertIn("countersignature", self.verdict().reason)

        ClassificationService.countersign(record, actor=self.risk)
        self.agent.refresh_from_db()
        self.assertTrue(self.verdict().allowed)

    def test_a_due_rescore_blocks_only_when_configured(self):
        record = ClassificationService.score(
            self.agent, scores=SCORES, rationale="x", actor=self.scorer
        )
        ClassificationService.countersign(record, actor=self.risk)
        ClassificationService.flag_rescore(self.agent, "Autonomy increased from level 2 to 4.")
        self.assertTrue(self.verdict().allowed)

        self.policy.configuration = {"block_when_rescore_due": True}
        self.policy.save()
        decision = self.verdict()
        self.assertFalse(decision.allowed)
        self.assertIn("Autonomy increased", decision.reason)

    def test_the_new_keys_pass_schema_validation(self):
        self.policy.configuration = {
            "environments": ["production"], "require_countersignature": False,
            "block_when_rescore_due": True, "tiers": [3],
        }
        self.policy.clean()
