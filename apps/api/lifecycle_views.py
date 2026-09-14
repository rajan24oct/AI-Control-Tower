"""IRGF Gate G3 over the API, read-only.

A deployment pipeline can ask whether an agent holds a deployment authorization
in force before it ships -- IRGF's authorization sequencing -- and compare what
it is about to deploy with the authorized configuration. Deciding stays in the
Control Tower, where decision rights are enforced and every signature audited.
"""

from __future__ import annotations

from rest_framework import serializers, viewsets

from apps.accounts.roles import Perm
from apps.api.permissions import HasOrganizationPermission, IsAuthenticatedOrganizationMember
from apps.lifecycle.models import DeploymentAuthorization


class DeploymentAuthorizationSerializer(serializers.ModelSerializer):
    agent_id = serializers.SerializerMethodField()
    in_force = serializers.BooleanField(source="is_in_force", read_only=True)

    class Meta:
        model = DeploymentAuthorization
        fields = [
            "id", "agent", "agent_id", "application", "environment", "status", "in_force", "tier",
            "requested_at", "decided_at", "signatures", "suspension_reason", "configuration",
        ]
        read_only_fields = fields

    def get_agent_id(self, authorization):
        return authorization.agent.agent_id if authorization.agent_id else None


class DeploymentAuthorizationViewSet(viewsets.ReadOnlyModelViewSet):
    """An agent's or an application's authorizations: filter by `agent` or `application`."""

    serializer_class = DeploymentAuthorizationSerializer
    permission_classes = [IsAuthenticatedOrganizationMember, HasOrganizationPermission]
    required_permission = Perm.INVENTORY_VIEW
    queryset = DeploymentAuthorization.objects.select_related("agent", "application")
    filterset_fields = ["agent", "application", "status", "environment", "tier"]
    ordering_fields = ["requested_at", "decided_at"]

    def get_queryset(self):
        return self.queryset.for_user(self.request.user)
