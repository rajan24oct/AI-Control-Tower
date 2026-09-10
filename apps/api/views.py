"""Configuration APIs (section 5).

Telemetry ingestion endpoints arrive with the execution-monitoring build; this
module establishes the org-scoped, permission-checked viewset pattern they will
reuse.
"""

from __future__ import annotations

from rest_framework import viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.accounts.roles import Perm
from apps.agents.models import AIAgent
from apps.api.permissions import HasOrganizationPermission, IsAuthenticatedOrganizationMember
from apps.api.serializers import (
    AIAgentSerializer,
    AIApplicationSerializer,
    AIModelSerializer,
    AIProviderSerializer,
    OrganizationSerializer,
)
from apps.applications_registry.models import AIApplication
from apps.models_registry.models import AIModel
from apps.organizations.models import Organization
from apps.providers.models import AIProvider
from integrations.registry import registry


class OrganizationScopedViewSet(viewsets.ModelViewSet):
    """Base viewset that confines every query to the caller's organizations.

    Isolation is applied in `get_queryset`, so no view can accidentally expose
    another tenant's records by forgetting a filter.
    """

    permission_classes = [IsAuthenticatedOrganizationMember, HasOrganizationPermission]
    required_permission = Perm.INVENTORY_VIEW
    required_write_permission = Perm.INVENTORY_MANAGE

    def get_queryset(self):
        return self.queryset.for_user(self.request.user)

    def perform_create(self, serializer):
        organization = serializer.validated_data.get("organization")
        if organization is None:
            organization = getattr(self.request, "organization", None) or (
                self.request.user.default_organization
            )
        serializer.save(organization=organization)


class ProviderViewSet(OrganizationScopedViewSet):
    queryset = AIProvider.objects.select_related("organization", "owner")
    serializer_class = AIProviderSerializer
    required_write_permission = Perm.PROVIDER_MANAGE
    filterset_fields = ["provider_type", "status", "health_status", "is_approved", "region"]
    search_fields = ["name", "slug", "description"]
    ordering_fields = ["name", "created_at", "health_status"]


class ModelViewSet(OrganizationScopedViewSet):
    queryset = AIModel.objects.select_related("organization", "provider")
    serializer_class = AIModelSerializer
    required_write_permission = Perm.MODEL_MANAGE
    filterset_fields = ["provider", "model_type", "approved_for_use", "status", "risk_classification"]
    search_fields = ["model_name", "model_id", "model_family"]
    ordering_fields = ["model_name", "created_at"]


class AgentViewSet(OrganizationScopedViewSet):
    queryset = AIAgent.objects.select_related("organization", "provider", "primary_model", "team")
    serializer_class = AIAgentSerializer
    required_write_permission = Perm.AGENT_MANAGE
    filterset_fields = ["framework", "environment", "risk_level", "autonomy_level", "status"]
    search_fields = ["name", "agent_id", "description"]
    ordering_fields = ["name", "created_at", "autonomy_level"]


class ApplicationViewSet(OrganizationScopedViewSet):
    queryset = AIApplication.objects.select_related("organization", "team").prefetch_related(
        "agents", "ai_models", "providers"
    )
    serializer_class = AIApplicationSerializer
    required_write_permission = Perm.APPLICATION_MANAGE
    filterset_fields = ["environment", "criticality", "risk_level", "status", "team"]
    search_fields = ["name", "description", "department"]
    ordering_fields = ["name", "created_at"]


class OrganizationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = OrganizationSerializer
    permission_classes = [IsAuthenticatedOrganizationMember]

    def get_queryset(self):
        user = self.request.user
        if getattr(user, "is_superuser", False):
            return Organization.objects.all()
        return user.organizations


@api_view(["GET"])
def adapter_catalog(request):
    """What the Control Tower can currently connect to.

    Lets operators confirm an integration is available before pointing a
    provider or agent record at it.
    """
    return Response(registry.describe_all())


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    return Response({"status": "ok", "service": "ai-control-tower"})
