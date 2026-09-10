from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.agents.models import AIAgent
from apps.core.constants import RiskLevel
from apps.organizations.models import Organization, OrganizationMembership
from apps.risks.models import Risk, RiskCategory, RiskStatus, level_for_score
from apps.risks.services import RiskService

User = get_user_model()


class RiskScoringTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Risk Org")

    def _risk(self, **kwargs):
        defaults = {
            "organization": self.organization,
            "title": f"Risk {Risk.objects.count() + 1}",
            "likelihood": 3,
            "impact": 3,
        }
        return Risk.objects.create(**{**defaults, **kwargs})

    def test_the_inherent_score_is_likelihood_times_impact(self):
        self.assertEqual(self._risk(likelihood=4, impact=5).inherent_risk_score, 20)

    def test_the_residual_score_is_derived_when_both_values_are_present(self):
        risk = self._risk(likelihood=5, impact=5, residual_likelihood=2, residual_impact=3)
        self.assertEqual(risk.residual_risk_score, 6)
        self.assertEqual(risk.reduction, 19)

    def test_a_partial_residual_assessment_yields_no_residual_score(self):
        risk = self._risk(residual_likelihood=2)
        self.assertIsNone(risk.residual_risk_score)

    def test_a_hand_entered_score_cannot_drift_from_the_inputs(self):
        risk = self._risk(likelihood=2, impact=2, inherent_risk_score=25)
        self.assertEqual(risk.inherent_risk_score, 4)

    def test_levels_follow_the_configured_thresholds(self):
        # Defaults: <=4 low, <=9 moderate, <=16 high, above that critical.
        self.assertEqual(level_for_score(4), RiskLevel.LOW)
        self.assertEqual(level_for_score(9), RiskLevel.MODERATE)
        self.assertEqual(level_for_score(16), RiskLevel.HIGH)
        self.assertEqual(level_for_score(20), RiskLevel.CRITICAL)

    def test_thresholds_are_configurable(self):
        from django.test import override_settings

        tightened = {
            "DEFAULT_CURRENCY": "USD",
            "RISK_THRESHOLDS": {"LOW": 2, "MODERATE": 4, "HIGH": 8},
            "AGENT_AUTONOMY_APPROVAL_THRESHOLD": 3,
        }
        with override_settings(CONTROL_TOWER=tightened):
            self.assertEqual(level_for_score(9), RiskLevel.CRITICAL)

    def test_the_effective_level_prefers_the_residual_assessment(self):
        risk = self._risk(likelihood=5, impact=5, residual_likelihood=1, residual_impact=1)
        self.assertEqual(risk.inherent_level, RiskLevel.CRITICAL)
        self.assertEqual(risk.effective_level, RiskLevel.LOW)
        self.assertEqual(risk.effective_score, 1)

    def test_the_effective_level_falls_back_to_inherent(self):
        self.assertEqual(self._risk(likelihood=5, impact=5).effective_level, RiskLevel.CRITICAL)


class RiskIdentifierTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Id Org")

    def test_identifiers_are_sequential(self):
        first = Risk.objects.create(organization=self.organization, title="One")
        second = Risk.objects.create(organization=self.organization, title="Two")
        self.assertEqual(first.risk_id, "RISK-0001")
        self.assertEqual(second.risk_id, "RISK-0002")

    def test_identifiers_restart_per_organization(self):
        other = Organization.objects.create(name="Other Id Org")
        Risk.objects.create(organization=self.organization, title="One")
        theirs = Risk.objects.create(organization=other, title="Theirs")
        self.assertEqual(theirs.risk_id, "RISK-0001")

    def test_an_existing_identifier_is_preserved(self):
        risk = Risk.objects.create(
            organization=self.organization, title="Fixed", risk_id="RISK-9999"
        )
        risk.save()
        self.assertEqual(risk.risk_id, "RISK-9999")


class RiskServiceTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Service Org")
        self.agent = AIAgent.objects.create(
            organization=self.organization, name="Risky Agent", agent_id="risky"
        )

        Risk.objects.create(
            organization=self.organization, title="Critical open", likelihood=5, impact=5,
            agent=self.agent, status=RiskStatus.MITIGATING,
            risk_category=RiskCategory.AUTONOMOUS_AGENT,
        )
        Risk.objects.create(
            organization=self.organization, title="High open", likelihood=4, impact=4,
            status=RiskStatus.IDENTIFIED, risk_category=RiskCategory.PRIVACY,
        )
        Risk.objects.create(
            organization=self.organization, title="Mitigated down", likelihood=5, impact=5,
            residual_likelihood=1, residual_impact=2, mitigation="Controls in place.",
            status=RiskStatus.MONITORING, risk_category=RiskCategory.PRIVACY,
        )
        Risk.objects.create(
            organization=self.organization, title="Closed one", likelihood=5, impact=5,
            status=RiskStatus.CLOSED,
        )
        self.service = RiskService(self.organization)

    def test_summary_counts_only_open_risks(self):
        summary = self.service.summary()
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["open"], 3)

    def test_summary_uses_the_effective_level(self):
        # The mitigated risk counts as low, not critical.
        summary = self.service.summary()
        self.assertEqual(summary["critical"], 1)
        self.assertEqual(summary["by_level"][RiskLevel.LOW], 1)

    def test_unmitigated_risks_are_counted(self):
        self.assertEqual(self.service.summary()["unmitigated"], 2)

    def test_the_heat_map_is_five_by_five(self):
        grid = self.service.heat_map()
        self.assertEqual(len(grid), 5)
        self.assertTrue(all(len(row) == 5 for row in grid))

    def test_impact_runs_down_the_rows(self):
        grid = self.service.heat_map()
        self.assertEqual(grid[0][0]["impact"], 5)
        self.assertEqual(grid[4][0]["impact"], 1)

    def test_a_risk_lands_in_the_right_cell(self):
        grid = self.service.heat_map()
        top_right = grid[0][4]
        self.assertEqual((top_right["likelihood"], top_right["impact"]), (5, 5))
        self.assertEqual(top_right["count"], 2)
        self.assertEqual(top_right["level"], RiskLevel.CRITICAL)

    def test_the_residual_map_plots_the_mitigated_position(self):
        grid = self.service.heat_map(residual=True)
        # Residual likelihood 1, impact 2 -> row for impact 2, first column.
        cell = grid[3][0]
        self.assertEqual((cell["likelihood"], cell["impact"]), (1, 2))
        self.assertEqual(cell["count"], 1)

    def test_risks_without_a_residual_assessment_are_not_plotted(self):
        total = sum(cell["count"] for row in self.service.heat_map(residual=True) for cell in row)
        self.assertEqual(total, 1)

    def test_category_breakdown(self):
        rows = {row["label"]: row["count"] for row in self.service.by_category()}
        self.assertEqual(rows["Privacy"], 2)


class RiskViewTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Risk View Org")
        self.user = User.objects.create_user(email="risk@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.user, role=Role.RISK_MANAGER, is_default=True
        )
        self.client.force_login(self.user)

        self.risk = Risk.objects.create(
            organization=self.organization, title="Visible risk", likelihood=4, impact=4,
            risk_category=RiskCategory.SECURITY,
        )
        self.other_org = Organization.objects.create(name="Rival Risks")
        self.foreign = Risk.objects.create(organization=self.other_org, title="Foreign risk")

    def test_the_register_lists_only_this_organization(self):
        response = self.client.get(reverse("risks:list"))
        self.assertContains(response, "Visible risk")
        self.assertNotContains(response, "Foreign risk")

    def test_a_foreign_risk_returns_404(self):
        response = self.client.get(reverse("risks:detail", args=[self.foreign.pk]))
        self.assertEqual(response.status_code, 404)

    def test_the_detail_page_renders(self):
        response = self.client.get(reverse("risks:detail", args=[self.risk.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.risk.risk_id)

    def test_the_matrix_renders(self):
        response = self.client.get(reverse("risks:matrix"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["grid"]), 5)

    def test_the_matrix_can_show_residual_scores(self):
        response = self.client.get(reverse("risks:matrix"), {"view": "residual"})
        self.assertTrue(response.context["show_residual"])

    def test_filtering_by_category(self):
        Risk.objects.create(
            organization=self.organization, title="Cost risk",
            risk_category=RiskCategory.COST,
        )
        response = self.client.get(reverse("risks:list"), {"category": RiskCategory.SECURITY})
        self.assertContains(response, "Visible risk")
        self.assertNotContains(response, "Cost risk")

    def test_anonymous_users_are_redirected(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("risks:list")).status_code, 302)


class DashboardRiskMetricsTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Dash Risk Org")
        self.user = User.objects.create_user(email="dashrisk@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.user, role=Role.ORG_ADMIN, is_default=True
        )
        self.client.force_login(self.user)

        Risk.objects.create(
            organization=self.organization, title="Critical", likelihood=5, impact=5,
            status=RiskStatus.IDENTIFIED,
        )
        Risk.objects.create(
            organization=self.organization, title="Overdue", likelihood=2, impact=2,
            review_date=timezone.now().date() - timedelta(days=5), status=RiskStatus.MONITORING,
        )

    def test_open_and_critical_risk_metrics_are_real(self):
        response = self.client.get(reverse("dashboard:home"))
        metrics = {m.label: m.value for m in response.context["data"].metrics}
        self.assertEqual(metrics["Open Risks"], 2)
        self.assertEqual(metrics["Critical Risks"], 1)

    def test_critical_and_overdue_risks_reach_the_attention_panel(self):
        response = self.client.get(reverse("dashboard:home"))
        titles = " ".join(i.title for i in response.context["data"].attention)
        self.assertIn("critical risk", titles)
        self.assertIn("overdue for review", titles)
