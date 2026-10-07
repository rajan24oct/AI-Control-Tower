"""IRGF Phase 4: applications at Gate G3, composite authority, and delegation."""

from __future__ import annotations

from django.urls import reverse
from django.utils import timezone

from apps.a2a.models import A2AAccessMode, A2AAgentCard
from apps.accounts.roles import Role
from apps.agents.models import AIAgent
from apps.api.models import APIKey, ServiceAccount
from apps.applications_registry.models import AIApplication
from apps.audit.context import AuditContext, reset_audit_context, set_audit_context
from apps.classification.services import ClassificationService
from apps.controlplane.models import DriftRecord, DriftStatus, VerificationOutcome
from apps.controlplane.services import ControlPlane, DriftService
from apps.core.constants import Environment, LifecycleStatus
from apps.executions.models import Execution
from apps.gateway.messaging import MessageRequest, MessagingService
from apps.lifecycle import irgf
from apps.lifecycle.models import AuthorizationStatus, ChangeStatus
from apps.lifecycle.services import AuthorizationService, LifecycleError
from apps.lifecycle.tests import FIELDS, Estate
from apps.policies.models import Policy, PolicyEffect, PolicyType
from apps.policies.services import PolicyContext, _check_deployment_authorization


class SystemEstate(Estate):
    def setUp(self):
        super().setUp()
        self.application = AIApplication.objects.create(
            organization=self.organization, name="Claims Platform", business_owner=self.owner,
            environment=Environment.PRODUCTION, status=LifecycleStatus.ACTIVE,
        )
        self.application.agents.add(self.agent)
        self.application.ai_models.add(self.model)
        self.application.providers.add(self.model.provider)
        # An advisory member: it informs, and needs no authorization of its own.
        self.intake = AIAgent.objects.create(
            organization=self.organization, name="Intake Agent", agent_id="intake-agent", owner=self.owner,
            provider=self.model.provider, primary_model=self.model, environment=Environment.PRODUCTION,
            status=LifecycleStatus.ACTIVE, autonomy_level=0,
        )
        self.application.agents.add(self.intake)

    def classify_application(self, scores=(2, 2, 2, 2, 2, 2)):
        record = ClassificationService.score(
            self.application, scores=dict(zip(FIELDS, scores)), rationale="Evidence.", actor=self.developer
        )
        ClassificationService.countersign(record, actor=self.risk)
        self.application.refresh_from_db()
        return record

    def checks(self, **statements):
        return {check.key: check for check in AuthorizationService.readiness(self.application, **statements)}

    def authorize_application(self):
        self.authorize_tier_2()
        self.classify_application()
        authorization = AuthorizationService.request(
            self.application, actor=self.owner, oversight_point="A claims lead reviews urgent routings.",
            assurance_summary="Sampled 100 claims.",
        )
        AuthorizationService.sign(authorization, actor=self.owner)
        authorization.refresh_from_db()
        self.application.refresh_from_db()
        return authorization

    def edit(self, instance, **fields):
        token = set_audit_context(AuditContext(actor=self.developer, actor_label=self.developer.email))
        try:
            for name, value in fields.items():
                setattr(instance, name, value)
            instance.save()
        finally:
            reset_audit_context(token)


class ApplicationReadinessTests(SystemEstate):
    def test_each_acting_agent_is_authorized_first(self):
        self.classify_application()
        self.assertTrue(self.checks()["members"].blocking)
        self.authorize_tier_2()
        self.assertFalse(self.checks()["members"].blocking)

    def test_d2_is_scored_against_the_composite_authority(self):
        self.agent.autonomy_level = 4
        self.agent.save()
        self.classify_application()
        check = self.checks()["composite"]
        self.assertTrue(check.blocking)
        self.assertIn("at least 4", check.detail)

    def test_several_acting_agents_need_a_delegation_graph_at_tier_3(self):
        self.intake.allowed_tools = ["ocr"]
        self.intake.save()
        self.classify_application((3, 3, 3, 3, 2, 2))
        self.assertTrue(self.checks()["delegations"].blocking)
        self.application.delegations = [{"from": "claims-agent", "to": "intake-agent", "authority": "own"}]
        self.application.save()
        self.assertFalse(self.checks()["delegations"].blocking)

    def test_a_delegation_names_member_agents_only(self):
        self.application.delegations = [{"from": "claims-agent", "to": "someone-else", "authority": "own"}]
        self.application.save()
        self.classify_application()
        self.assertTrue(self.checks()["delegations"].blocking)

    def test_unapproved_models_block_the_application(self):
        self.model.approved_for_use = False
        self.model.save()
        self.classify_application()
        self.assertTrue(self.checks()["components"].blocking)


class ApplicationAuthorizationTests(SystemEstate):
    def test_the_owner_authorizes_a_tier_2_application(self):
        authorization = self.authorize_application()
        self.assertEqual(authorization.status, AuthorizationStatus.AUTHORIZED)
        self.assertEqual(authorization.subject, self.application)
        self.assertEqual(authorization.subject_kind, "application")
        self.assertEqual({ref["agent_id"] for ref in authorization.configuration["agents"]},
                         {"claims-agent", "intake-agent"})
        self.assertEqual(authorization.configuration["composite"]["autonomy"], 2)
        self.assertEqual(AuthorizationService.in_force(self.application), authorization)

    def test_an_unready_application_cannot_be_requested(self):
        self.classify_application()
        with self.assertRaises(LifecycleError):
            AuthorizationService.request(self.application, actor=self.owner,
                                         oversight_point="x", assurance_summary="y")

    def test_adding_an_agent_is_a_change_for_gate_g4(self):
        self.authorize_application()
        extra = AIAgent.objects.create(
            organization=self.organization, name="Payout Agent", agent_id="payout-agent",
            environment=Environment.PRODUCTION, status=LifecycleStatus.ACTIVE, autonomy_level=2,
        )
        self.application.agents.add(extra)
        change = self.application.change_records.get(field="agents")
        self.assertEqual(change.change_type, "agent_added")
        self.assertEqual(change.classification, irgf.MATERIAL)
        self.assertEqual(change.status, ChangeStatus.AWAITING_G4)

    def test_widening_a_member_agent_widens_the_composite(self):
        self.authorize_application()
        self.edit(self.agent, allowed_tools=[*self.agent.allowed_tools, "payments"])
        change = self.application.change_records.get(field="composite")
        self.assertEqual(change.change_type, "tool_added")
        # The agent's own authorization sees its own change as well.
        self.assertTrue(self.agent.change_records.filter(field="allowed_tools").exists())

    def test_declaring_a_delegation_is_a_change(self):
        self.authorize_application()
        self.edit(self.application, delegations=[{"from": "claims-agent", "to": "intake-agent", "authority": "own"}])
        change = self.application.change_records.get(field="delegations")
        self.assertEqual(change.change_type, "delegation_added")

    def test_the_deployment_authorization_policy_holds_applications_too(self):
        policy = Policy.objects.create(
            organization=self.organization, name="Authorized systems only",
            policy_type=PolicyType.DEPLOYMENT_AUTHORIZATION, effect=PolicyEffect.DENY, configuration={},
        )
        context = PolicyContext(application=self.application, environment=Environment.PRODUCTION)
        self.assertIn("Application 'Claims Platform'", _check_deployment_authorization(policy, context))
        self.authorize_application()
        self.assertIsNone(_check_deployment_authorization(policy, context))


class SystemControlPlaneTests(SystemEstate):
    def run_for(self, agent, **fields):
        now = timezone.now()
        return Execution.objects.create(
            organization=self.organization, agent=agent, application=self.application, model=self.model,
            external_id=f"run-{Execution.objects.count()}", environment=Environment.PRODUCTION,
            status="completed", started_at=now, ended_at=now, **fields,
        )

    def test_an_unauthorized_application_in_production_is_structural_drift(self):
        ControlPlane.verify_execution(self.run_for(self.agent))
        record = DriftRecord.objects.get(application=self.application, signal="unauthorized_deployment")
        self.authorize_application()
        self.assertEqual(DriftService.settle_structural(self.organization), 2)  # the agent's and the application's
        record.refresh_from_db()
        self.assertEqual(record.status, DriftStatus.RESOLVED)

    def test_a_run_with_an_agent_outside_the_application(self):
        self.authorize_application()
        outsider = AIAgent.objects.create(
            organization=self.organization, name="Outside Agent", agent_id="outside-agent",
            environment=Environment.PRODUCTION, status=LifecycleStatus.ACTIVE,
        )
        ControlPlane.verify_execution(self.run_for(outsider))
        record = DriftRecord.objects.get(application=self.application, signal="agent_outside_system")
        self.assertEqual(record.observed, ["Outside Agent"])

    def publish(self, agent):
        """Put an agent on the A2A Gateway, open to every agent, so a hand-off needs no contract."""
        return A2AAgentCard.objects.create(
            organization=self.organization, agent=agent, is_approved=True, access_mode=A2AAccessMode.OPEN,
            data_classification="restricted",
        )

    def test_an_undeclared_delegation_is_integration_drift(self):
        self.authorize_application()
        self.publish(self.intake)
        task = MessagingService.send(
            self.organization, MessageRequest(sender=self.agent, recipient=self.intake, body="Please extract this.")
        )
        verification = ControlPlane.verify_a2a_task(task)
        self.assertEqual(verification.outcome, VerificationOutcome.DIVERGES)
        record = DriftRecord.objects.get(application=self.application, signal="undeclared_delegation")
        self.assertEqual(record.category, "integration")

    def test_a_declared_delegation_conforms(self):
        self.application.delegations = [{"from": "claims-agent", "to": "intake-agent", "authority": "own"}]
        self.application.save()
        self.publish(self.intake)
        self.authorize_application()
        task = MessagingService.send(
            self.organization, MessageRequest(sender=self.agent, recipient=self.intake, body="Please extract this.")
        )
        self.assertEqual(ControlPlane.verify_a2a_task(task).outcome, VerificationOutcome.CONFORMS)


class SystemPageTests(SystemEstate):
    def test_the_application_pages_render(self):
        authorization = self.authorize_application()
        self.client.force_login(self.owner)
        for url, text in [
            (reverse("inventory:application_detail", args=[self.application.pk]), "Composite reachable authority"),
            (reverse("lifecycle:authorization_detail", args=[authorization.pk]), "Member agents"),
            (reverse("lifecycle:application_authorization_request", args=[self.application.pk]), "Every acting agent authorized"),
            (reverse("lifecycle:authorizations"), "Claims Platform"),
        ]:
            self.assertContains(self.client.get(url), text, msg_prefix=url)

    def test_the_api_filters_authorizations_by_application(self):
        authorization = self.authorize_application()
        account = ServiceAccount.objects.create(organization=self.organization, name="ci", role=Role.AI_DEVELOPER)
        _, key = APIKey.generate(account)
        response = self.client.get(
            reverse("api:deployment-authorization-list") + f"?application={self.application.pk}", HTTP_X_API_KEY=key
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["results"][0]["id"], str(authorization.pk))
