"""IRGF Phase 2: Gate G3 authorization, Gate G4 change, M17 exceptions."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.agents.models import AIAgent
from apps.api.models import APIKey, ServiceAccount
from apps.audit.context import AuditContext, reset_audit_context, set_audit_context
from apps.classification.services import ClassificationService
from apps.core.constants import Environment, HealthStatus, LifecycleStatus
from apps.evaluations.models import EvaluationRun, EvaluationSuite, RunStatus, TargetKind
from apps.lifecycle import decisions, irgf
from apps.lifecycle.models import (
    AuthorizationCondition,
    AuthorizationStatus,
    ChangeStatus,
    DeploymentAuthorization,
    ExceptionStatus,
)
from apps.lifecycle.services import (
    AuthorizationService,
    ChangeService,
    ConditionService,
    ExceptionService,
    LifecycleError,
    readiness,
)
from apps.models_registry.models import AIModel, ModelType
from apps.notifications.models import Notification
from apps.organizations.models import Organization, OrganizationMembership
from apps.policies.models import Policy, PolicyEffect, PolicyType
from apps.policies.services import PolicyContext, PolicyEngine
from apps.providers.models import AIProvider
from apps.risks.models import Risk, RiskCategory

User = get_user_model()

TIER_SCORES = {
    1: (1, 1, 1, 1, 1, 1),
    2: (2, 2, 2, 2, 2, 2),
    3: (3, 3, 3, 3, 2, 2),
    4: (3, 3, 3, 3, 4, 3),
}
FIELDS = ("d1_consequence", "d2_autonomy", "d3_reversibility", "d4_exposure",
          "d5_data_sensitivity", "d5_model_uncertainty")


class ChangeTableTests(SimpleTestCase):
    def test_a_same_family_model_version_is_minor_low_and_material_high(self):
        self.assertEqual(irgf.classify("model_version", 2).classification, irgf.MINOR)
        self.assertEqual(irgf.classify("model_version", 3).classification, irgf.MATERIAL)

    def test_boundary_widening_is_major_and_re_enters_s2(self):
        for change in ("model_family", "autonomy_increase", "oversight_removed", "limits_raised"):
            verdict = irgf.classify(change, 2)
            self.assertEqual(verdict.classification, irgf.MAJOR, change)
            self.assertTrue(verdict.requires_rescore, change)

    def test_a_new_population_or_a_higher_tier_needs_g3_again(self):
        self.assertTrue(irgf.classify("population", 2).requires_reauthorization)
        self.assertTrue(irgf.classify("tier_increase", 2).requires_reauthorization)

    def test_narrowing_is_minor(self):
        for change in ("autonomy_decrease", "tool_removed", "limits_lowered", "tier_decrease"):
            self.assertEqual(irgf.classify(change, 4).classification, irgf.MINOR, change)

    def test_tier_4_decisions_need_two_different_people(self):
        self.assertEqual(len(irgf.rights(irgf.AUTHORIZATION, 4)), 2)
        self.assertIn("two different people", irgf.describe_rights(irgf.AUTHORIZATION, 4))
        self.assertIn("System owner", irgf.describe_rights(irgf.AUTHORIZATION, 1))


class Estate(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Gate Org")
        self.owner = self.member("owner@example.com", Role.AI_DEVELOPER)
        self.developer = self.member("dev@example.com", Role.AI_DEVELOPER)
        self.risk = self.member("risk@example.com", Role.RISK_MANAGER)
        self.body = self.member("body@example.com", Role.GOVERNANCE_MANAGER)
        self.sponsor = self.member("sponsor@example.com", Role.ORG_ADMIN)
        provider = AIProvider.objects.create(
            organization=self.organization, name="Mock", adapter_key="mock", is_approved=True,
            status=LifecycleStatus.ACTIVE, health_status=HealthStatus.HEALTHY,
        )
        self.model = AIModel.objects.create(
            organization=self.organization, provider=provider, model_name="Mock Large",
            model_id="mock-large", model_family="mock", model_type=ModelType.CHAT,
            approved_for_use=True, status=LifecycleStatus.ACTIVE,
        )
        self.agent = AIAgent.objects.create(
            organization=self.organization, name="Claims Agent", agent_id="claims-agent",
            owner=self.owner, provider=provider, primary_model=self.model,
            environment=Environment.PRODUCTION, autonomy_level=2,
            allowed_tools=["claims_db"], action_limits={"max_actions_per_hour": 100},
            kill_switch_conditions="Misrouting above 5% in an hour.",
            kill_switch_triggers=["action_limit_breach"],
            business_purpose="Routes claims.",
        )

    def member(self, email, role):
        user = User.objects.create_user(email=email, password="pw-12345")
        OrganizationMembership.objects.create(
            organization=self.organization, user=user, role=role, is_default=True
        )
        return user

    def classify(self, tier, confirm=True):
        record = ClassificationService.score(
            self.agent, scores=dict(zip(FIELDS, TIER_SCORES[tier])), rationale="Evidence.",
            actor=self.developer,
        )
        if confirm:
            ClassificationService.countersign(record, actor=self.risk)
        self.agent.refresh_from_db()
        return record

    def ready_for_high_tier(self):
        suite = EvaluationSuite.objects.create(
            organization=self.organization, name="Acceptance", target_kind=TargetKind.AGENT
        )
        EvaluationRun.objects.create(
            organization=self.organization, suite=suite, agent=self.agent,
            status=RunStatus.COMPLETED, finished_at=timezone.now(), passed=True,
            score=Decimal("100"), total_cases=1, completed_cases=1, passed_cases=1,
        )
        Risk.objects.create(
            organization=self.organization, risk_id="R-1", title="Misrouted urgent claims",
            risk_category=RiskCategory.HALLUCINATION, agent=self.agent, likelihood=3, impact=4,
            mitigation="Adjuster sign-off on every urgent claim.",
        )
        account = ServiceAccount.objects.create(
            organization=self.organization, name="claims-runtime", role=Role.AI_DEVELOPER
        )
        account.agents.add(self.agent)
        self.agent.refresh_from_db()

    def request(self, **extra):
        payload = {
            "oversight_point": "A senior adjuster reviews urgent claims within an hour.",
            "monitoring_commitments": "Misrouting rate, daily.",
            "assurance_summary": "Sampled 100 claims; 99 routed correctly.",
            **extra,
        }
        return AuthorizationService.request(self.agent, actor=self.owner, **payload)

    def authorize_tier_2(self):
        self.classify(2)
        authorization = self.request()
        AuthorizationService.sign(authorization, actor=self.owner)
        authorization.refresh_from_db()
        return authorization

    def as_actor(self, user):
        return set_audit_context(AuditContext(actor=user, actor_label=user.email))


class DecisionRightsTests(Estate):
    def record(self, tier):
        return DeploymentAuthorization(
            organization=self.organization, agent=self.agent, tier=tier,
            environment=Environment.PRODUCTION, signatures=[],
        )

    def test_the_owner_decides_at_tier_1_2(self):
        self.assertTrue(decisions.check(self.record(2), self.owner).ok)
        self.assertFalse(decisions.check(self.record(2), self.developer).ok)

    def test_tier_3_goes_to_the_body_and_never_the_owner(self):
        self.assertFalse(decisions.check(self.record(3), self.owner).ok)
        verdict = decisions.check(self.record(3), self.body)
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.seat, irgf.BODY)

    def test_tier_4_needs_the_body_then_the_sponsor(self):
        record = self.record(4)
        # The sponsor's seat is second; first comes the body, which the sponsor also holds.
        self.assertEqual(decisions.check(record, self.body).seat, irgf.BODY)
        record.signatures = [decisions.signature(self.body, irgf.BODY)]
        self.assertFalse(decisions.check(record, self.body).ok)
        self.assertFalse(decisions.check(record, self.risk).ok)
        self.assertEqual(decisions.check(record, self.sponsor).seat, irgf.SPONSOR)

    def test_the_only_sponsor_signs_second_at_tier_4(self):
        # Signing as the body would leave nobody to sign as sponsor.
        record = self.record(4)
        verdict = decisions.check(record, self.sponsor)
        self.assertFalse(verdict.ok)
        self.assertIn("only Enterprise Governance Sponsor", verdict.reason)
        self.assertTrue(decisions.check(record, self.sponsor, approving=False).ok)

    def test_a_second_sponsor_frees_the_first_seat(self):
        self.member("sponsor2@example.com", Role.ORG_ADMIN)
        self.assertEqual(decisions.check(self.record(4), self.sponsor).seat, irgf.BODY)

    def test_the_body_waits_when_nobody_else_could_sponsor(self):
        # The only administrator owns the agent, and owners cannot sign from Tier 3.
        self.agent.owner = self.sponsor
        verdict = decisions.check(self.record(4), self.body)
        self.assertFalse(verdict.ok)
        self.assertIn("Nobody else can sign as Enterprise Governance Sponsor", verdict.reason)

    def test_a_service_account_cannot_sign(self):
        account = ServiceAccount.objects.create(
            organization=self.organization, name="bot", role=Role.ORG_ADMIN
        )
        self.assertFalse(decisions.check(self.record(1), account).ok)


class ReadinessTests(Estate):
    def keys(self, checks):
        return {check.key for check in checks if check.blocking}

    def test_an_unclassified_agent_cannot_enter_g3(self):
        self.assertIn("classification", self.keys(readiness(self.agent)))
        with self.assertRaises(LifecycleError):
            self.request()

    def test_a_provisional_tier_is_not_enough(self):
        self.classify(2, confirm=False)
        self.assertIn("classification", self.keys(readiness(self.agent)))

    def test_tier_2_needs_an_oversight_point_and_some_assurance(self):
        self.classify(2)
        blocking = self.keys(readiness(self.agent))
        self.assertTrue({"oversight", "assurance"} <= blocking)
        self.assertFalse(self.keys(readiness(
            self.agent, oversight_point="Lead reviews.", assurance_summary="Sampled."
        )))

    def test_tier_4_needs_evaluation_controls_identity_and_monitoring(self):
        self.classify(4)
        blocking = self.keys(readiness(self.agent, oversight_point="x", monitoring_commitments="y"))
        self.assertEqual(blocking, {"assurance", "controls", "identity"})
        self.ready_for_high_tier()
        self.assertFalse(self.keys(readiness(self.agent, oversight_point="x", monitoring_commitments="y")))


class AuthorizationFlowTests(Estate):
    def test_the_owner_authorizes_a_tier_2_agent(self):
        authorization = self.authorize_tier_2()
        self.assertEqual(authorization.status, AuthorizationStatus.AUTHORIZED)
        self.assertEqual(authorization.configuration["primary_model"]["id"], str(self.model.pk))
        self.assertEqual(AuthorizationService.in_force(self.agent), authorization)

    def test_tier_4_needs_two_signatures(self):
        self.classify(4)
        self.ready_for_high_tier()
        authorization = self.request()
        AuthorizationService.sign(authorization, actor=self.body)
        authorization.refresh_from_db()
        self.assertTrue(authorization.is_pending)
        self.assertEqual(decisions.remaining(authorization), 1)
        AuthorizationService.sign(authorization, actor=self.sponsor)
        authorization.refresh_from_db()
        self.assertTrue(authorization.is_in_force)
        self.assertEqual([s["seat"] for s in authorization.signatures], ["body", "sponsor"])

    def test_the_only_sponsor_may_refuse_a_tier_4_request_first(self):
        self.classify(4)
        self.ready_for_high_tier()
        authorization = self.request()
        with self.assertRaises(LifecycleError):
            AuthorizationService.sign(authorization, actor=self.sponsor)
        AuthorizationService.reject(authorization, actor=self.sponsor, note="Monitoring is too thin.")
        authorization.refresh_from_db()
        self.assertEqual(authorization.status, AuthorizationStatus.REJECTED)

    def test_a_request_goes_stale_if_the_agent_changes(self):
        self.classify(2)
        authorization = self.request()
        self.agent.allowed_tools = ["claims_db", "payments"]
        self.agent.save()
        with self.assertRaises(LifecycleError):
            AuthorizationService.sign(authorization, actor=self.owner)

    def test_only_one_request_waits_at_a_time_and_rejection_needs_a_reason(self):
        self.classify(2)
        authorization = self.request()
        with self.assertRaises(LifecycleError):
            self.request()
        with self.assertRaises(LifecycleError):
            AuthorizationService.reject(authorization, actor=self.owner, note=" ")
        AuthorizationService.reject(authorization, actor=self.owner, note="Oversight is too thin.")
        authorization.refresh_from_db()
        self.assertEqual(authorization.status, AuthorizationStatus.REJECTED)


class ChangeDetectionTests(Estate):
    def setUp(self):
        super().setUp()
        self.authorization = self.authorize_tier_2()

    def open_change(self, field):
        return self.authorization.changes.get(field=field)

    def test_a_narrowing_change_is_recorded_and_accepted(self):
        self.agent.autonomy_level = 1
        self.agent.save()
        change = self.open_change("autonomy_level")
        self.assertEqual((change.classification, change.status), (irgf.MINOR, ChangeStatus.ACCEPTED))
        self.authorization.refresh_from_db()
        self.assertEqual(self.authorization.configuration["autonomy_level"], 1)
        self.assertTrue(self.authorization.is_in_force)

    def test_a_material_change_waits_for_g4_without_suspending(self):
        self.agent.knowledge_sources = ["Claims Handbook"]
        self.agent.save()
        change = self.open_change("knowledge_sources")
        self.assertEqual((change.classification, change.status), (irgf.MATERIAL, ChangeStatus.AWAITING_G4))
        self.authorization.refresh_from_db()
        self.assertTrue(self.authorization.is_in_force)

    def test_a_major_change_suspends_until_it_is_reverted(self):
        self.agent.requires_human_approval = False
        self.agent.save()
        self.authorization.refresh_from_db()
        self.assertEqual(self.authorization.status, AuthorizationStatus.SUSPENDED)

        self.agent.requires_human_approval = True
        self.agent.save()
        self.assertEqual(self.open_change("requires_human_approval").status, ChangeStatus.REVERTED)
        self.authorization.refresh_from_db()
        self.assertTrue(self.authorization.is_in_force)

    def test_approving_needs_a_delta_scope_and_rebaselines(self):
        self.agent.knowledge_sources = ["Claims Handbook"]
        self.agent.save()
        change = self.open_change("knowledge_sources")
        with self.assertRaises(LifecycleError):
            ChangeService.decide(change, actor=self.risk, approve=True)
        ChangeService.decide(
            change, actor=self.risk, approve=True,
            delta_assurance_scope="Re-ran retrieval grounding cases; the rest is unaffected.",
        )
        change.refresh_from_db()
        self.assertEqual(change.status, ChangeStatus.APPROVED)
        self.authorization.refresh_from_db()
        self.assertEqual(self.authorization.configuration["knowledge_sources"], ["Claims Handbook"])

    def test_whoever_made_the_change_cannot_decide_it(self):
        token = self.as_actor(self.risk)
        try:
            self.agent.knowledge_sources = ["Claims Handbook"]
            self.agent.save()
        finally:
            reset_audit_context(token)
        change = self.open_change("knowledge_sources")
        self.assertEqual(change.changed_by, self.risk)
        with self.assertRaises(LifecycleError):
            ChangeService.decide(change, actor=self.risk, approve=True, delta_assurance_scope="x")

    def test_a_major_change_needs_a_fresh_confirmed_tier_before_g4(self):
        self.agent.autonomy_level = 3
        self.agent.save()
        change = self.open_change("autonomy_level")
        self.assertEqual(change.classification, irgf.MAJOR)
        with self.assertRaises(LifecycleError):
            ChangeService.decide(change, actor=self.body, approve=True, delta_assurance_scope="x")

        self.classify(2)  # re-scored after the change, still Tier 2
        ChangeService.decide(change, actor=self.body, approve=True, delta_assurance_scope="Re-ran autonomy cases.")
        self.authorization.refresh_from_db()
        self.assertTrue(self.authorization.is_in_force)

    def test_a_rejected_change_suspends_until_reverted(self):
        self.agent.knowledge_sources = ["Claims Handbook"]
        self.agent.save()
        ChangeService.decide(
            self.open_change("knowledge_sources"), actor=self.risk, approve=False, note="Not assessed."
        )
        self.authorization.refresh_from_db()
        self.assertEqual(self.authorization.status, AuthorizationStatus.SUSPENDED)
        self.agent.knowledge_sources = []
        self.agent.save()
        self.authorization.refresh_from_db()
        self.assertTrue(self.authorization.is_in_force)

    def test_a_new_identity_binding_is_a_material_change(self):
        account = ServiceAccount.objects.create(
            organization=self.organization, name="second-runtime", role=Role.AI_DEVELOPER
        )
        account.agents.add(self.agent)
        self.assertEqual(self.open_change("identities").classification, irgf.MATERIAL)

    def test_a_higher_tier_needs_g3_again(self):
        self.classify(3)
        change = self.open_change("risk_tier")
        self.assertTrue(change.requires_reauthorization)
        self.authorization.refresh_from_db()
        self.assertEqual(self.authorization.status, AuthorizationStatus.SUSPENDED)
        with self.assertRaises(LifecycleError):
            ChangeService.decide(change, actor=self.body, approve=True, delta_assurance_scope="x")

    def test_re_authorization_supersedes_and_resolves_open_changes(self):
        self.agent.knowledge_sources = ["Claims Handbook"]
        self.agent.save()
        fresh = self.request()
        AuthorizationService.sign(fresh, actor=self.owner)
        self.authorization.refresh_from_db()
        self.assertEqual(self.authorization.status, AuthorizationStatus.SUPERSEDED)
        self.assertEqual(self.open_change("knowledge_sources").status, ChangeStatus.SUPERSEDED)


class SecondPersonTests(Estate):
    def test_a_lower_class_claimed_at_tier_3_needs_a_second_person(self):
        self.classify(3)
        self.ready_for_high_tier()
        authorization = self.request()
        AuthorizationService.sign(authorization, actor=self.body)
        self.agent.system_prompt_reference = "git://prompts/claims-v2.md"
        self.agent.save()
        change = authorization.changes.get(field="system_prompt_reference")
        self.assertEqual(change.classification, irgf.MATERIAL)

        ChangeService.reclassify(change, actor=self.owner, classification=irgf.COSMETIC,
                                 reason="Only fixed spelling in the prompt.")
        change.refresh_from_db()
        self.assertEqual(change.status, ChangeStatus.AWAITING_CONFIRMATION)
        with self.assertRaises(LifecycleError):
            ChangeService.confirm(change, actor=self.owner)
        ChangeService.confirm(change, actor=self.developer)
        change.refresh_from_db()
        self.assertEqual(change.status, ChangeStatus.ACCEPTED)

    def test_irgf_fixed_classes_cannot_be_lowered(self):
        self.authorize_tier_2()
        self.agent.autonomy_level = 3
        self.agent.save()
        change = self.agent.change_records.get(field="autonomy_level")
        with self.assertRaises(LifecycleError):
            ChangeService.reclassify(change, actor=self.owner, classification=irgf.MINOR, reason="x")


class ExceptionTests(Estate):
    def setUp(self):
        super().setUp()
        self.classify(2)
        self.policy = Policy.objects.create(
            organization=self.organization, name="Autonomy ceiling", policy_type=PolicyType.AGENT_AUTONOMY,
            effect=PolicyEffect.DENY, configuration={"max_autonomy_level": 1},
        )

    def verdict(self):
        return PolicyEngine.evaluate(
            self.organization, PolicyContext(agent=self.agent, environment=Environment.PRODUCTION)
        )

    def test_limits_are_enforced_at_request(self):
        with self.assertRaises(LifecycleError):
            ExceptionService.request(self.agent, self.policy, actor=self.owner,
                                     justification="x", duration_days=91)
        self.agent.risk_tier = 3
        with self.assertRaises(LifecycleError):
            ExceptionService.request(self.agent, self.policy, actor=self.owner,
                                     justification="x", duration_days=30)

    def test_an_active_exception_sets_the_policy_aside_until_it_expires(self):
        self.assertFalse(self.verdict().allowed)
        exception = ExceptionService.request(
            self.agent, self.policy, actor=self.owner,
            justification="Autonomy 2 is needed for the Q3 backlog.", duration_days=30,
        )
        with self.assertRaises(LifecycleError):
            ExceptionService.sign(exception, actor=self.owner)
        ExceptionService.sign(exception, actor=self.risk)
        exception.refresh_from_db()
        self.assertEqual(exception.status, ExceptionStatus.ACTIVE)

        decision = self.verdict()
        self.assertTrue(decision.allowed)
        self.assertEqual([p.name for p in decision.excepted], ["Autonomy ceiling"])
        self.assertIn("excepted", decision.as_payload())

        ExceptionService.sweep(now=timezone.now() + timedelta(days=31))
        exception.refresh_from_db()
        self.assertEqual(exception.status, ExceptionStatus.EXPIRED)
        self.assertFalse(self.verdict().allowed)

    def test_tier_4_exceptions_cannot_be_renewed(self):
        exception = ExceptionService.request(
            self.agent, self.policy, actor=self.owner, justification="x", duration_days=10
        )
        exception.tier = 4
        exception.save()
        self.assertFalse(exception.can_renew)
        with self.assertRaises(LifecycleError):
            ExceptionService.request(self.agent, self.policy, actor=self.owner, justification="x",
                                     compensating_control="y", duration_days=10, renewal_of=exception)


class DeploymentAuthorizationPolicyTests(Estate):
    def setUp(self):
        super().setUp()
        Policy.objects.create(
            organization=self.organization, name="Authorized agents only",
            policy_type=PolicyType.DEPLOYMENT_AUTHORIZATION, effect=PolicyEffect.DENY,
            configuration={"environments": [Environment.PRODUCTION]},
        )

    def verdict(self):
        return PolicyEngine.evaluate(
            self.organization, PolicyContext(agent=self.agent, environment=Environment.PRODUCTION)
        )

    def test_an_unauthorized_agent_is_refused(self):
        self.assertIn("no deployment authorization", self.verdict().reason)

    def test_a_pending_request_is_not_enough(self):
        self.classify(2)
        self.request()
        self.assertIn("awaits a decision at Gate G3", self.verdict().reason)

    def test_an_authorized_agent_is_served_until_suspended(self):
        self.authorize_tier_2()
        self.assertTrue(self.verdict().allowed)
        self.agent.requires_human_approval = False
        self.agent.save()
        self.assertIn("suspended", self.verdict().reason)


class ConditionTests(Estate):
    def test_an_overdue_condition_escalates_once_a_day_and_can_be_met(self):
        authorization = self.authorize_tier_2()
        condition = AuthorizationCondition.objects.create(
            organization=self.organization, authorization=authorization,
            description="Deliver the PII audit.", owner=self.developer,
            due_date=timezone.now().date() - timedelta(days=1),
        )
        before = Notification.objects.count()
        self.assertEqual(ConditionService.escalate_overdue(), 1)
        self.assertGreater(Notification.objects.count(), before)
        self.assertEqual(ConditionService.escalate_overdue(), 0)

        with self.assertRaises(LifecycleError):
            ConditionService.mark_met(condition, actor=self.risk)
        ConditionService.mark_met(condition, actor=self.developer, note="Delivered.")
        condition.refresh_from_db()
        self.assertTrue(condition.is_met)


class ViewTests(Estate):
    def test_the_gate_pages_render(self):
        authorization = self.authorize_tier_2()
        self.agent.knowledge_sources = ["Claims Handbook"]
        self.agent.save()
        change = authorization.changes.get(field="knowledge_sources")
        policy = Policy.objects.create(organization=self.organization, name="Ceiling",
                                       policy_type=PolicyType.AGENT_AUTONOMY,
                                       configuration={"max_autonomy_level": 1})
        exception = ExceptionService.request(self.agent, policy, actor=self.owner,
                                             justification="x", duration_days=5)

        self.client.force_login(self.body)
        for url, text in [
            (reverse("lifecycle:authorizations"), "Authorization Register"),
            (reverse("lifecycle:authorization_detail", args=[authorization.pk]), "Authorized configuration"),
            (reverse("lifecycle:changes"), "Change Records"),
            (reverse("lifecycle:change_detail", args=[change.pk]), "Gate G4"),
            (reverse("lifecycle:exceptions"), "Exception Register"),
            (reverse("lifecycle:exception_detail", args=[exception.pk]), "IRGF M17"),
            (reverse("lifecycle:exception_request"), "Request the exception"),
            (reverse("inventory:agent_detail", args=[self.agent.pk]), "Deployment Authorization"),
        ]:
            response = self.client.get(url)
            self.assertContains(response, text, msg_prefix=url)

    def test_requesting_and_signing_through_the_pages(self):
        self.classify(2)
        self.client.force_login(self.owner)
        url = reverse("lifecycle:authorization_request", args=[self.agent.pk])
        self.assertContains(self.client.get(url), "Entry criteria")
        response = self.client.post(url, {
            "oversight_point": "A lead reviews every reply.", "assurance_summary": "Sampled 200.",
        })
        authorization = AuthorizationService.pending(self.agent)
        self.assertRedirects(response, reverse("lifecycle:authorization_detail", args=[authorization.pk]))

        self.client.post(reverse("lifecycle:authorization_decide", args=[authorization.pk]),
                         {"decision": "sign", "note": "Approved."})
        authorization.refresh_from_db()
        self.assertTrue(authorization.is_in_force)


class ApiTests(Estate):
    def test_a_pipeline_can_check_the_authorization(self):
        authorization = self.authorize_tier_2()
        account = ServiceAccount.objects.create(
            organization=self.organization, name="ci", role=Role.AI_DEVELOPER
        )
        _, key = APIKey.generate(account)
        response = self.client.get(
            reverse("api:deployment-authorization-list") + f"?agent={self.agent.pk}", HTTP_X_API_KEY=key
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["results"][0]["status"], AuthorizationStatus.AUTHORIZED)

        agent = self.client.get(reverse("api:agent-detail", args=[self.agent.pk]), HTTP_X_API_KEY=key).json()
        self.assertEqual(agent["deployment_authorization"]["id"], str(authorization.pk))
