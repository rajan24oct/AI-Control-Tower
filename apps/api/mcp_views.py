"""MCP Gateway endpoints.

Two ways in, one set of rules. `/api/v1/mcp/` speaks MCP itself, so any MCP
client connects with nothing but a URL and a key. The REST pair under
`/api/v1/mcp-gateway/` serves the same catalog and the same enforced calls to
code that would rather not speak JSON-RPC.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from rest_framework import serializers, status as http_status, viewsets
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from apps.accounts.roles import Perm
from apps.api.authentication import ServiceAccountAuthentication
from apps.api.execution_views import caller_organization, caller_service_account
from apps.api.identity import IdentityRefused, acting_agent
from apps.api.permissions import HasOrganizationPermission, IsAuthenticatedOrganizationMember
from apps.core.constants import Environment
from apps.executions.services import AssetResolver
from apps.mcp_gateway.models import CallChannel, MCPServer, ToolStatus
from apps.mcp_gateway.protocol import (
    INVALID_REQUEST,
    PARSE_ERROR,
    UNKNOWN_AGENT,
    Caller,
    MCPEndpoint,
    error_response,
)
from apps.mcp_gateway.services import MCPGateway, ToolCallRequest, UnknownTool
from integrations.base.mcp import SUPPORTED_PROTOCOL_VERSIONS


class MCPKeyAuthentication(ServiceAccountAuthentication):
    """Also accepts `Authorization: Bearer act_...`.

    MCP clients are configured with a bearer token far more often than with a
    custom header, so the MCP endpoint speaks their dialect. Only platform keys
    are recognised; any other bearer token is left to fail as unauthenticated.
    """

    @staticmethod
    def _extract_key(request) -> str:
        key = ServiceAccountAuthentication._extract_key(request)
        if key:
            return key
        scheme, _, token = request.META.get("HTTP_AUTHORIZATION", "").strip().partition(" ")
        token = token.strip()
        if scheme.lower() == "bearer" and token.startswith("act_"):
            return token
        return ""

    def authenticate_header(self, request):
        return 'Bearer realm="ai-control-tower-mcp"'


def _jsonrpc_error(code: int, message: str, *, status: int) -> JsonResponse:
    return JsonResponse(error_response(None, code, message), status=status)


def _origin_allowed(request) -> bool:
    """Refuse browser pages from origins nobody configured.

    The MCP spec requires this of HTTP servers: it is the defence against a
    web page using DNS rebinding to reach an endpoint on a private network.
    Clients that are not browsers send no Origin and are unaffected.
    """
    origin = request.META.get("HTTP_ORIGIN")
    if not origin:
        return True
    allowed = set(settings.CSRF_TRUSTED_ORIGINS) | set(
        settings.CONTROL_TOWER.get("MCP_GATEWAY", {}).get("ALLOWED_ORIGINS", [])
    )
    return origin in allowed


@api_view(["GET", "POST", "DELETE"])
@authentication_classes([MCPKeyAuthentication])
@permission_classes([IsAuthenticatedOrganizationMember])
def mcp_endpoint(request, agent_id=None):
    """POST /api/v1/mcp/[{agent}/] -- the MCP Gateway, as an MCP server."""
    if request.method != "POST":
        # No server-initiated stream to open (GET) and no session to end
        # (DELETE): the endpoint is stateless, which the spec permits.
        response = _jsonrpc_error(
            INVALID_REQUEST, "Only POST is supported; this endpoint is stateless.", status=405
        )
        response["Allow"] = "POST"
        return response

    if not _origin_allowed(request):
        return _jsonrpc_error(INVALID_REQUEST, "Origin not allowed.", status=403)

    version = request.META.get("HTTP_MCP_PROTOCOL_VERSION")
    if version and version not in SUPPORTED_PROTOCOL_VERSIONS:
        return _jsonrpc_error(
            INVALID_REQUEST, f"Unsupported MCP-Protocol-Version '{version}'.", status=400
        )

    organization = caller_organization(request)
    if organization is None:
        return _jsonrpc_error(INVALID_REQUEST, "No organization resolved for this caller.", status=400)
    if not request.user.has_org_permission(organization, Perm.MCP_INVOKE):
        return _jsonrpc_error(
            INVALID_REQUEST, "Your role does not grant MCP Gateway access.", status=403
        )

    # The agent can ride in the URL, which is the easiest thing to put in an
    # MCP client's configuration, or in a header for clients that share a URL.
    requested = agent_id or request.META.get("HTTP_X_AGENT_ID", "")
    agent = None
    if requested:
        agent = AssetResolver(organization).agent(requested)
        if agent is None:
            return _jsonrpc_error(UNKNOWN_AGENT, f"Unknown agent '{requested}'.", status=404)
    try:
        agent = acting_agent(caller_service_account(request), agent, requested=bool(requested))
    except IdentityRefused as exc:
        return _jsonrpc_error(INVALID_REQUEST, exc.message, status=403)

    try:
        payload = json.loads(request.body or b"null")
    except ValueError:
        return _jsonrpc_error(PARSE_ERROR, "Parse error: the body is not JSON.", status=400)

    environment = request.META.get("HTTP_X_ENVIRONMENT", "")
    reply = MCPEndpoint(
        Caller(
            organization=organization,
            agent=agent,
            service_account=caller_service_account(request),
            environment=environment if environment in Environment.values else "",
            execution_id=request.META.get("HTTP_X_EXECUTION_ID", "")[:200],
        )
    ).handle(payload)

    if reply is None:
        return HttpResponse(status=http_status.HTTP_202_ACCEPTED)
    return JsonResponse(reply, safe=False)


# -- REST ---------------------------------------------------------------------


def _invoking_organization(request):
    """The caller's organization, or the response refusing them."""
    organization = caller_organization(request)
    if organization is None:
        return None, Response(
            {"detail": "No organization resolved for this caller."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    if not request.user.has_org_permission(organization, Perm.MCP_INVOKE):
        return None, Response(
            {"detail": "Your role does not grant MCP Gateway access."},
            status=http_status.HTTP_403_FORBIDDEN,
        )
    return organization, None


def _identity_refused(exc: IdentityRefused) -> Response:
    return Response(
        {"error": exc.code, "detail": exc.message}, status=http_status.HTTP_403_FORBIDDEN
    )


@api_view(["GET"])
@permission_classes([IsAuthenticatedOrganizationMember])
def mcp_tools(request):
    """GET /api/v1/mcp-gateway/tools/?agent= -- the unified catalog for a caller."""
    organization, refusal = _invoking_organization(request)
    if refusal:
        return refusal

    requested = request.query_params.get("agent", "")
    agent = AssetResolver(organization).agent(requested) if requested else None
    if requested and agent is None:
        return Response({"detail": "Unknown agent."}, status=http_status.HTTP_404_NOT_FOUND)
    try:
        agent = acting_agent(caller_service_account(request), agent, requested=bool(requested))
    except IdentityRefused as exc:
        return _identity_refused(exc)

    entries = MCPGateway.catalog(
        organization, agent, environment=request.query_params.get("environment", "")
    )
    return Response(
        {
            "agent": agent.agent_id if agent else None,
            "count": len(entries),
            "tools": [entry.as_payload() for entry in entries],
        }
    )


class ToolCallSerializer(serializers.Serializer):
    tool = serializers.CharField(max_length=300, help_text="Qualified name: <server>__<tool>.")
    arguments = serializers.DictField(required=False)
    agent = serializers.CharField(required=False, allow_blank=True, max_length=200)
    environment = serializers.ChoiceField(choices=Environment.choices, required=False)
    execution_id = serializers.CharField(required=False, allow_blank=True, max_length=200)


@api_view(["POST"])
@permission_classes([IsAuthenticatedOrganizationMember])
def mcp_call(request):
    """POST /api/v1/mcp-gateway/tools/call/ -- one enforced tool call.

    The status says what kind of answer it is: 200 the tool ran (check
    `is_error`), 403 the gateway refused, 503 the server is out of rotation or
    misconfigured, 502 the server failed, 404 no such tool.
    """
    organization, refusal = _invoking_organization(request)
    if refusal:
        return refusal

    serializer = ToolCallSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    agent = AssetResolver(organization).agent(data["agent"]) if data.get("agent") else None
    if data.get("agent") and agent is None:
        return Response({"detail": "Unknown agent."}, status=http_status.HTTP_404_NOT_FOUND)
    try:
        agent = acting_agent(
            caller_service_account(request), agent, requested=bool(data.get("agent"))
        )
    except IdentityRefused as exc:
        return _identity_refused(exc)

    try:
        outcome = MCPGateway.call(
            organization,
            ToolCallRequest(
                name=data["tool"],
                arguments=data.get("arguments") or {},
                agent=agent,
                environment=data.get("environment", ""),
                execution_id=data.get("execution_id", ""),
                channel=CallChannel.REST,
            ),
            service_account=caller_service_account(request),
        )
    except UnknownTool as exc:
        return Response(
            {"error": "unknown_tool", "detail": str(exc)}, status=http_status.HTTP_404_NOT_FOUND
        )
    return Response(outcome.as_payload(), status=outcome.http_status)


class MCPServerSerializer(serializers.ModelSerializer):
    transport_display = serializers.CharField(source="get_transport_display", read_only=True)
    gateway_reachable = serializers.BooleanField(source="is_gateway_reachable", read_only=True)
    enabled_tools = serializers.IntegerField(read_only=True)
    pending_tools = serializers.IntegerField(read_only=True)

    class Meta:
        model = MCPServer
        fields = [
            "id", "organization", "name", "slug", "description", "publisher", "category",
            "documentation_url", "transport", "transport_display", "endpoint", "auth_method",
            # The name of a secret, never the secret: the secret has no field at all.
            "credential_ref", "data_classification", "risk_level", "is_approved",
            "access_mode", "status", "health_status", "last_health_check", "circuit_state",
            "protocol_version", "server_info", "last_synced_at", "gateway_reachable",
            "enabled_tools", "pending_tools", "created_at", "updated_at",
        ]
        read_only_fields = fields


class MCPServerViewSet(viewsets.ReadOnlyModelViewSet):
    """The MCP registry, read-only.

    Registering, approving and reviewing are governed acts with an audit
    trail, made in the Control Tower rather than over this API.
    """

    serializer_class = MCPServerSerializer
    permission_classes = [IsAuthenticatedOrganizationMember, HasOrganizationPermission]
    required_permission = Perm.INVENTORY_VIEW
    queryset = MCPServer.objects.select_related("organization")
    filterset_fields = ["transport", "status", "is_approved", "access_mode", "health_status"]
    search_fields = ["name", "slug", "description", "publisher"]
    ordering_fields = ["name", "created_at"]

    def get_queryset(self):
        return (
            self.queryset.for_user(self.request.user)
            .annotate(
                enabled_tools=Count("tools", filter=Q(tools__status=ToolStatus.ENABLED)),
                pending_tools=Count("tools", filter=Q(tools__status=ToolStatus.PENDING_REVIEW)),
            )
            # Meta.ordering does not survive an aggregate, so pages would shuffle.
            .order_by("name")
        )
