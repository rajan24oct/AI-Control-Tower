import os
from decimal import Decimal

from django.test import TestCase

from apps.agents.models import AIAgent, AutonomyLevel
from apps.core.constants import HealthStatus, RiskLevel
from apps.models_registry.models import AIModel, ModelType
from apps.organizations.models import Organization
from apps.providers.models import AIProvider, ProviderType
from apps.providers.services import ProviderCapabilityService, ProviderHealthService
from integrations.base.exceptions import AdapterNotFound
from integrations.providers.mock import MockProviderAdapter


class ProviderAdapterBindingTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Provider Org")

    def test_a_provider_resolves_its_adapter_through_the_registry(self):
        provider = AIProvider.objects.create(
            organization=self.organization, name="Mock Provider", adapter_key="mock"
        )
        self.assertIsInstance(provider.get_adapter(), MockProviderAdapter)

    def test_an_unknown_adapter_key_fails_loudly(self):
        provider = AIProvider.objects.create(
            organization=self.organization, name="Broken", adapter_key="not-registered"
        )
        with self.assertRaises(AdapterNotFound):
            provider.get_adapter()

    def test_adapter_config_carries_connection_details_but_no_secret(self):
        provider = AIProvider.objects.create(
            organization=self.organization,
            name="Configured",
            adapter_key="mock",
            endpoint="https://api.example.test",
            region="eu-west-1",
            credential_ref="SOME_ENV_VAR",
            options={"timeout_seconds": 5},
        )
        config = provider.adapter_config()

        self.assertEqual(config.endpoint, "https://api.example.test")
        self.assertEqual(config.credential_ref, "SOME_ENV_VAR")
        self.assertEqual(config.option("timeout_seconds"), 5)
        # The config holds only the reference; the secret is resolved on demand.
        self.assertNotIn("secret", str(config).lower())

    def test_credential_resolution_reflects_the_environment(self):
        provider = AIProvider.objects.create(
            organization=self.organization,
            name="Credentialed",
            adapter_key="mock",
            credential_ref="CT_PROVIDER_TEST_KEY",
        )
        self.assertFalse(provider.has_resolvable_credential)

        os.environ["CT_PROVIDER_TEST_KEY"] = "abc123"
        try:
            self.assertTrue(provider.has_resolvable_credential)
        finally:
            del os.environ["CT_PROVIDER_TEST_KEY"]

    def test_the_raw_credential_is_never_a_model_field(self):
        field_names = {f.name for f in AIProvider._meta.get_fields()}
        for forbidden in ("api_key", "secret", "password", "token", "credential"):
            self.assertNotIn(forbidden, field_names)


class ProviderHealthServiceTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Health Org")
        self.provider = AIProvider.objects.create(
            organization=self.organization, name="Mock", adapter_key="mock"
        )

    def test_a_healthy_probe_updates_the_record(self):
        ProviderHealthService.check(self.provider)
        self.provider.refresh_from_db()

        self.assertEqual(self.provider.health_status, HealthStatus.HEALTHY)
        self.assertIsNotNone(self.provider.last_health_check)

    def test_an_unresolvable_adapter_is_recorded_as_unhealthy(self):
        broken = AIProvider.objects.create(
            organization=self.organization, name="Broken", adapter_key="missing"
        )
        ProviderHealthService.check(broken)
        broken.refresh_from_db()
        self.assertEqual(broken.health_status, HealthStatus.UNHEALTHY)

    def test_a_status_change_writes_an_audit_entry(self):
        from apps.audit.models import AuditLog

        ProviderHealthService.check(self.provider)
        self.assertTrue(
            AuditLog.objects.filter(
                resource_id=str(self.provider.pk), action="health_check"
            ).exists()
        )

    def test_capabilities_are_refreshed_from_the_adapter(self):
        capabilities = ProviderCapabilityService.refresh(self.provider)
        self.assertIn("supports_chat", capabilities)
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.supported_capabilities, capabilities)


class ModelRegistryTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Model Org")
        self.provider = AIProvider.objects.create(
            organization=self.organization, name="P", adapter_key="mock"
        )

    def test_cost_estimation_uses_registered_rates(self):
        model = AIModel.objects.create(
            organization=self.organization,
            provider=self.provider,
            model_name="Test",
            model_id="test-1",
            input_cost_per_1k=Decimal("0.01"),
            output_cost_per_1k=Decimal("0.03"),
        )
        self.assertEqual(model.estimate_cost(2000, 1000), Decimal("0.050000"))

    def test_a_model_is_usable_only_when_approved_and_active(self):
        model = AIModel.objects.create(
            organization=self.organization,
            provider=self.provider,
            model_name="Pending",
            model_id="pending-1",
            approved_for_use=False,
        )
        self.assertFalse(model.is_usable)

        model.approved_for_use = True
        model.save()
        self.assertTrue(model.is_usable)

    def test_capability_lookup(self):
        model = AIModel.objects.create(
            organization=self.organization,
            provider=self.provider,
            model_name="Cap",
            model_id="cap-1",
            model_type=ModelType.CHAT,
            capabilities=["supports_chat", "supports_vision"],
        )
        self.assertTrue(model.supports("supports_vision"))
        self.assertFalse(model.supports("supports_audio"))


class AgentGovernanceTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Agent Org")

    def _agent(self, **kwargs):
        defaults = {
            "organization": self.organization,
            "name": "Test Agent",
            "agent_id": "test-agent",
        }
        return AIAgent.objects.create(**{**defaults, **kwargs})

    def test_high_autonomy_forces_approval_even_when_unset(self):
        agent = self._agent(
            autonomy_level=AutonomyLevel.AUTONOMOUS, requires_human_approval=False
        )
        # The autonomy policy overrides the per-agent flag.
        self.assertTrue(agent.approval_required)

    def test_low_autonomy_without_the_flag_needs_no_approval(self):
        agent = self._agent(autonomy_level=AutonomyLevel.ADVISORY, requires_human_approval=False)
        self.assertFalse(agent.approval_required)

    def test_excessive_autonomy_is_flagged_for_high_risk_agents(self):
        risky = self._agent(
            agent_id="risky",
            autonomy_level=AutonomyLevel.AUTONOMOUS,
            requires_human_approval=False,
            risk_level=RiskLevel.CRITICAL,
        )
        self.assertTrue(risky.has_excessive_autonomy)

    def test_excessive_autonomy_does_not_trigger_on_low_risk(self):
        benign = self._agent(
            agent_id="benign",
            autonomy_level=AutonomyLevel.AUTONOMOUS,
            requires_human_approval=False,
            risk_level=RiskLevel.LOW,
        )
        self.assertFalse(benign.has_excessive_autonomy)

    def test_agent_id_is_generated_from_the_name(self):
        agent = AIAgent.objects.create(organization=self.organization, name="Claims Triage Agent")
        self.assertEqual(agent.agent_id, "claims-triage-agent")
