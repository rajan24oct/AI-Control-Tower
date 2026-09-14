from __future__ import annotations

from rest_framework import serializers

from apps.agents.models import AIAgent
from apps.applications_registry.models import AIApplication
from apps.models_registry.models import AIModel
from apps.organizations.models import Organization, Team
from apps.providers.models import AIProvider


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "name", "slug", "description", "industry", "status", "created_at"]
        read_only_fields = ["id", "created_at"]


class TeamSerializer(serializers.ModelSerializer):
    class Meta:
        model = Team
        fields = ["id", "organization", "name", "slug", "department", "lead", "status"]
        read_only_fields = ["id"]


class AIProviderSerializer(serializers.ModelSerializer):
    provider_type_display = serializers.CharField(source="get_provider_type_display", read_only=True)

    class Meta:
        model = AIProvider
        fields = [
            "id",
            "organization",
            "name",
            "slug",
            "description",
            "provider_type",
            "provider_type_display",
            "adapter_key",
            "region",
            "endpoint",
            "deployment",
            "authentication_method",
            "credential_ref",
            "supported_capabilities",
            "is_approved",
            "health_status",
            "last_health_check",
            "last_health_latency_ms",
            "status",
            "created_at",
            "updated_at",
        ]
        # `credential_ref` is only the NAME of a secret, never the secret. The
        # secret itself has no field and cannot be read back through the API.
        read_only_fields = [
            "id",
            "slug",
            "supported_capabilities",
            "health_status",
            "last_health_check",
            "last_health_latency_ms",
            "created_at",
            "updated_at",
        ]


class AIModelSerializer(serializers.ModelSerializer):
    provider_name = serializers.CharField(source="provider.name", read_only=True)

    class Meta:
        model = AIModel
        fields = [
            "id",
            "organization",
            "provider",
            "provider_name",
            "model_name",
            "model_id",
            "model_family",
            "model_type",
            "version",
            "context_window",
            "max_output_tokens",
            "input_cost_per_1k",
            "output_cost_per_1k",
            "currency",
            "capabilities",
            "region",
            "deployment",
            "approved_for_use",
            "risk_classification",
            "status",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class AIAgentSerializer(serializers.ModelSerializer):
    provider_name = serializers.CharField(source="provider.name", read_only=True)
    primary_model_name = serializers.CharField(source="primary_model.model_name", read_only=True)
    approval_required = serializers.BooleanField(read_only=True)
    is_suspended = serializers.BooleanField(read_only=True)
    deployment_authorization = serializers.SerializerMethodField()

    def get_deployment_authorization(self, agent):
        """IRGF S7 · Gate G3: the authorization the agent is measured against, if any."""
        from apps.lifecycle.services import AuthorizationService

        authorization = AuthorizationService.baseline(agent)
        if authorization is None:
            return None
        return {
            "id": str(authorization.pk),
            "status": authorization.status,
            "environment": authorization.environment,
            "tier": authorization.tier,
        }

    class Meta:
        model = AIAgent
        fields = [
            "id",
            "organization",
            "name",
            "agent_id",
            "description",
            "business_purpose",
            "owner",
            "team",
            "framework",
            "provider",
            "provider_name",
            "primary_model",
            "primary_model_name",
            "fallback_model",
            "environment",
            "risk_level",
            "autonomy_level",
            "requires_human_approval",
            "approval_required",
            "allowed_tools",
            "restricted_tools",
            "knowledge_sources",
            "data_classification",
            "system_prompt_reference",
            "adapter_key",
            "remote_endpoint",
            "status",
            # IRGF: the S2 tier, the Agent Card and the kill-switch state.
            "risk_tier",
            "out_of_scope",
            "action_limits",
            "escalation_thresholds",
            "kill_switch_conditions",
            "kill_switch_triggers",
            "is_suspended",
            "suspended_at",
            "suspension_reason",
            "deployment_authorization",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id", "risk_tier", "suspended_at", "suspension_reason", "created_at", "updated_at",
        ]


class AIApplicationSerializer(serializers.ModelSerializer):
    agent_count = serializers.IntegerField(source="agents.count", read_only=True)
    deployment_authorization = serializers.SerializerMethodField()

    def get_deployment_authorization(self, application):
        """IRGF S7 · Gate G3: the authorization the application is measured against, if any."""
        from apps.lifecycle.services import AuthorizationService

        authorization = AuthorizationService.baseline(application)
        if authorization is None:
            return None
        return {
            "id": str(authorization.pk),
            "status": authorization.status,
            "in_force": authorization.is_in_force,
            "environment": authorization.environment,
            "tier": authorization.tier,
            "decided_at": authorization.decided_at,
        }

    class Meta:
        model = AIApplication
        fields = [
            "id",
            "organization",
            "name",
            "slug",
            "description",
            "business_purpose",
            "business_owner",
            "technical_owner",
            "team",
            "department",
            "environment",
            "criticality",
            "data_classification",
            "risk_level",
            "production_date",
            "review_date",
            "risk_tier",
            "agents",
            "ai_models",
            "providers",
            "agent_count",
            "tools",
            "delegations",
            "deployment_authorization",
            "status",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "slug", "risk_tier", "created_at", "updated_at"]
