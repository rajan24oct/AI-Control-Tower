"""IRGF M13 drift records over the API, read-only.

Incident tooling and dashboards can follow what the Control Plane finds.
Resolving drift stays in the Control Tower, where the resolution rights are
enforced and every decision audited.
"""

from __future__ import annotations

from rest_framework import serializers, viewsets

from apps.accounts.roles import Perm
from apps.api.permissions import HasOrganizationPermission, IsAuthenticatedOrganizationMember
from apps.controlplane.models import DriftRecord


class DriftRecordSerializer(serializers.ModelSerializer):
    agent_id = serializers.SerializerMethodField()

    class Meta:
        model = DriftRecord
        fields = [
            "id", "agent", "agent_id", "application", "signal", "category", "is_incident", "tier",
            "title", "status", "resolution", "occurrences", "first_seen_at", "last_seen_at",
            "route", "response_due", "escalated_at", "auto_suspended",
        ]
        read_only_fields = fields

    def get_agent_id(self, record):
        return record.agent.agent_id if record.agent_id else None


class DriftRecordViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = DriftRecordSerializer
    permission_classes = [IsAuthenticatedOrganizationMember, HasOrganizationPermission]
    required_permission = Perm.INVENTORY_VIEW
    queryset = DriftRecord.objects.select_related("agent")
    filterset_fields = ["agent", "application", "category", "status", "is_incident", "signal", "tier"]
    ordering_fields = ["last_seen_at", "first_seen_at", "response_due"]

    def get_queryset(self):
        return self.queryset.for_user(self.request.user)
