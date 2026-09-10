"""Telemetry and execution APIs (section 5).

These are the endpoints an external engine talks to. Everything an engine needs
to be governed and observed is here: register a run, stream normalized events,
close it out. No engine ever needs to know how the Control Tower stores any of
it.
"""

from __future__ import annotations

import uuid

from django.shortcuts import get_object_or_404
from rest_framework import status as http_status
from rest_framework import viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response

from apps.accounts.roles import Perm
from apps.api.execution_serializers import (
    EventIngestSerializer,
    ExecutionEventSerializer,
    ExecutionFinishSerializer,
    ExecutionIngestSerializer,
    ExecutionSerializer,
    WebhookIngestSerializer,
)
from apps.api.permissions import HasOrganizationPermission, IsAuthenticatedOrganizationMember
from apps.executions.models import Execution
from apps.executions.services import ExecutionService
from apps.telemetry.services import TelemetryIngestionError, TelemetryService


def caller_organization(request):
    """The organization a write is attributed to.

    A service account has exactly one; a human uses their active organization.
    """
    return getattr(request, "organization", None) or request.user.default_organization


def caller_service_account(request):
    return request.user if getattr(request.user, "is_service_account", False) else None


class ExecutionViewSet(viewsets.ReadOnlyModelViewSet):
    """Read executions, and accept telemetry that creates them."""

    serializer_class = ExecutionSerializer
    permission_classes = [IsAuthenticatedOrganizationMember, HasOrganizationPermission]
    required_permission = Perm.EXECUTION_VIEW
    required_write_permission = Perm.TELEMETRY_INGEST

    queryset = Execution.objects.select_related(
        "agent", "application", "provider", "model", "organization"
    )
    filterset_fields = ["status", "environment", "agent", "application", "provider", "model", "framework"]
    search_fields = ["external_id", "trace__trace_id", "error_message"]
    ordering_fields = ["started_at", "cost", "total_tokens", "latency_ms"]

    def get_queryset(self):
        return self.queryset.for_user(self.request.user)

    def get_object(self):
        """Resolve by Control Tower UUID or by the engine's own execution id.

        Engines hold their own identifier; forcing them to store a second one
        would be a needless coupling.
        """
        lookup = self.kwargs[self.lookup_field]
        queryset = self.get_queryset()

        try:
            uuid.UUID(str(lookup))
        except (ValueError, TypeError):
            obj = get_object_or_404(queryset, external_id=lookup)
        else:
            obj = get_object_or_404(queryset, pk=lookup)

        self.check_object_permissions(self.request, obj)
        return obj

    def create(self, request, *args, **kwargs):
        """POST /api/v1/executions/ -- register or update a run."""
        serializer = ExecutionIngestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        events = data.pop("events", [])

        organization = caller_organization(request)
        if organization is None:
            return Response(
                {"detail": "No organization resolved for this caller."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        execution = ExecutionService.record(
            organization=organization,
            external_id=data.pop("execution_id", ""),
            service_account=caller_service_account(request),
            source_adapter="control_tower_api",
            **data,
        )
        if events:
            ExecutionService.append_events(execution, events)
            execution.refresh_from_db()

        return Response(
            ExecutionSerializer(execution).data, status=http_status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["get", "post"], url_path="events")
    def events(self, request, pk=None):
        """GET the timeline, or POST a batch of normalized events onto it."""
        execution = self.get_object()

        if request.method == "GET":
            queryset = execution.events.all()
            page = self.paginate_queryset(queryset)
            if page is not None:
                return self.get_paginated_response(ExecutionEventSerializer(page, many=True).data)
            return Response(ExecutionEventSerializer(queryset, many=True).data)

        serializer = EventIngestSerializer(data=request.data, many=isinstance(request.data, list))
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data
        events = payload if isinstance(payload, list) else [payload]

        created = ExecutionService.append_events(execution, events)
        execution.refresh_from_db()

        return Response(
            {
                "accepted": len(created),
                "execution": ExecutionSerializer(execution).data,
            },
            status=http_status.HTTP_201_CREATED,
        )

    def _finish(self, request, transition: str):
        execution = self.get_object()
        serializer = ExecutionFinishSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        fields = dict(serializer.validated_data)
        reason = fields.pop("reason", "")

        if transition == "cancel":
            execution = ExecutionService.cancel(execution, reason=reason, **fields)
        elif transition == "fail":
            message = fields.pop("error_message", "") or reason
            execution = ExecutionService.fail(execution, error_message=message, **fields)
        else:
            fields.pop("error_message", None)
            execution = ExecutionService.complete(execution, **fields)

        return Response(ExecutionSerializer(execution).data)

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        return self._finish(request, "complete")

    @action(detail=True, methods=["post"])
    def fail(self, request, pk=None):
        return self._finish(request, "fail")

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return self._finish(request, "cancel")


@api_view(["POST"])
@permission_classes([IsAuthenticatedOrganizationMember])
def telemetry_webhook(request):
    """Accept a whole engine payload and normalize it through an adapter.

    This is the zero-integration path: an engine posts whatever it already
    emits, and a configured adapter maps it onto the Control Tower model.
    """
    organization = caller_organization(request)
    if organization is None:
        return Response(
            {"detail": "No organization resolved for this caller."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    if not request.user.has_org_permission(organization, Perm.TELEMETRY_INGEST):
        return Response(
            {"detail": "Your role does not grant telemetry ingestion."},
            status=http_status.HTTP_403_FORBIDDEN,
        )

    serializer = WebhookIngestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        execution = TelemetryService.ingest(
            organization=organization,
            payload=serializer.validated_data["payload"],
            adapter_key=serializer.validated_data.get("adapter") or "generic_webhook",
            service_account=caller_service_account(request),
            endpoint=request.path,
        )
    except TelemetryIngestionError as exc:
        return Response({"detail": str(exc)}, status=http_status.HTTP_422_UNPROCESSABLE_ENTITY)

    return Response(
        ExecutionSerializer(execution).data, status=http_status.HTTP_201_CREATED
    )
