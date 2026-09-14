"""IRGF Phase 3: the Control Plane (M12), drift (M13), action limits (link 06), verification (link 07)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.agents.models import AIAgent
from apps.api.models import ServiceAccount
from apps.audit.context import AuditContext, reset_audit_context, set_audit_context
from apps.classification.services import ClassificationService
from apps.controlplane import irgf
from apps.controlplane.models import DriftRecord, DriftStatus, Resolution, Verification, VerificationOutcome
from apps.controlplane.services import ControlPlane, ControlPlaneError, DriftService, Finding
from apps.core.constants import Environment, HealthStatus, LifecycleStatus
from apps.evaluations.models import EvaluationRun, EvaluationSuite, RunStatus, TargetKind
from apps.executions.models import Execution, ExecutionEvent
from apps.lifecycle.models import AuthorizationStatus, ChangeStatus, ExceptionStatus
from apps.lifecycle.services import AuthorizationService, ChangeService, ExceptionService
from apps.mcp_gateway.models import AccessMode, CallStatus, MCPServer, MCPToolCall, MCPTransport
from apps.mcp_gateway.registry import MCPRegistryService
from apps.mcp_gateway.services import MCPGateway, ToolCallRequest
from apps.models_registry.models import AIModel, ModelType
from apps.notifications.models import Notification
from apps.organizations.models import Organization, OrganizationMembership
from apps.policies.models import Policy, PolicyEffect, PolicyType
from apps.providers.models import AIProvider

User = get_user_model()

FIELDS = ("d1_consequence", "d2_autonomy", "d3_reversibility", "d4_exposure",
          "d5_data_sensitivity", "d5_model_uncertainty")

TOOLS = [
    {"name": "search_incidents", "description": "Search incidents.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}},
    {"name": "create_incident", "description": "Open an incident.",
     "inputSchema": {"type": "object", "properties": {"summary": {"type": "string"}}}},
    {"name": "refund", "description": "Refund a customer.",
     "inputSchema": {"type": "object", "properties": {"amount": {"type": "number"}, "customer": {"type": "string"}}}},
]


def aware(*args):
    return timezone.make_aware(datetime(*args), timezone.get_current_timezone())


class ResponseWindowTests(SimpleTestCase):
    def test_windows_follow_irgf_chapter_19(self):
        self.assertTrue(irgf.response_for(irgf.STRUCTURAL, 4).same_day)
        self.assertEqual(irgf.response_for(irgf.CONFIGURATION, 3).working_days, 5)
        self.assertEqual(irgf.response_for(irgf.SECURITY, 2).working_days, 10)
        self.assertEqual(irgf.response_for(irgf.COST, 1).route, irgf.DIGEST)
        self.assertEqual(irgf.response_for(irgf.POLICY, 2).route, irgf.BATCH)
        self.assertEqual(irgf.response_for(irgf.BEHAVIORAL, 3).action, irgf.RESCORE)
        self.assertEqual(irgf.response_for(irgf.POLICY, 4).action, irgf.COMPLIANCE_REVIEW)

    def test_a_boundary_incident_is_immediate_at_any_tier(self):
        response = irgf.response_for(irgf.SECURITY, 1, incident=True)
        self.assertEqual(response.route, irgf.IMMEDIATE)
        self.assertTrue(response.same_day)

    def test_an_unclassified_system_is_triaged_as_tier_3(self):
        self.assertEqual(irgf.response_for(irgf.CONFIGURATION, None).working_days, 5)

    def test_working_days_skip_weekends(self):
        five_days = irgf.response_for(irgf.CONFIGURATION, 3)
        same_day = irgf.response_for(irgf.STRUCTURAL, 4)
        # Friday 11 September 2026, mid-morning.
        self.assertEqual(irgf.due_at(five_days, aware(2026, 9, 11, 10)), aware(2026, 9, 18, 18))
        # Found on a Saturday: due by the end of Monday.
        self.assertEqual(irgf.due_at(same_day, aware(2026, 9, 12, 10)), aware(2026, 9, 14, 18))
        # Found after the business day ended: due the next day.
        self.assertEqual(irgf.due_at(same_day, aware(2026, 9, 15, 19)), aware(2026, 9, 16, 18))
        self.assertIsNone(irgf.due_at(irgf.response_for(irgf.STRUCTURAL, 1), aware(2026, 9, 15, 9)))

    @override_settings(CONTROL_PLANE={"VERIFICATION_CEILING_SECONDS": {4: 60}})
    def test_verification_ceilings_can_be_set_per_tier(self):
        self.assertEqual(irgf.verification_ceiling(4), 60)
        self.assertEqual(irgf.verification_ceiling(3), 900)


class Estate(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Plane Org")
        self.owner = self.member("owner@example.com", Role.AI_DEVELOPER)
        self.developer = self.member("dev@example.com", Role.AI_DEVELOPER)
        self.risk = self.member("risk@example.com", Role.RISK_MANAGER)
        self.body = self.member("body@example.com", Role.GOVERNANCE_MANAGER)
        self.viewer = self.member("viewer@example.com", Role.VIEWER)
        provider = AIProvider.objects.create(
            organization=self.organization, name="Mock", adapter_key="mock", is_approved=True,
            status=LifecycleStatus.ACTIVE, health_status=HealthStatus.HEALTHY,
        )
        self.model = AIModel.objects.create(
            organization=self.organization, provider=provider, model_name="Mock Large",
            model_id="mock-large", model_family="mock", model_type=ModelType.CHAT,
            approved_for_use=True, status=LifecycleStatus.ACTIVE,
        )
        self.other_model = AIModel.objects.create(
            organization=self.organization, provider=provider, model_name="Mock Small",
            model_id="mock-small", model_family="mock", model_type=ModelType.CHAT,
            approved_for_use=True, status=LifecycleStatus.ACTIVE,
        )
        self.server = MCPServer.objects.create(
            organization=self.organization, name="Service Desk", transport=MCPTransport.MOCK,
            options={"tools": TOOLS}, is_approved=True, access_mode=AccessMode.OPEN,
            status=LifecycleStatus.ACTIVE, data_classification="internal",
        )
        MCPRegistryService.sync(self.server)
        for tool in self.server.tools.all():
            MCPRegistryService.review(tool, enable=True, actor=None)
        self.refund = self.server.tools.get(name="refund")
        MCPRegistryService.set_limit_arguments(
            self.refund, value_argument="amount", counterparty_argument="customer", actor=None
        )
        self.agent = AIAgent.objects.create(
            organization=self.organization, name="Desk Agent", agent_id="desk-agent",
            owner=self.owner, provider=provider, primary_model=self.model,
            environment=Environment.PRODUCTION, status=LifecycleStatus.ACTIVE, autonomy_level=2,
            data_classification="internal",
            allowed_tools=["service-desk__search_incidents", "service-desk__refund"],
            action_limits={"max_value_per_action": 100, "max_value_per_day": 150, "currency": "USD"},
            kill_switch_conditions="Any refund over the limit.", business_purpose="Answers the service desk.",
        )

    def member(self, email, role):
        user = User.objects.create_user(email=email, password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=user, role=role, is_default=True
        )
        return user

    def classify(self, scores=(2, 2, 2, 2, 2, 2)):
        record = ClassificationService.score(
            self.agent, scores=dict(zip(FIELDS, scores)), rationale="Evidence.", actor=self.developer
        )
        ClassificationService.countersign(record, actor=self.risk)
        self.agent.refresh_from_db()
        return record

    def authorize(self):
        self.classify()
        authorization = AuthorizationService.request(
            self.agent, actor=self.owner, oversight_point="A desk lead reviews every reply.",
            assurance_summary="Sampled 100 tickets.",
        )
        AuthorizationService.sign(authorization, actor=self.owner)
        authorization.refresh_from_db()
        self.agent.refresh_from_db()
        return authorization

    def call(self, tool="search_incidents", arguments=None, *, service_account=None):
        request = ToolCallRequest(
            name=f"service-desk__{tool}", arguments={"query": "vpn"} if arguments is None else arguments,
            agent=self.agent,
        )
        with self.captureOnCommitCallbacks(execute=True):
            return MCPGateway.call(self.organization, request, service_account=service_account)

    def reported_run(self, **fields):
        fields.setdefault("model", self.model)
        fields.setdefault("environment", Environment.PRODUCTION)
        now = timezone.now()
        return Execution.objects.create(
            organization=self.organization, agent=self.agent, external_id=f"run-{Execution.objects.count()}",
            status="completed", started_at=now, ended_at=now, **fields,
        )

    def drift(self, signal):
        return DriftRecord.objects.filter(agent=self.agent, signal=signal)


class ActionLimitTests(Estate):
    """Link 06: preventive, enforced at the MCP Gateway before a call goes out."""

    def test_a_value_over_the_per_action_limit_is_refused_before_the_call(self):
        outcome = self.call("refund", {"amount": 250, "customer": "C-1"})
        self.assertEqual(outcome.status, CallStatus.BLOCKED)
        self.assertEqual(outcome.limit, "max_value_per_action")
        self.assertFalse(outcome.dispatched)
        call = MCPToolCall.objects.get()
        self.assertEqual(call.limit_breached, "max_value_per_action")
        self.assertEqual(call.action_value, Decimal("250"))

    def test_the_daily_value_accumulates(self):
        self.assertEqual(self.call("refund", {"amount": 90, "customer": "C-1"}).status, CallStatus.SUCCEEDED)
        outcome = self.call("refund", {"amount": 70, "customer": "C-2"})
        self.assertEqual(outcome.limit, "max_value_per_day")

    def test_a_counterparty_outside_the_list_is_refused(self):
        self.agent.action_limits = {**self.agent.action_limits, "allowed_counterparties": ["C-1"]}
        self.agent.save()
        self.assertEqual(self.call("refund", {"amount": 10, "customer": "C-9"}).limit, "allowed_counterparties")
        self.assertEqual(self.call("refund", {"amount": 10, "customer": "c-1"}).status, CallStatus.SUCCEEDED)

    def test_the_hourly_rate_counts_actions_that_reached_a_tool(self):
        self.agent.action_limits = {"max_actions_per_hour": 2}
        self.agent.save()
        self.call()
        self.call()
        self.assertEqual(self.call().limit, "max_actions_per_hour")

    def test_a_value_that_cannot_be_read_is_refused(self):
        self.assertEqual(self.call("refund", {"amount": "lots", "customer": "C-1"}).status, CallStatus.BLOCKED)

    def test_an_agent_without_limits_is_not_held(self):
        self.agent.action_limits = {}
        self.agent.save()
        self.assertEqual(self.call("refund", {"amount": 5000, "customer": "C-1"}).status, CallStatus.SUCCEEDED)

    def test_only_a_known_argument_can_carry_the_value(self):
        with self.assertRaises(ValueError):
            MCPRegistryService.set_limit_arguments(self.refund, value_argument="total", counterparty_argument="", actor=None)


class VerificationTests(Estate):
    """Link 07, and the M12 comparison behind it."""

    def test_a_tool_call_is_verified_as_it_is_recorded(self):
        self.authorize()
        self.call()
        verification = Verification.objects.get()
        self.assertEqual(verification.outcome, VerificationOutcome.CONFORMS)
        self.assertEqual(verification.tier, 2)
        self.assertEqual(verification.ceiling_seconds, irgf.verification_ceiling(2))
        self.assertFalse(verification.late)
        self.assertFalse(DriftRecord.objects.exists())

    def test_a_tool_outside_the_boundary_is_an_incident(self):
        self.authorize()
        self.call("create_incident", {"summary": "VPN down"})
        record = self.drift("boundary_divergence").get()
        self.assertTrue(record.is_incident)
        self.assertEqual(record.category, irgf.SECURITY)
        self.assertEqual(record.route, irgf.IMMEDIATE)
        self.assertIsNotNone(record.response_due)
        self.assertEqual(record.observed, ["service-desk__create_incident"])
        self.assertTrue(Notification.objects.filter(recipient=self.owner, category="drift").exists())
        self.assertEqual(Verification.objects.get().outcome, VerificationOutcome.DIVERGES)

    def test_recurrences_add_to_one_record(self):
        self.authorize()
        self.call("create_incident", {"summary": "one"})
        self.call("create_incident", {"summary": "two"})
        record = self.drift("boundary_divergence").get()
        self.assertEqual(record.occurrences, 2)
        self.assertEqual(len(record.evidence), 2)

    def test_a_breach_pulls_a_pre_approved_kill_switch_trigger(self):
        self.agent.kill_switch_triggers = ["action_limit_breach"]
        self.agent.save()
        self.authorize()
        self.call("refund", {"amount": 250, "customer": "C-1"})
        record = self.drift("action_limit_breach").get()
        self.assertTrue(record.is_incident)
        self.assertTrue(record.auto_suspended)
        self.agent.refresh_from_db()
        self.assertTrue(self.agent.is_suspended)
        self.assertEqual(self.agent.suspended_by_label, "the platform")

    def test_without_a_trigger_the_agent_keeps_running(self):
        self.authorize()
        self.call("refund", {"amount": 250, "customer": "C-1"})
        self.assertTrue(self.drift("action_limit_breach").exists())
        self.agent.refresh_from_db()
        self.assertFalse(self.agent.is_suspended)

    def test_running_in_production_without_authorization_is_structural_drift(self):
        self.call()
        record = self.drift("unauthorized_deployment").get()
        self.assertEqual(record.category, irgf.STRUCTURAL)
        self.assertEqual(Verification.objects.get().outcome, VerificationOutcome.DIVERGES)

    def test_testing_in_staging_without_authorization_is_not_drift(self):
        self.agent.environment = Environment.STAGING
        self.agent.save()
        self.call()
        self.assertFalse(DriftRecord.objects.exists())
        self.assertEqual(Verification.objects.get().outcome, VerificationOutcome.UNGOVERNED)

    def test_an_identity_not_bound_at_authorization(self):
        bound = ServiceAccount.objects.create(organization=self.organization, name="desk-runtime", role=Role.AI_DEVELOPER)
        bound.agents.add(self.agent)
        self.authorize()
        other = ServiceAccount.objects.create(organization=self.organization, name="batch-runner", role=Role.AI_DEVELOPER)
        self.call(service_account=other)
        record = self.drift("unbound_identity").get()
        self.assertEqual(record.observed, ["batch-runner"])

    def test_data_above_the_authorized_classification(self):
        self.authorize()
        self.server.data_classification = "restricted"
        self.server.save()
        self.call()
        self.assertEqual(self.drift("data_above_classification").get().category, irgf.DATA_LINEAGE)

    def test_a_run_served_by_a_model_outside_the_authorization(self):
        self.authorize()
        verification = ControlPlane.verify_execution(self.reported_run(model=self.other_model))
        self.assertEqual(verification.outcome, VerificationOutcome.DIVERGES)
        record = self.drift("unauthorized_model").get()
        self.assertEqual(record.route, irgf.DIRECT)
        self.assertIsNone(record.response_due)

    def test_tools_a_run_reports_are_checked_too(self):
        self.agent.restricted_tools = ["approve_payout"]
        self.agent.save()
        self.authorize()
        execution = self.reported_run()
        ExecutionEvent.objects.create(
            execution=execution, organization=self.organization, event_type="TOOL_CALL_COMPLETED",
            metadata={"tool": "approve_payout"},
        )
        ControlPlane.verify_execution(execution)
        self.assertTrue(self.drift("restricted_tool").get().is_incident)

    def test_the_sweep_verifies_what_the_hook_missed_once(self):
        self.authorize()
        execution = self.reported_run()
        ControlPlane.sweep(self.organization, settle=timedelta(0))
        ControlPlane.sweep(self.organization, settle=timedelta(0))
        self.assertEqual(Verification.objects.filter(execution=execution).count(), 1)

    def test_a_verification_after_the_ceiling_is_late(self):
        self.authorize()
        execution = self.reported_run()
        Execution.objects.filter(pk=execution.pk).update(created_at=timezone.now() - timedelta(hours=2))
        ControlPlane.sweep(self.organization, settle=timedelta(0))
        self.assertTrue(Verification.objects.get(execution=execution).late)


class ObserverTests(Estate):
    def test_a_failing_evaluation_after_authorization_is_behavioral_drift_once(self):
        self.authorize()
        suite = EvaluationSuite.objects.create(organization=self.organization, name="Desk", target_kind=TargetKind.AGENT)
        EvaluationRun.objects.create(
            organization=self.organization, suite=suite, agent=self.agent, status=RunStatus.COMPLETED,
            finished_at=timezone.now(), passed=False, score=Decimal("61"),
        )
        self.assertEqual(ControlPlane.observe_evaluations(self.organization), 1)
        self.assertEqual(ControlPlane.observe_evaluations(self.organization), 0)
        self.assertEqual(self.drift("evaluation_regression").get().category, irgf.BEHAVIORAL)

    def test_behavioral_drift_at_tier_3_flags_a_rescore(self):
        self.classify((3, 3, 3, 3, 2, 2))
        record, _ = DriftService.record(self.agent, Finding("evaluation_regression", "run", "Regressed."), tier=3)
        self.assertEqual(record.automatic_action, irgf.RESCORE)
        self.assertTrue(ClassificationService.current(self.agent).rescore_required)

    def test_a_policy_changed_after_authorization_waits_for_batch_review(self):
        self.authorize()
        Policy.objects.create(
            organization=self.organization, name="Authorized agents only",
            policy_type=PolicyType.DEPLOYMENT_AUTHORIZATION, effect=PolicyEffect.WARN, configuration={},
        )
        self.assertEqual(ControlPlane.observe_policies(self.organization), 1)
        record = self.drift("policy_changed").get()
        self.assertEqual(record.route, irgf.BATCH)
        self.assertIsNone(record.notified_at)

    def test_rewording_a_policy_is_not_policy_drift(self):
        policy = Policy.objects.create(
            organization=self.organization, name="Prohibited tools", policy_type=PolicyType.TOOL_ACCESS,
            effect=PolicyEffect.DENY, configuration={"blocked_tools": ["transfer_funds"]},
        )
        self.authorize()
        policy.description = "Reworded after the review."
        policy.save()
        self.assertEqual(ControlPlane.observe_policies(self.organization), 0)
        policy.configuration = {"blocked_tools": ["transfer_funds", "export_accounts"]}
        policy.save(update_fields=["configuration", "updated_at"])
        self.assertEqual(ControlPlane.observe_policies(self.organization), 1)


class ResolutionTests(Estate):
    def boundary(self):
        self.authorize()
        self.call("create_incident", {"summary": "VPN down"})
        return self.drift("boundary_divergence").get()

    def test_revert_needs_the_right_person_and_a_note(self):
        record = self.boundary()
        with self.assertRaises(ControlPlaneError):
            DriftService.revert(record, actor=self.viewer, note="Put back.")
        with self.assertRaises(ControlPlaneError):
            DriftService.revert(record, actor=self.owner, note=" ")
        DriftService.revert(record, actor=self.owner, note="The prompt no longer offers the tool.")
        self.assertEqual(record.status, DriftStatus.RESOLVED)
        self.assertEqual(record.resolution, Resolution.REVERT)

    def test_a_recurrence_after_revert_opens_a_new_record(self):
        record = self.boundary()
        DriftService.revert(record, actor=self.owner, note="Fixed.")
        self.call("create_incident", {"summary": "again"})
        fresh = self.drift("boundary_divergence").get(status=DriftStatus.OPEN)
        self.assertEqual(fresh.recurrence_of, record)

    def test_ratifying_a_wider_boundary_goes_through_gate_g4(self):
        record = self.boundary()
        with self.assertRaises(ControlPlaneError):
            DriftService.ratify(record, actor=self.owner, note="It should have it.")

        token = set_audit_context(AuditContext(actor=self.owner, actor_label=self.owner.email))
        try:
            self.agent.allowed_tools = [*self.agent.allowed_tools, "service-desk__create_incident"]
            self.agent.save()
        finally:
            reset_audit_context(token)
        change = self.agent.change_records.get()
        self.assertEqual(change.status, ChangeStatus.AWAITING_G4)

        DriftService.ratify(record, actor=self.owner, change=change)
        self.assertEqual(record.status, DriftStatus.RATIFYING)
        with self.captureOnCommitCallbacks(execute=True):
            ChangeService.decide(change, actor=self.risk, approve=True, delta_assurance_scope="Re-ran the desk suite.")
        record.refresh_from_db()
        self.assertEqual(record.status, DriftStatus.RESOLVED)
        self.assertEqual(record.resolution, Resolution.RATIFY)

    def test_running_without_authorization_is_ratified_at_gate_g3(self):
        self.call()
        record = self.drift("unauthorized_deployment").get()
        with self.assertRaises(ControlPlaneError):
            DriftService.ratify(record, actor=self.owner, note="Fine.")
        self.authorize()
        self.assertEqual(DriftService.settle_structural(self.organization), 1)
        record.refresh_from_db()
        self.assertEqual(record.resolution, Resolution.RATIFY)

    def test_except_asks_for_an_m17_exception_for_the_divergence(self):
        record = self.boundary()
        with self.assertRaises(ControlPlaneError):
            DriftService.except_(record, actor=self.owner, justification="x", duration_days=10)
        DriftService.except_(record, actor=self.risk, justification="Needed until the new desk tool ships.",
                             duration_days=30)
        exception = record.exception
        self.assertIsNone(exception.policy)
        self.assertIn(record.title, exception.divergence)
        self.assertEqual(record.status, DriftStatus.EXCEPTING)
        # A divergence exception sets no policy aside.
        self.assertEqual(ExceptionService.active_policy_ids(self.organization, self.agent), set())

        with self.captureOnCommitCallbacks(execute=True):
            ExceptionService.sign(exception, actor=self.body)
        record.refresh_from_db()
        self.assertEqual(record.status, DriftStatus.EXCEPTED)

        with self.captureOnCommitCallbacks(execute=True):
            ExceptionService.sweep(now=timezone.now() + timedelta(days=31))
        record.refresh_from_db()
        self.assertEqual(record.status, DriftStatus.OPEN)

    def test_escalate_flags_a_rescore_and_can_suspend_the_authorization(self):
        record = self.boundary()
        with self.assertRaises(ControlPlaneError):
            DriftService.escalate(record, actor=self.owner, reason="Wrong tier.")
        DriftService.escalate(record, actor=self.risk, reason="It acts far more than Tier 2 assumed.",
                              suspend_authorization=True)
        self.assertEqual(record.resolution, Resolution.ESCALATE)
        self.assertTrue(ClassificationService.current(self.agent).rescore_required)
        authorization = AuthorizationService.baseline(self.agent)
        self.assertEqual(authorization.status, AuthorizationStatus.SUSPENDED)
        self.assertFalse(authorization.suspended_for_changes)


class RoutingTests(Estate):
    def test_overdue_drift_escalates_to_the_governance_body(self):
        record, _ = DriftService.record(self.agent, Finding("unauthorized_deployment", "production", "Running."), tier=4)
        self.assertEqual(record.route, irgf.DIRECT)
        DriftRecord.objects.filter(pk=record.pk).update(response_due=timezone.now() - timedelta(hours=1))
        self.assertEqual(DriftService.escalate_overdue(self.organization), 1)
        record.refresh_from_db()
        self.assertIsNotNone(record.escalated_at)
        self.assertTrue(Notification.objects.filter(recipient=self.body, category="gate_decision").exists())
        self.assertEqual(DriftService.escalate_overdue(self.organization), 0)

    def test_quiet_routes_wait_for_the_digest(self):
        record, _ = DriftService.record(
            self.agent, Finding("unauthorized_deployment", "production", "Running."), tier=1,
            at=timezone.now() - timedelta(hours=25),
        )
        self.assertEqual(record.route, irgf.DIGEST)
        self.assertFalse(Notification.objects.filter(recipient=self.owner).exists())
        self.assertEqual(DriftService.send_digests(self.organization), 1)
        self.assertTrue(Notification.objects.filter(recipient=self.owner, category="drift").exists())
        record.refresh_from_db()
        self.assertIsNotNone(record.notified_at)


class ReadinessTests(Estate):
    def test_tier_3_needs_an_automatic_kill_switch_trigger_at_gate_g3(self):
        self.classify((3, 3, 3, 3, 2, 2))
        check = {c.key: c for c in AuthorizationService.readiness(self.agent)}["kill_switch"]
        self.assertTrue(check.blocking)
        self.agent.kill_switch_triggers = ["action_limit_breach"]
        check = {c.key: c for c in AuthorizationService.readiness(self.agent)}["kill_switch"]
        self.assertFalse(check.blocking)

    def test_unknown_triggers_are_refused(self):
        from django.core.exceptions import ValidationError

        self.agent.kill_switch_triggers = ["gut_feeling"]
        with self.assertRaises(ValidationError):
            self.agent.full_clean()


class ViewTests(Estate):
    def setUp(self):
        super().setUp()
        self.authorize()
        self.call("create_incident", {"summary": "VPN down"})
        self.record = self.drift("boundary_divergence").get()

    def test_the_pages_render(self):
        self.client.force_login(self.viewer)
        for url in (
            reverse("controlplane:overview"),
            reverse("controlplane:drift"),
            reverse("controlplane:drift") + "?status=all&incident=1",
            reverse("controlplane:drift_detail", args=[self.record.pk]),
            reverse("controlplane:verifications"),
            reverse("inventory:agent_detail", args=[self.agent.pk]),
        ):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_a_viewer_cannot_resolve(self):
        self.client.force_login(self.viewer)
        response = self.client.post(
            reverse("controlplane:drift_resolve", args=[self.record.pk]),
            {"action": "revert", "note": "No."}, HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 403)

    def test_the_owner_reverts_from_the_page(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("controlplane:drift_resolve", args=[self.record.pk]),
                         {"action": "revert", "note": "Removed the tool from the prompt."})
        self.record.refresh_from_db()
        self.assertEqual(self.record.resolution, Resolution.REVERT)

    def test_running_the_control_plane_needs_risk_or_governance(self):
        self.client.force_login(self.viewer)
        response = self.client.post(reverse("controlplane:run"), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 403)
        self.client.force_login(self.risk)
        response = self.client.post(reverse("controlplane:run"), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200)

    def test_the_api_lists_drift(self):
        from apps.api.models import APIKey

        account = ServiceAccount.objects.create(organization=self.organization, name="soc", role=Role.AI_DEVELOPER)
        _, key = APIKey.generate(account)
        response = self.client.get(
            reverse("api:drift-record-list") + f"?agent={self.agent.pk}", HTTP_X_API_KEY=key
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["results"][0]["id"], str(self.record.pk))
