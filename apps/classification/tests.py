"""IRGF S2 risk classification: the tier rule, scoring, countersigning and triggers."""

from __future__ import annotations

from itertools import product

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from apps.accounts.roles import Role
from apps.agents.models import AIAgent, AutonomyLevel
from apps.applications_registry.models import AIApplication
from apps.audit.models import AuditAction, AuditLog
from apps.classification import irgf
from apps.classification.models import ClassificationStatus
from apps.classification.services import ClassificationError, ClassificationService
from apps.core.constants import DataClassification, Environment
from apps.organizations.models import Organization, OrganizationMembership

User = get_user_model()

BASELINE = {
    "d1_consequence": 2,
    "d2_autonomy": 2,
    "d3_reversibility": 2,
    "d4_exposure": 2,
    "d5_data_sensitivity": 2,
    "d5_model_uncertainty": 2,
}


def scores(**overrides) -> dict:
    return {**BASELINE, **overrides}


class TierRuleTests(SimpleTestCase):
    def test_the_matrix_matches_irgf(self):
        expected = {
            (1, 1): 1, (1, 2): 1, (1, 3): 2, (1, 4): 2,
            (2, 1): 1, (2, 2): 2, (2, 3): 2, (2, 4): 3,
            (3, 1): 2, (3, 2): 2, (3, 3): 3, (3, 4): 4,
            (4, 1): 3, (4, 2): 3, (4, 3): 4, (4, 4): 4,
        }
        for (impact, deficit), tier in expected.items():
            self.assertEqual(irgf.TIER_MATRIX[impact][deficit], tier, (impact, deficit))

    def test_both_axes_are_rounded_up_averages(self):
        result = irgf.compute(d1=3, d2=2, d3=3, d4=2, d5_data=2, d5_model=1)
        # Impact (3 + 2 + 2) / 3 = 2.33 -> 3; control deficit (2 + 3) / 2 = 2.5 -> 3.
        self.assertEqual((result.impact, result.control_deficit, result.tier), (3, 3, 3))

    def test_d5_is_the_higher_sub_score_never_the_average(self):
        result = irgf.compute(1, 1, 1, 1, 4, 1)
        self.assertEqual(result.d5, 4)
        self.assertEqual(result.impact, 2)

    def test_a_catastrophic_consequence_is_floored_at_tier_3(self):
        result = irgf.compute(4, 1, 1, 1, 1, 1)
        self.assertEqual((result.matrix_tier, result.tier), (1, 3))
        self.assertTrue(result.floor_applied)

    def test_full_autonomy_with_irreversibility_is_floored_too(self):
        result = irgf.compute(1, 4, 4, 1, 1, 1)
        self.assertEqual((result.matrix_tier, result.tier), (2, 3))

    def test_the_floor_never_lowers_a_higher_tier(self):
        result = irgf.compute(4, 4, 4, 4, 4, 4)
        self.assertEqual(result.tier, 4)
        self.assertFalse(result.floor_applied)

    def test_every_combination_resolves_to_a_tier(self):
        for values in product(irgf.SCORES, repeat=6):
            self.assertIn(irgf.compute(*values).tier, irgf.TIERS)

    def test_scores_outside_one_to_four_are_rejected(self):
        with self.assertRaises(ValueError):
            irgf.compute(0, 1, 1, 1, 1, 1)
        with self.assertRaises(ValueError):
            irgf.compute(1, 5, 1, 1, 1, 1)


class ClassificationFixture(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Tier Org")
        self.owner = self.member("owner@example.com", Role.AI_DEVELOPER)
        self.scorer = self.member("scorer@example.com", Role.AI_DEVELOPER)
        self.risk = self.member("risk@example.com", Role.RISK_MANAGER)
        self.agent = AIAgent.objects.create(
            organization=self.organization, name="Claims Agent", agent_id="claims-agent",
            owner=self.owner, environment=Environment.PRODUCTION,
            autonomy_level=AutonomyLevel.EXECUTE_WITH_APPROVAL,
            data_classification=DataClassification.CONFIDENTIAL,
        )

    def member(self, email, role):
        user = User.objects.create_user(email=email, password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=user, role=role, is_default=True
        )
        return user

    def score(self, subject=None, **overrides):
        return ClassificationService.score(
            subject or self.agent, scores=scores(**overrides), rationale="Evidence.",
            actor=self.scorer,
        )


class ScoringTests(ClassificationFixture):
    def test_a_scoring_is_provisional_and_the_tier_is_cached_on_the_agent(self):
        record = self.score(d1_consequence=3, d4_exposure=3, d5_data_sensitivity=3)
        self.assertEqual(record.status, ClassificationStatus.PROVISIONAL)
        self.assertEqual(record.gate_evidence, "G1")
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.risk_tier, record.tier)

    def test_a_rescore_supersedes_the_old_record_and_keeps_it(self):
        first = self.score()
        second = self.score(d1_consequence=4)
        first.refresh_from_db()
        self.assertEqual(first.status, ClassificationStatus.SUPERSEDED)
        self.assertEqual(first.superseded_by, second)
        self.assertEqual(ClassificationService.current(self.agent), second)
        self.assertEqual(self.agent.risk_classifications.count(), 2)

    def test_a_rationale_is_required(self):
        with self.assertRaises(ClassificationError):
            ClassificationService.score(self.agent, scores=scores(), rationale=" ", actor=self.scorer)

    def test_an_override_needs_a_reason_and_cannot_go_below_the_floor(self):
        with self.assertRaises(ClassificationError):
            ClassificationService.score(
                self.agent, scores=scores(), rationale="x", actor=self.scorer, tier_override=3
            )
        with self.assertRaises(ClassificationError):
            ClassificationService.score(
                self.agent, scores=scores(d1_consequence=4), rationale="x", actor=self.scorer,
                tier_override=2, override_rationale="Looks fine to us.",
            )
        record = ClassificationService.score(
            self.agent, scores=scores(), rationale="x", actor=self.scorer,
            tier_override=3, override_rationale="Under regulator scrutiny.",
        )
        self.assertEqual((record.matrix_tier, record.tier), (2, 3))

    def test_scoring_is_audited_as_stage_s2(self):
        record = self.score()
        entry = AuditLog.objects.get(
            action=AuditAction.RISK_CLASSIFICATION, resource_id=str(record.pk)
        )
        self.assertEqual(entry.metadata["stage"], "S2")
        self.assertEqual(entry.metadata["gate_evidence"], "G1")

    def test_suggestions_start_from_the_registry_and_never_lower_a_score(self):
        suggested = ClassificationService.suggested_scores(self.agent)
        self.assertEqual(suggested["d2_autonomy"], 2)
        self.assertEqual(suggested["d5_data_sensitivity"], 3)
        self.score(d2_autonomy=4)
        self.assertEqual(ClassificationService.suggested_scores(self.agent)["d2_autonomy"], 4)


class CountersignTests(ClassificationFixture):
    def test_an_independent_risk_manager_confirms_the_tier(self):
        record = self.score(d1_consequence=3, d2_autonomy=3, d3_reversibility=3, d4_exposure=3)
        ClassificationService.countersign(record, actor=self.risk)
        record.refresh_from_db()
        self.assertEqual(record.status, ClassificationStatus.CONFIRMED)
        self.assertEqual(record.gate_evidence, "G2")
        self.assertEqual(record.countersigned_by, self.risk)
        # Tier 3-4 systems are re-scored at least annually.
        self.assertEqual(record.tier, 3)
        self.assertIsNotNone(record.review_due)

    def test_a_tier_1_2_confirmation_sets_no_annual_review(self):
        record = self.score()
        ClassificationService.countersign(record, actor=self.risk)
        record.refresh_from_db()
        self.assertIsNone(record.review_due)

    def test_the_scorer_cannot_countersign(self):
        record = self.score()
        with self.assertRaises(ClassificationError):
            ClassificationService.countersign(record, actor=self.scorer)

    def test_the_owner_cannot_countersign(self):
        record = ClassificationService.score(
            self.agent, scores=scores(), rationale="x", actor=self.risk
        )
        with self.assertRaises(ClassificationError):
            ClassificationService.countersign(record, actor=self.owner)

    def test_adjusting_a_score_needs_a_note_and_keeps_the_scorers_original(self):
        record = self.score()
        with self.assertRaises(ClassificationError):
            ClassificationService.countersign(
                record, actor=self.risk, adjusted_scores=scores(d1_consequence=4)
            )
        ClassificationService.countersign(
            record, actor=self.risk, adjusted_scores=scores(d1_consequence=4),
            note="Payout errors harm customers and cannot be undone.",
        )
        record.refresh_from_db()
        self.assertEqual(record.d1_consequence, 4)
        self.assertEqual(record.scorer_scores["d1_consequence"], 2)
        self.assertEqual(record.tier, 3)  # the safety floor
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.risk_tier, 3)

    def test_a_confirmed_record_cannot_be_countersigned_again(self):
        record = self.score()
        ClassificationService.countersign(record, actor=self.risk)
        with self.assertRaises(ClassificationError):
            ClassificationService.countersign(record, actor=self.risk)


class TriggerTests(ClassificationFixture):
    def test_an_autonomy_increase_flags_a_rescore(self):
        record = self.score()
        self.agent.autonomy_level = AutonomyLevel.AUTONOMOUS
        self.agent.save()
        record.refresh_from_db()
        self.assertTrue(record.rescore_required)
        self.assertTrue(record.rescore_due)
        self.assertIn("Autonomy increased", record.rescore_reasons[0])

    def test_lowering_autonomy_is_not_a_trigger(self):
        record = self.score()
        self.agent.autonomy_level = AutonomyLevel.ADVISORY
        self.agent.save()
        record.refresh_from_db()
        self.assertFalse(record.rescore_required)

    def test_more_sensitive_data_and_a_move_to_production_flag_an_application(self):
        application = AIApplication.objects.create(
            organization=self.organization, name="Portal", environment=Environment.STAGING,
            data_classification=DataClassification.INTERNAL,
        )
        record = self.score(subject=application)
        application.data_classification = DataClassification.RESTRICTED
        application.environment = Environment.PRODUCTION
        application.save()
        record.refresh_from_db()
        self.assertEqual(len(record.rescore_reasons), 2)

    def test_a_trigger_is_recorded_once(self):
        record = self.score()
        ClassificationService.flag_rescore(self.agent, "New grounding source.")
        ClassificationService.flag_rescore(self.agent, "New grounding source.")
        record.refresh_from_db()
        self.assertEqual(record.rescore_reasons, ["New grounding source."])

    def test_an_unclassified_system_has_nothing_to_flag(self):
        self.assertIsNone(ClassificationService.flag_rescore(self.agent, "Anything."))


class ClassificationViewTests(ClassificationFixture):
    def test_the_register_lists_classifications_and_what_is_unclassified(self):
        self.score()
        AIAgent.objects.create(
            organization=self.organization, name="Unscored Agent", agent_id="unscored"
        )
        self.client.force_login(self.risk)
        response = self.client.get(reverse("classification:register"))
        self.assertContains(response, "Claims Agent")
        self.assertContains(response, "Unscored Agent")
        self.assertContains(response, "IRGF stage S2")

    def test_scoring_through_the_form(self):
        self.client.force_login(self.scorer)
        url = reverse("classification:score", args=["agents", self.agent.pk])
        self.assertEqual(self.client.get(url).status_code, 200)

        response = self.client.post(url, {**scores(d1_consequence=3), "rationale": "Payouts."})
        record = ClassificationService.current(self.agent)
        self.assertRedirects(response, reverse("classification:detail", args=[record.pk]))
        self.assertEqual(record.scored_by, self.scorer)

    def test_a_viewer_cannot_score(self):
        viewer = self.member("viewer@example.com", Role.VIEWER)
        self.client.force_login(viewer)
        url = reverse("classification:score", args=["agents", self.agent.pk])
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_only_the_risk_function_countersigns_through_the_form(self):
        record = self.score()
        url = reverse("classification:countersign", args=[record.pk])

        self.client.force_login(self.scorer)
        self.assertEqual(self.client.get(url).status_code, 403)

        self.client.force_login(self.risk)
        response = self.client.post(url, {**scores(), "note": ""})
        self.assertRedirects(response, reverse("classification:detail", args=[record.pk]))
        record.refresh_from_db()
        self.assertTrue(record.is_confirmed)

    def test_the_record_and_the_subject_pages_render(self):
        record = self.score()
        self.client.force_login(self.risk)
        detail = self.client.get(reverse("classification:detail", args=[record.pk]))
        self.assertContains(detail, "Evidence for Gate G1")
        agent_page = self.client.get(reverse("inventory:agent_detail", args=[self.agent.pk]))
        self.assertContains(agent_page, "Risk Classification")
        self.assertContains(agent_page, "Agent Governance Chain")
