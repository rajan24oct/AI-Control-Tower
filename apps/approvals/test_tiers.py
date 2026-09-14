"""Approval rules scoped by IRGF tier, and independent decisions at Tier 3-4."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.agents.models import AIAgent
from apps.approvals.models import ApprovalRule, ApprovalStatus
from apps.approvals.services import ApprovalContext, ApprovalRuleEngine, ApprovalService
from apps.core.constants import RiskLevel
from apps.organizations.models import Organization

User = get_user_model()


class TierConditionTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Tier Rule Org")
        self.agent = AIAgent.objects.create(
            organization=self.organization, name="Payout Agent", agent_id="payout-agent"
        )
        ApprovalRule.objects.create(
            organization=self.organization, name="Tier 3-4 systems", tiers=[3, 4],
            severity=RiskLevel.HIGH,
        )

    def requires_approval(self) -> bool:
        return ApprovalRuleEngine.evaluate(
            self.organization, ApprovalContext(agent=self.agent)
        ).requires_approval

    def test_the_rule_matches_only_the_tiers_it_names(self):
        self.assertFalse(self.requires_approval())
        self.agent.risk_tier = 2
        self.assertFalse(self.requires_approval())
        self.agent.risk_tier = 4
        self.assertTrue(self.requires_approval())

    def test_the_condition_is_described(self):
        rule = ApprovalRule.objects.get(name="Tier 3-4 systems")
        self.assertIn("IRGF tier in 3, 4", rule.conditions)


class IndependentDecisionTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Independence Org")
        self.owner = User.objects.create_user(email="owner@example.com")
        self.requester = User.objects.create_user(email="requester@example.com")
        self.approver = User.objects.create_user(email="approver@example.com")
        self.agent = AIAgent.objects.create(
            organization=self.organization, name="Payout Agent", agent_id="payout-agent",
            owner=self.owner, risk_tier=3,
        )

    def raise_request(self):
        return ApprovalService.request(
            organization=self.organization,
            requested_action="Release a payout",
            context=ApprovalContext(agent=self.agent),
            requested_by=self.requester,
        )

    def test_the_owner_cannot_decide_for_a_tier_3_system(self):
        with self.assertRaises(ValueError):
            ApprovalService.approve(self.raise_request(), user=self.owner)

    def test_the_requester_cannot_decide_it_either(self):
        with self.assertRaises(ValueError):
            ApprovalService.reject(self.raise_request(), user=self.requester)

    def test_an_independent_approver_can(self):
        request = ApprovalService.approve(self.raise_request(), user=self.approver)
        self.assertEqual(request.status, ApprovalStatus.APPROVED)

    def test_lower_tiers_keep_their_old_rules(self):
        self.agent.risk_tier = 2
        self.agent.save()
        request = ApprovalService.approve(self.raise_request(), user=self.owner)
        self.assertEqual(request.status, ApprovalStatus.APPROVED)
