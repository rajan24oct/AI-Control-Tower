from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.roles import Role
from apps.applications_registry.models import AIApplication
from apps.audit.models import AuditLog
from apps.core.constants import DataClassification, LifecycleStatus
from apps.intake.models import G1Outcome, UseCase, UseCaseStatus
from apps.intake.services import IntakeError, IntakeService, can_decide, criteria
from apps.organizations.models import Organization, OrganizationMembership, Team

User = get_user_model()

COMPLETE = {
    "need": "Claims handlers spend too long reading medical reports.",
    "current_measure": "45 minutes per complex claim", "target_measure": "15 minutes",
    "non_ai_alternative": "More handlers; does not scale with volume.",
    "decision_supported": "Which parts of a medical report matter for this claim.",
    "affected_parties": "Claims handlers; claimants indirectly.",
    "intended_autonomy": 2, "data_path": "Reports from the claims document store.",
    "data_classification": DataClassification.CONFIDENTIAL, "no_commitment": True,
}
TIER_1 = dict(d1_consequence=1, d2_autonomy=1, d3_reversibility=1, d4_exposure=1,
              d5_data_sensitivity=1, d5_model_uncertainty=1)
TIER_2 = dict(d1_consequence=2, d2_autonomy=2, d3_reversibility=2, d4_exposure=2,
              d5_data_sensitivity=3, d5_model_uncertainty=3)
TIER_4 = dict(d1_consequence=4, d2_autonomy=3, d3_reversibility=4, d4_exposure=3,
              d5_data_sensitivity=4, d5_model_uncertainty=3)


class IntakeFixture:
    def build(self):
        self.org = Organization.objects.create(name="Intake Org")
        self.team = Team.objects.create(organization=self.org, name="Claims", department="Claims")
        self.developer = self.member("dev@example.com", Role.AI_DEVELOPER)
        self.risk = self.member("risk@example.com", Role.RISK_MANAGER)
        self.body = self.member("body@example.com", Role.GOVERNANCE_MANAGER)
        self.viewer = self.member("viewer@example.com", Role.VIEWER)

    def member(self, email, role):
        user = User.objects.create_user(email=email, password="pw-12345", first_name=email.split("@")[0].title())
        OrganizationMembership.objects.create(organization=self.org, user=user, role=role, is_default=True)
        return user

    def use_case(self, scores=TIER_2, **overrides):
        values = {"organization": self.org, "title": "Medical report summaries", "team": self.team,
                  "sponsor": self.risk, "created_by": self.developer, **COMPLETE, **scores}
        values.update(overrides)
        return UseCase.objects.create(**values)


class IntakeServiceTests(IntakeFixture, TestCase):
    def setUp(self):
        self.build()

    def test_references_are_sequential_per_organization(self):
        first, second = self.use_case(), self.use_case(title="Another")
        self.assertEqual((first.reference, second.reference), ("UC-0001", "UC-0002"))

    def test_the_provisional_tier_uses_the_s2_rules(self):
        self.assertEqual(self.use_case(TIER_2).provisional_tier, 2)
        floored = self.use_case(dict(TIER_1, d1_consequence=4), title="Floored")
        self.assertEqual(floored.provisional_tier, 3)
        self.assertTrue(floored.floor_applied)

    def test_an_incomplete_use_case_cannot_be_submitted(self):
        use_case = self.use_case(non_ai_alternative="", d3_reversibility=None)
        self.assertEqual(len([c for c in criteria(use_case) if not c.met]), 2)
        with self.assertRaisesMessage(IntakeError, "non-AI alternative"):
            IntakeService.submit(use_case, self.developer)

    def test_tier_1_with_internal_data_is_approved_by_rule(self):
        use_case = self.use_case(TIER_1, data_classification=DataClassification.INTERNAL)
        IntakeService.submit(use_case, self.developer)
        self.assertEqual(use_case.status, UseCaseStatus.APPROVED)
        self.assertIsNone(use_case.decided_by)
        self.assertIn("by rule", use_case.decided_by_rule)

    def test_tier_1_with_sector_constraints_still_goes_to_a_person(self):
        use_case = self.use_case(TIER_1, data_classification=DataClassification.INTERNAL,
                                 sector_constraints="Consumer duty")
        IntakeService.submit(use_case, self.developer)
        self.assertEqual(use_case.status, UseCaseStatus.SUBMITTED)

    def test_tier_2_is_decided_by_the_risk_lead_but_not_by_the_proposer(self):
        use_case = self.use_case(TIER_2)
        IntakeService.submit(use_case, self.developer)
        self.assertFalse(can_decide(use_case, self.developer)[0])
        self.assertTrue(can_decide(use_case, self.risk)[0])
        IntakeService.decide(use_case, self.risk, G1Outcome.APPROVE)
        self.assertEqual(use_case.status, UseCaseStatus.APPROVED)
        self.assertTrue(AuditLog.objects.filter(resource_id=str(use_case.pk), action="gate_signature").exists())

    def test_tier_4_needs_the_governance_body_and_not_the_sponsor(self):
        use_case = self.use_case(TIER_4, sponsor=self.body)
        IntakeService.submit(use_case, self.developer)
        self.assertFalse(can_decide(use_case, self.risk)[0])
        allowed, reason = can_decide(use_case, self.body)
        self.assertFalse(allowed)
        self.assertIn("sponsor", reason)
        other = self.member("body2@example.com", Role.GOVERNANCE_MANAGER)
        self.assertTrue(can_decide(use_case, other)[0])

    def test_outcomes_that_need_a_reason_or_conditions(self):
        use_case = self.use_case(TIER_2)
        IntakeService.submit(use_case, self.developer)
        for outcome in (G1Outcome.REMEDIATE, G1Outcome.ESCALATE, G1Outcome.REJECT):
            with self.assertRaises(IntakeError):
                IntakeService.decide(use_case, self.risk, outcome)
        with self.assertRaisesMessage(IntakeError, "at least one condition"):
            IntakeService.decide(use_case, self.risk, G1Outcome.APPROVE_WITH_CONDITIONS)
        due = (date.today() + timedelta(days=30)).isoformat()
        IntakeService.decide(use_case, self.risk, G1Outcome.APPROVE_WITH_CONDITIONS, conditions=[
            {"text": "DPIA completed", "owner_id": str(self.developer.pk), "owner": "Dev", "due": due}])
        self.assertEqual(use_case.status, UseCaseStatus.CONDITIONAL)
        IntakeService.complete_condition(use_case, 0, self.developer)
        self.assertEqual(use_case.open_conditions, [])

    def test_remediate_returns_it_for_editing_and_resubmission(self):
        use_case = self.use_case(TIER_2)
        IntakeService.submit(use_case, self.developer)
        IntakeService.decide(use_case, self.risk, G1Outcome.REMEDIATE, "Quantify the target measure.")
        self.assertTrue(use_case.is_editable)
        IntakeService.submit(use_case, self.developer)
        self.assertEqual(use_case.status, UseCaseStatus.SUBMITTED)
        self.assertEqual(use_case.outcome, "")

    def test_an_approved_use_case_registers_as_a_draft_application(self):
        use_case = self.use_case(TIER_2)
        with self.assertRaises(IntakeError):
            IntakeService.register(use_case, self.developer)
        IntakeService.submit(use_case, self.developer)
        IntakeService.decide(use_case, self.risk, G1Outcome.APPROVE)
        application = IntakeService.register(use_case, self.developer)
        self.assertEqual(application.status, LifecycleStatus.DRAFT)
        self.assertEqual((application.business_owner, application.team, application.department),
                         (self.risk, self.team, "Claims"))
        self.assertIn(use_case.reference, application.business_purpose)
        self.assertEqual(use_case.status, UseCaseStatus.REGISTERED)
        # A second registration of a same-named use case does not collide.
        other = self.use_case(TIER_2)
        IntakeService.submit(other, self.developer)
        IntakeService.decide(other, self.risk, G1Outcome.APPROVE)
        self.assertEqual(IntakeService.register(other, self.developer).name, "Medical report summaries (2)")
        self.assertEqual(AIApplication.objects.filter(organization=self.org).count(), 2)


class IntakeViewTests(IntakeFixture, TestCase):
    def setUp(self):
        self.build()

    def test_the_register_and_a_use_case_render(self):
        use_case = self.use_case()
        self.client.force_login(self.risk)
        self.assertContains(self.client.get(reverse("intake:list")), use_case.reference)
        response = self.client.get(reverse("intake:detail", args=[use_case.pk]))
        self.assertContains(response, "Gate G1 checklist")
        self.assertContains(response, "Provisional tier")

    def test_proposing_through_the_form(self):
        self.client.force_login(self.developer)
        payload = {"title": "Fraud alert triage", "need": "Too many false positives.",
                   "data_classification": "restricted", **{k: v for k, v in TIER_2.items()}}
        response = self.client.post(reverse("intake:create"), payload)
        use_case = UseCase.objects.get(title="Fraud alert triage")
        self.assertRedirects(response, reverse("intake:detail", args=[use_case.pk]))
        self.assertEqual((use_case.status, use_case.created_by), (UseCaseStatus.DRAFT, self.developer))

    def test_a_viewer_cannot_propose(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("intake:create")).status_code, 403)

    def test_submit_and_decide_through_the_pages(self):
        use_case = self.use_case(TIER_2)
        self.client.force_login(self.developer)
        self.client.post(reverse("intake:submit", args=[use_case.pk]))
        use_case.refresh_from_db()
        self.assertEqual(use_case.status, UseCaseStatus.SUBMITTED)
        # The proposer cannot decide their own use case: refused, and nothing changes.
        self.client.post(reverse("intake:decide", args=[use_case.pk]), {"outcome": "approve"})
        use_case.refresh_from_db()
        self.assertEqual(use_case.status, UseCaseStatus.SUBMITTED)
        self.client.force_login(self.risk)
        self.client.post(reverse("intake:decide", args=[use_case.pk]), {
            "outcome": "approve_with_conditions", "note": "",
            "condition_text": ["Run a DPIA", ""], "condition_owner": [str(self.developer.pk), ""],
            "condition_due": [(date.today() + timedelta(days=14)).isoformat(), ""],
        })
        use_case.refresh_from_db()
        self.assertEqual(use_case.status, UseCaseStatus.CONDITIONAL)
        self.assertEqual(use_case.conditions[0]["owner"], "Dev")
