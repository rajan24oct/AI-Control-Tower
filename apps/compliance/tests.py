import io
import json
import zipfile
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.agents.models import AIAgent
from apps.audit.models import AuditLog
from apps.compliance.evidence import MET, MISSING, NOT_APPLICABLE, PARTIAL, Subject, check_evaluation, check_kill_switch
from apps.compliance.frameworks import REGIMES, REQUIREMENTS
from apps.compliance.models import Attestation, EUCategory, RegulatoryScope
from apps.compliance.services import assess, overview
from apps.evaluations.models import EvaluationCase, EvaluationRun, EvaluationSuite, RunStatus, TargetKind
from apps.organizations.models import Organization, OrganizationMembership
from apps.risks.models import Risk

User = get_user_model()


class ComplianceFixture:
    def build(self):
        self.org = Organization.objects.create(name="Compliance Org")
        self.owner = self.member("owner@example.com", Role.AI_DEVELOPER)
        self.risk_manager = self.member("risk@example.com", Role.RISK_MANAGER)
        self.viewer = self.member("viewer@example.com", Role.VIEWER)
        self.agent = AIAgent.objects.create(organization=self.org, name="Claims Agent", agent_id="claims-agent",
                                            owner=self.owner, business_purpose="Triage claims.",
                                            data_classification="confidential")
        self.subject = Subject(self.agent)

    def member(self, email, role):
        user = User.objects.create_user(email=email, password="pw-12345", first_name=email.split("@")[0].title())
        OrganizationMembership.objects.create(organization=self.org, user=user, role=role, is_default=True)
        return user

    def result(self, regime, code):
        assessment = next(a for a in assess(self.subject) if a.regime.key == regime)
        return next(r for r in assessment.results if r.requirement.code == code)


class CatalogTests(TestCase):
    def test_every_requirement_uses_known_checks_or_needs_an_attestation(self):
        from apps.compliance.evidence import CHECKS

        for code, requirement in REQUIREMENTS.items():
            with self.subTest(code):
                self.assertTrue(requirement.checks or requirement.attestation)
                self.assertTrue(set(requirement.checks) <= set(CHECKS))
        self.assertEqual(set(REGIMES), {"eu_ai_act", "iso_42001", "nist_ai_rmf"})


class EvidenceTests(ComplianceFixture, TestCase):
    def setUp(self):
        self.build()

    def test_an_unscoped_system_is_worked_through_as_possibly_high_risk(self):
        eu = next(a for a in assess(self.subject) if a.regime.key == "eu_ai_act")
        self.assertTrue(eu.applies)
        self.assertEqual(eu.scope.eu_category, EUCategory.TO_ASSESS)
        self.assertFalse(any(r.status == NOT_APPLICABLE for r in eu.results))

    def test_a_minimal_risk_system_takes_no_high_risk_obligations(self):
        RegulatoryScope.objects.create(organization=self.org, agent=self.agent, regime="eu_ai_act",
                                       eu_category=EUCategory.LIMITED, rationale="Chatbot")
        self.assertEqual(self.result("eu_ai_act", "EU-AIA-9").status, NOT_APPLICABLE)
        self.assertNotEqual(self.result("eu_ai_act", "EU-AIA-50").status, NOT_APPLICABLE)
        RegulatoryScope.objects.filter(agent=self.agent).update(eu_category=EUCategory.MINIMAL)
        self.assertFalse(next(a for a in assess(self.subject) if a.regime.key == "eu_ai_act").applies)

    def test_risks_need_mitigations(self):
        self.assertEqual(self.result("iso_42001", "ISO42001-6.1.2").status, MISSING)
        Risk.objects.create(organization=self.org, agent=self.agent, title="Wrong triage", likelihood=3, impact=4)
        self.assertEqual(self.result("iso_42001", "ISO42001-6.1.2").checks[1].status, PARTIAL)

    def test_a_recent_passing_evaluation_meets_the_robustness_requirement(self):
        self.assertEqual(check_evaluation(self.subject).status, MISSING)
        suite = EvaluationSuite.objects.create(organization=self.org, name="Suite", target_kind=TargetKind.AGENT)
        EvaluationCase.objects.create(organization=self.org, suite=suite, name="c", prompt="p", scorer="contains",
                                      configuration={"values": ["x"]})
        EvaluationRun.objects.create(organization=self.org, suite=suite, agent=self.agent, status=RunStatus.COMPLETED,
                                     passed=True, score=Decimal("90"), finished_at=timezone.now() - timedelta(days=10))
        self.assertEqual(check_evaluation(self.subject).status, MET)

    def test_kill_switch_conditions_are_evidence_of_oversight(self):
        self.assertEqual(check_kill_switch(self.subject).status, PARTIAL)
        self.agent.kill_switch_conditions = "Any refund above the limit."
        self.agent.save()
        self.assertEqual(check_kill_switch(Subject(self.agent)).status, MET)

    def test_an_attestation_answers_what_the_platform_cannot_see(self):
        self.assertEqual(self.result("eu_ai_act", "EU-AIA-4").status, MISSING)
        Attestation.objects.create(organization=self.org, agent=self.agent, requirement="EU-AIA-4",
                                   statement="Team trained.", attested_by=self.owner)
        self.assertEqual(self.result("eu_ai_act", "EU-AIA-4").status, MET)
        Attestation.objects.filter(agent=self.agent).update(valid_until=timezone.localdate() - timedelta(days=1))
        self.assertEqual(self.result("eu_ai_act", "EU-AIA-4").status, MISSING)

    def test_the_overview_rolls_up_coverage_and_gaps(self):
        data = overview(self.org)
        self.assertEqual(len(data["rows"]), 1)
        self.assertIsNotNone(data["totals"]["iso_42001"]["coverage"])
        self.assertTrue(any(gap["reference"] == "Article 4" for gap in data["gaps"]))


class ComplianceViewTests(ComplianceFixture, TestCase):
    def setUp(self):
        self.build()
        self.system_url = reverse("compliance:system", args=["agent", self.agent.pk])

    def test_pages_render(self):
        self.client.force_login(self.viewer)
        self.assertContains(self.client.get(reverse("compliance:overview")), "Claims Agent")
        response = self.client.get(self.system_url)
        self.assertContains(response, "Article 14")
        self.assertNotContains(response, "Download pack")

    def test_scope_needs_a_governance_role_and_a_rationale(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("compliance:scope", args=["agent", self.agent.pk]),
                         {"regime": "eu_ai_act", "eu_category": "minimal", "rationale": "Internal"})
        self.assertFalse(RegulatoryScope.objects.exists())
        self.client.force_login(self.risk_manager)
        self.client.post(reverse("compliance:scope", args=["agent", self.agent.pk]),
                         {"regime": "eu_ai_act", "eu_category": "high_risk", "rationale": ""})
        self.assertFalse(RegulatoryScope.objects.exists())
        self.client.post(reverse("compliance:scope", args=["agent", self.agent.pk]),
                         {"regime": "eu_ai_act", "eu_category": "high_risk", "rationale": "Annex III use"})
        scope = RegulatoryScope.objects.get()
        self.assertEqual((scope.eu_category, scope.assessed_by), ("high_risk", self.risk_manager))

    def test_the_owner_can_attest(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("compliance:attest", args=["agent", self.agent.pk]),
                         {"requirement": "EU-AIA-4", "status": "met", "statement": "Trained", "valid_until": ""})
        self.assertTrue(Attestation.objects.filter(agent=self.agent, attested_by=self.owner).exists())

    def test_the_evidence_pack_is_restricted_audited_and_complete(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("compliance:pack", args=["agent", self.agent.pk])).status_code, 403)
        self.client.force_login(self.risk_manager)
        html = self.client.get(reverse("compliance:pack", args=["agent", self.agent.pk]))
        self.assertContains(html, "AI System Card")
        response = self.client.get(reverse("compliance:pack_zip", args=["agent", self.agent.pk]))
        self.assertEqual(response["Content-Type"], "application/zip")
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        names = archive.namelist()
        self.assertTrue(any(name.endswith("evidence.json") for name in names))
        self.assertTrue(any(name.endswith("audit-trail.csv") for name in names))
        evidence = json.loads(archive.read(next(n for n in names if n.endswith("evidence.json"))))
        self.assertEqual(evidence["system"]["name"], "Claims Agent")
        self.assertEqual(len(evidence["regimes"]), 3)
        self.assertEqual(AuditLog.objects.filter(action="export").count(), 2)

    def test_another_organizations_system_is_not_found(self):
        other = Organization.objects.create(name="Other")
        foreign = AIAgent.objects.create(organization=other, name="Foreign", agent_id="foreign")
        self.client.force_login(self.risk_manager)
        self.assertEqual(self.client.get(reverse("compliance:system", args=["agent", foreign.pk])).status_code, 404)
