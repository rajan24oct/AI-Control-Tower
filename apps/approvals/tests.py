from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.agents.models import AIAgent, AutonomyLevel
from apps.applications_registry.models import AIApplication
from apps.approvals.models import ApprovalRequest, ApprovalRule, ApprovalStatus, TriggerAction
from apps.approvals.services import ApprovalContext, ApprovalRuleEngine, ApprovalService
from apps.core.constants import DataClassification, Environment, LifecycleStatus, RiskLevel
from apps.executions.models import ApprovalState, Execution, ExecutionStatusChoices
from apps.executions.services import ExecutionService
from apps.models_registry.models import AIModel
from apps.organizations.models import Organization, OrganizationMembership
from apps.providers.models import AIProvider
from integrations.base.events import EventType

User = get_user_model()


class ApprovalFixtureMixin:
    def build_estate(self, name="Approval Org"):
        self.organization = Organization.objects.create(name=name)
        self.provider = AIProvider.objects.create(
            organization=self.organization, name="Mock", adapter_key="mock", is_approved=True
        )
        self.model = AIModel.objects.create(
            organization=self.organization, provider=self.provider,
            model_name="Mock Large", model_id="mock-large", approved_for_use=True,
        )
        self.agent = AIAgent.objects.create(
            organization=self.organization,
            name="Support Agent",
            agent_id="support-agent",
            provider=self.provider,
            primary_model=self.model,
            autonomy_level=AutonomyLevel.RECOMMEND,
            risk_level=RiskLevel.MODERATE,
            environment=Environment.PRODUCTION,
            data_classification=DataClassification.INTERNAL,
            restricted_tools=["send_email", "issue_refund"],
        )
        self.application = AIApplication.objects.create(
            organization=self.organization, name="Support Portal"
        )
        self.application.agents.add(self.agent)

    def rule(self, **kwargs):
        defaults = {
            "organization": self.organization,
            "name": f"Rule {ApprovalRule.objects.count() + 1}",
            "status": LifecycleStatus.ACTIVE,
        }
        return ApprovalRule.objects.create(**{**defaults, **kwargs})


class RuleEngineTests(ApprovalFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()

    def test_no_rules_means_no_approval(self):
        decision = ApprovalRuleEngine.evaluate(
            self.organization, ApprovalContext(agent=self.agent)
        )
        self.assertFalse(decision.requires_approval)

    def test_a_rule_with_no_conditions_never_matches(self):
        """An empty rule gating everything would be worse than one doing nothing."""
        self.rule(name="Empty")
        decision = ApprovalRuleEngine.evaluate(
            self.organization, ApprovalContext(agent=self.agent)
        )
        self.assertFalse(decision.requires_approval)

    def test_autonomy_threshold(self):
        self.rule(name="High autonomy", min_autonomy_level=3)

        low = ApprovalRuleEngine.evaluate(self.organization, ApprovalContext(agent=self.agent))
        self.assertFalse(low.requires_approval)

        self.agent.autonomy_level = AutonomyLevel.AUTONOMOUS
        self.agent.save()
        high = ApprovalRuleEngine.evaluate(self.organization, ApprovalContext(agent=self.agent))
        self.assertTrue(high.requires_approval)

    def test_risk_level_condition(self):
        self.rule(name="Critical", risk_levels=[RiskLevel.CRITICAL])
        self.assertFalse(
            ApprovalRuleEngine.evaluate(
                self.organization, ApprovalContext(agent=self.agent)
            ).requires_approval
        )

        self.agent.risk_level = RiskLevel.CRITICAL
        self.agent.save()
        self.assertTrue(
            ApprovalRuleEngine.evaluate(
                self.organization, ApprovalContext(agent=self.agent)
            ).requires_approval
        )

    def test_restricted_tool_condition(self):
        self.rule(name="Restricted tool", match_restricted_tool=True)

        allowed = ApprovalRuleEngine.evaluate(
            self.organization, ApprovalContext(agent=self.agent, tool="web_search")
        )
        restricted = ApprovalRuleEngine.evaluate(
            self.organization, ApprovalContext(agent=self.agent, tool="send_email")
        )
        self.assertFalse(allowed.requires_approval)
        self.assertTrue(restricted.requires_approval)

    def test_action_condition(self):
        self.rule(name="Sensitive", actions=[TriggerAction.FINANCIAL_TRANSACTION])
        decision = ApprovalRuleEngine.evaluate(
            self.organization,
            ApprovalContext(agent=self.agent, action=TriggerAction.FINANCIAL_TRANSACTION),
        )
        self.assertTrue(decision.requires_approval)

    def test_cost_threshold_condition(self):
        self.rule(name="Expensive", cost_threshold=Decimal("5"))

        cheap = ApprovalRuleEngine.evaluate(
            self.organization, ApprovalContext(agent=self.agent, estimated_cost=Decimal("1"))
        )
        pricey = ApprovalRuleEngine.evaluate(
            self.organization, ApprovalContext(agent=self.agent, estimated_cost=Decimal("9"))
        )
        self.assertFalse(cheap.requires_approval)
        self.assertTrue(pricey.requires_approval)

    def test_unapproved_provider_condition(self):
        self.rule(name="Unapproved provider", match_unapproved_provider=True)
        self.assertFalse(
            ApprovalRuleEngine.evaluate(
                self.organization, ApprovalContext(agent=self.agent)
            ).requires_approval
        )

        self.provider.is_approved = False
        self.provider.save()
        self.agent.refresh_from_db()
        self.assertTrue(
            ApprovalRuleEngine.evaluate(
                self.organization, ApprovalContext(agent=self.agent)
            ).requires_approval
        )

    def test_all_conditions_on_a_rule_must_hold(self):
        self.rule(
            name="Restricted data in production",
            data_classifications=[DataClassification.RESTRICTED],
            environments=[Environment.PRODUCTION],
        )
        # Production, but the data is only internal.
        self.assertFalse(
            ApprovalRuleEngine.evaluate(
                self.organization, ApprovalContext(agent=self.agent)
            ).requires_approval
        )

        self.agent.data_classification = DataClassification.RESTRICTED
        self.agent.save()
        self.assertTrue(
            ApprovalRuleEngine.evaluate(
                self.organization, ApprovalContext(agent=self.agent)
            ).requires_approval
        )

    def test_severity_is_the_highest_matched(self):
        self.rule(name="Moderate", min_autonomy_level=0, severity=RiskLevel.MODERATE)
        self.rule(name="Critical", min_autonomy_level=0, severity=RiskLevel.CRITICAL)

        decision = ApprovalRuleEngine.evaluate(
            self.organization, ApprovalContext(agent=self.agent)
        )
        self.assertEqual(decision.severity, RiskLevel.CRITICAL)
        self.assertEqual(len(decision.matched), 2)

    def test_inactive_rules_are_ignored(self):
        self.rule(name="Disabled", min_autonomy_level=0, status=LifecycleStatus.INACTIVE)
        self.assertFalse(
            ApprovalRuleEngine.evaluate(
                self.organization, ApprovalContext(agent=self.agent)
            ).requires_approval
        )

    def test_rules_do_not_leak_across_organizations(self):
        other = Organization.objects.create(name="Rival Rules")
        ApprovalRule.objects.create(
            organization=other, name="Theirs", min_autonomy_level=0,
            status=LifecycleStatus.ACTIVE,
        )
        self.assertFalse(
            ApprovalRuleEngine.evaluate(
                self.organization, ApprovalContext(agent=self.agent)
            ).requires_approval
        )


class ApprovalServiceTests(ApprovalFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate()
        self.approver = User.objects.create_user(email="approver@example.com")
        self.execution = ExecutionService.record(
            organization=self.organization,
            external_id="run-approval",
            agent="support-agent",
            status=ExecutionStatusChoices.RUNNING,
        )

    def test_requesting_pauses_the_execution(self):
        ApprovalService.request(
            organization=self.organization,
            requested_action="Send an email",
            context=ApprovalContext(agent=self.agent, execution=self.execution),
        )
        self.execution.refresh_from_db()

        self.assertEqual(self.execution.status, ExecutionStatusChoices.WAITING_FOR_APPROVAL)
        self.assertEqual(self.execution.approval_status, ApprovalState.PENDING)

    def test_requesting_records_a_timeline_event(self):
        ApprovalService.request(
            organization=self.organization,
            requested_action="Send an email",
            context=ApprovalContext(agent=self.agent, execution=self.execution),
        )
        types = set(self.execution.events.values_list("event_type", flat=True))
        self.assertIn(str(EventType.APPROVAL_REQUESTED), types)

    def test_an_engine_emitted_request_marker_is_not_duplicated(self):
        """Engines often emit APPROVAL_REQUESTED before calling the API."""
        ExecutionService.append_events(
            self.execution, [{"event_type": str(EventType.APPROVAL_REQUESTED)}]
        )
        ApprovalService.request(
            organization=self.organization,
            requested_action="Send an email",
            context=ApprovalContext(agent=self.agent, execution=self.execution),
        )
        markers = self.execution.events.filter(
            event_type=str(EventType.APPROVAL_REQUESTED)
        ).count()
        self.assertEqual(markers, 1)

    def test_a_second_request_after_a_decision_records_its_own_marker(self):
        ApprovalService.request(
            organization=self.organization, requested_action="First",
            context=ApprovalContext(agent=self.agent, execution=self.execution),
        )
        first = ApprovalRequest.objects.get()
        ApprovalService.approve(first, user=self.approver)

        ApprovalService.request(
            organization=self.organization, requested_action="Second",
            context=ApprovalContext(agent=self.agent, execution=self.execution),
        )
        markers = self.execution.events.filter(
            event_type=str(EventType.APPROVAL_REQUESTED)
        ).count()
        self.assertEqual(markers, 2)

    def test_request_ids_are_sequential(self):
        first = ApprovalService.request(
            organization=self.organization, requested_action="One",
            context=ApprovalContext(agent=self.agent),
        )
        second = ApprovalService.request(
            organization=self.organization, requested_action="Two",
            context=ApprovalContext(agent=self.agent),
        )
        self.assertEqual(first.request_id, "APR-0001")
        self.assertEqual(second.request_id, "APR-0002")

    def test_approving_records_the_decision_and_releases_the_pause(self):
        request = ApprovalService.request(
            organization=self.organization, requested_action="Send an email",
            context=ApprovalContext(agent=self.agent, execution=self.execution),
        )
        ApprovalService.approve(request, user=self.approver, notes="Fine.")

        request.refresh_from_db()
        self.execution.refresh_from_db()

        self.assertEqual(request.status, ApprovalStatus.APPROVED)
        self.assertEqual(request.decision_by, self.approver)
        self.assertEqual(self.execution.approval_status, ApprovalState.APPROVED)
        self.assertEqual(self.execution.status, ExecutionStatusChoices.RUNNING)
        self.assertIn(
            str(EventType.APPROVAL_GRANTED),
            set(self.execution.events.values_list("event_type", flat=True)),
        )

    def test_rejecting_cancels_the_execution(self):
        request = ApprovalService.request(
            organization=self.organization, requested_action="Send an email",
            context=ApprovalContext(agent=self.agent, execution=self.execution),
        )
        ApprovalService.reject(request, user=self.approver, notes="Out of bounds.")
        self.execution.refresh_from_db()

        self.assertEqual(self.execution.status, ExecutionStatusChoices.CANCELLED)
        self.assertEqual(self.execution.approval_status, ApprovalState.REJECTED)

    def test_deciding_twice_is_refused(self):
        request = ApprovalService.request(
            organization=self.organization, requested_action="Send an email",
            context=ApprovalContext(agent=self.agent),
        )
        ApprovalService.approve(request, user=self.approver)
        with self.assertRaises(ValueError):
            ApprovalService.reject(request, user=self.approver)

    def test_decisions_are_audited(self):
        from apps.audit.models import AuditLog

        request = ApprovalService.request(
            organization=self.organization, requested_action="Send an email",
            context=ApprovalContext(agent=self.agent),
        )
        ApprovalService.approve(request, user=self.approver)
        self.assertTrue(
            AuditLog.objects.filter(action="approve", resource_id=str(request.pk)).exists()
        )

    def test_evaluate_and_request_only_raises_when_a_rule_matches(self):
        decision, request = ApprovalService.evaluate_and_request(
            organization=self.organization,
            requested_action="Harmless read",
            context=ApprovalContext(agent=self.agent),
        )
        self.assertFalse(decision.requires_approval)
        self.assertIsNone(request)

        self.rule(name="Everything", min_autonomy_level=0)
        decision, request = ApprovalService.evaluate_and_request(
            organization=self.organization,
            requested_action="Now gated",
            context=ApprovalContext(agent=self.agent),
        )
        self.assertTrue(decision.requires_approval)
        self.assertIsNotNone(request)
        self.assertEqual(request.matched_rules[0]["name"], "Everything")

    def test_stale_requests_expire(self):
        request = ApprovalService.request(
            organization=self.organization, requested_action="Forgotten",
            context=ApprovalContext(agent=self.agent, execution=self.execution),
            expires_in_hours=1,
        )
        ApprovalRequest.objects.filter(pk=request.pk).update(
            expires_at=timezone.now() - timedelta(hours=2)
        )

        self.assertEqual(ApprovalService.expire_stale(), 1)
        request.refresh_from_db()
        self.execution.refresh_from_db()

        self.assertEqual(request.status, ApprovalStatus.EXPIRED)
        self.assertEqual(self.execution.status, ExecutionStatusChoices.TIMED_OUT)

    def test_a_request_within_its_deadline_does_not_expire(self):
        ApprovalService.request(
            organization=self.organization, requested_action="Fresh",
            context=ApprovalContext(agent=self.agent), expires_in_hours=24,
        )
        self.assertEqual(ApprovalService.expire_stale(), 0)

    def test_the_application_is_inferred_from_the_agent(self):
        request = ApprovalService.request(
            organization=self.organization, requested_action="Infer",
            context=ApprovalContext(agent=self.agent),
        )
        self.assertEqual(request.application, self.application)


class ApprovalViewTests(ApprovalFixtureMixin, TestCase):
    def setUp(self):
        self.build_estate("Approval View Org")
        self.approver = User.objects.create_user(email="dec@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.approver, role=Role.APPROVER, is_default=True
        )
        self.viewer = User.objects.create_user(email="look@example.com", password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=self.viewer, role=Role.VIEWER, is_default=True
        )

        self.request_obj = ApprovalService.request(
            organization=self.organization,
            requested_action="Send a customer email",
            context=ApprovalContext(agent=self.agent),
            risk_level=RiskLevel.HIGH,
        )

    def test_the_centre_shows_the_pending_queue(self):
        self.client.force_login(self.approver)
        response = self.client.get(reverse("approvals:centre"))
        self.assertContains(response, "Send a customer email")
        self.assertTrue(response.context["can_decide"])

    def test_an_approver_can_approve_from_the_ui(self):
        self.client.force_login(self.approver)
        self.client.post(
            reverse("approvals:decide", args=[self.request_obj.pk, "approve"]),
            {"notes": "Looks fine"},
        )
        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.status, ApprovalStatus.APPROVED)
        self.assertEqual(self.request_obj.decision_notes, "Looks fine")

    def test_a_viewer_cannot_decide(self):
        self.client.force_login(self.viewer)
        self.client.post(reverse("approvals:decide", args=[self.request_obj.pk, "approve"]))
        self.request_obj.refresh_from_db()
        self.assertEqual(self.request_obj.status, ApprovalStatus.PENDING)

    def test_the_viewer_ui_hides_the_decision_controls(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("approvals:centre"))
        self.assertFalse(response.context["can_decide"])

    def test_a_foreign_request_returns_404(self):
        other = Organization.objects.create(name="Rival Approvals")
        foreign = ApprovalRequest.objects.create(
            organization=other, requested_action="Theirs"
        )
        self.client.force_login(self.approver)
        self.assertEqual(
            self.client.get(reverse("approvals:detail", args=[foreign.pk])).status_code, 404
        )

    def test_the_detail_page_renders(self):
        self.client.force_login(self.approver)
        response = self.client.get(reverse("approvals:detail", args=[self.request_obj.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.request_obj.request_id)

    def test_deciding_an_already_decided_request_shows_an_error(self):
        self.client.force_login(self.approver)
        url = reverse("approvals:decide", args=[self.request_obj.pk, "approve"])
        self.client.post(url)
        response = self.client.post(url, follow=True)
        self.assertContains(response, "already approved")
