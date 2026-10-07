"""A2A Gateway endpoints.

Two ways in, one set of rules, as with MCP. `/api/v1/a2a/<address>/` speaks
A2A itself: every published agent has an address there, with its Agent Card at
the usual well-known path, so any A2A client reaches it with a URL and a key.
The REST calls under `/api/v1/a2a-gateway/` make the same sends for code that
would rather not speak JSON-RPC -- and give mailbox agents, which have no
endpoint of their own, their inbox and a way to answer.
"""

from __future__ import annotations

import json
import uuid

from django.conf import settings
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from rest_framework import serializers, status as http_status, viewsets
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from apps.a2a.models import A2AAgentCard, A2AChannel, SkillStatus, TaskState
from apps.a2a.protocol import (
    INVALID_REQUEST,
    PARSE_ERROR,
    UNKNOWN_AGENT,
    A2AEndpoint,
    Caller,
    error_response,
)
from apps.a2a.registry import published_card
from apps.a2a.services import (
    ANSWER_STATES,
    A2AGateway,
    A2AGatewayError,
    SendRequest,
    TaskNotCancelable,
    TaskNotFound,
    UnknownRecipient,
)
from apps.a2a.wire import MessageInvalid, inbox_entry, rest_payload, validate_message
from apps.accounts.roles import Perm
from apps.api.execution_views import caller_organization, caller_service_account
from apps.api.identity import IdentityRefused, acting_agent
from apps.api.mcp_views import MCPKeyAuthentication
from apps.api.permissions import HasOrganizationPermission, IsAuthenticatedOrganizationMember
from apps.core.constants import DataClassification, Environment
from apps.executions.services import AssetResolver


class A2AKeyAuthentication(MCPKeyAuthentication):
    """Also accepts `Authorization: Bearer act_...`, which A2A clients are usually configured with."""

    def authenticate_header(self, request):
        return 'Bearer realm="ai-control-tower-a2a"'


def _jsonrpc_error(code: int, message: str, *, status: int) -> JsonResponse:
    return JsonResponse(error_response(None, code, message), status=status)


def _origin_allowed(request) -> bool:
    """Refuse browser pages from origins nobody configured, as the MCP endpoint does."""
    origin = request.META.get("HTTP_ORIGIN")
    if not origin:
        return True
    allowed = set(settings.CSRF_TRUSTED_ORIGINS) | set(
        settings.CONTROL_TOWER.get("A2A_GATEWAY", {}).get("ALLOWED_ORIGINS", [])
    )
    return origin in allowed


def _address_url(request, card) -> str:
    return request.build_absolute_uri(reverse("api:a2a-endpoint", args=[card.slug]))


def _invoking(request):
    """The caller's organization, or the response refusing them."""
    organization = caller_organization(request)
    if organization is None:
        return None, Response(
            {"detail": "No organization resolved for this caller."}, status=http_status.HTTP_400_BAD_REQUEST
        )
    if not request.user.has_org_permission(organization, Perm.A2A_INVOKE):
        return None, Response(
            {"detail": "Your role does not grant A2A Gateway access."}, status=http_status.HTTP_403_FORBIDDEN
        )
    return organization, None


def _acting(request, organization, requested: str = ""):
    """The agent this request acts for (IRGF link 02), or the response refusing it."""
    requested = requested or request.META.get("HTTP_X_AGENT_ID", "")
    agent = None
    if requested:
        agent = AssetResolver(organization).agent(requested)
        if agent is None:
            return None, Response({"detail": "Unknown agent."}, status=http_status.HTTP_404_NOT_FOUND)
    try:
        return acting_agent(caller_service_account(request), agent, requested=bool(requested)), None
    except IdentityRefused as exc:
        return None, Response({"error": exc.code, "detail": exc.message}, status=http_status.HTTP_403_FORBIDDEN)


def _needs_agent() -> Response:
    return Response(
        {"detail": "Name the agent this request is for, or call with a key bound to it."},
        status=http_status.HTTP_400_BAD_REQUEST,
    )


# -- The A2A endpoint ----------------------------------------------------------


@api_view(["GET", "POST", "DELETE"])
@authentication_classes([A2AKeyAuthentication])
@permission_classes([IsAuthenticatedOrganizationMember])
def a2a_endpoint(request, address):
    """POST /api/v1/a2a/{address}/ -- a published agent, as an A2A server."""
    if request.method != "POST":
        response = _jsonrpc_error(INVALID_REQUEST, "Only POST is supported; the endpoint is stateless.", status=405)
        response["Allow"] = "POST"
        return response
    if not _origin_allowed(request):
        return _jsonrpc_error(INVALID_REQUEST, "Origin not allowed.", status=403)

    organization = caller_organization(request)
    if organization is None:
        return _jsonrpc_error(INVALID_REQUEST, "No organization resolved for this caller.", status=400)
    if not request.user.has_org_permission(organization, Perm.A2A_INVOKE):
        return _jsonrpc_error(INVALID_REQUEST, "Your role does not grant A2A Gateway access.", status=403)
    try:
        card = A2AGateway.resolve(organization, address)
    except UnknownRecipient:
        return _jsonrpc_error(UNKNOWN_AGENT, f"No agent is published at '{address}'.", status=404)

    # The sender rides in a header -- the address is the recipient's -- or is
    # the one agent the key is bound to.
    requested = request.META.get("HTTP_X_AGENT_ID", "")
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
    reply = A2AEndpoint(
        Caller(
            organization=organization,
            card=card,
            agent=agent,
            service_account=caller_service_account(request),
            environment=environment if environment in Environment.values else "",
            execution_id=request.META.get("HTTP_X_EXECUTION_ID", "")[:200],
            url=_address_url(request, card),
        )
    ).handle(payload)
    if reply is None:
        return HttpResponse(status=http_status.HTTP_202_ACCEPTED)
    return JsonResponse(reply, safe=False)


@api_view(["GET"])
@authentication_classes([A2AKeyAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticatedOrganizationMember])
def a2a_card(request, address):
    """GET /api/v1/a2a/{address}/.well-known/agent-card.json -- the Agent Card other agents are shown."""
    organization, refusal = _invoking(request)
    if refusal:
        return refusal
    try:
        card = A2AGateway.resolve(organization, address)
    except UnknownRecipient:
        card = None
    # An agent nobody approved does not exist, as far as other agents are concerned.
    if card is None or not card.is_serving:
        return Response({"detail": "No agent is published there."}, status=http_status.HTTP_404_NOT_FOUND)
    return Response(published_card(card, url=_address_url(request, card)))


@api_view(["GET"])
@authentication_classes([A2AKeyAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticatedOrganizationMember])
def a2a_directory(request):
    """GET /api/v1/a2a/?agent= -- the agents a sender may reach, and the skills it may ask each for."""
    organization, refusal = _invoking(request)
    if refusal:
        return refusal
    agent, refusal = _acting(request, organization, request.query_params.get("agent", ""))
    if refusal:
        return refusal
    if agent is None:
        return _needs_agent()
    entries = A2AGateway.directory(organization, agent, environment=request.query_params.get("environment", ""))
    return Response(
        {
            "agent": agent.agent_id,
            "count": len(entries),
            "agents": [entry.as_payload(url=_address_url(request, entry.card)) for entry in entries],
        }
    )


# -- REST ---------------------------------------------------------------------


class SendSerializer(serializers.Serializer):
    agent = serializers.CharField(required=False, allow_blank=True, max_length=200, help_text="The sending agent.")
    recipient = serializers.CharField(max_length=200, help_text="The address of the agent to send to.")
    text = serializers.CharField(required=False, allow_blank=True)
    parts = serializers.ListField(child=serializers.DictField(), required=False)
    data = serializers.DictField(required=False, help_text="Structured content, sent as a data part.")
    skill = serializers.CharField(required=False, allow_blank=True, max_length=128)
    data_classification = serializers.ChoiceField(choices=DataClassification.choices, required=False)
    context_id = serializers.CharField(required=False, allow_blank=True, max_length=200)
    task_id = serializers.CharField(
        required=False, allow_blank=True, max_length=200, help_text="Answer a task that is waiting for the sender."
    )
    parent_task_id = serializers.CharField(
        required=False, allow_blank=True, max_length=200, help_text="The task the sender is handing on work from."
    )
    execution_id = serializers.CharField(required=False, allow_blank=True, max_length=200)
    environment = serializers.ChoiceField(choices=Environment.choices, required=False)
    metadata = serializers.DictField(required=False)

    def validate(self, attrs):
        if not attrs.get("text") and not attrs.get("parts") and attrs.get("data") is None:
            raise serializers.ValidationError("Send text, parts or data.")
        return attrs

    def message(self) -> dict:
        data = self.validated_data
        parts = list(data.get("parts") or [])
        if data.get("text"):
            parts.insert(0, {"kind": "text", "text": data["text"]})
        if data.get("data") is not None:
            parts.append({"kind": "data", "data": data["data"]})
        message = {
            "kind": "message", "role": "user", "messageId": str(uuid.uuid4()),
            "parts": parts, "metadata": data.get("metadata") or {},
        }
        if data.get("context_id"):
            message["contextId"] = data["context_id"]
        if data.get("task_id"):
            message["taskId"] = data["task_id"]
        return message


@api_view(["POST"])
@permission_classes([IsAuthenticatedOrganizationMember])
def a2a_send(request):
    """POST /api/v1/a2a-gateway/messages/ -- one agent hands another work, over REST.

    The status says what kind of answer it is: 201 delivered (200 for an answer
    to a waiting task), 403 the gateway refused, 503 the agent is out of
    rotation or misconfigured, 502 it failed, 404 no such agent or task.
    """
    organization, refusal = _invoking(request)
    if refusal:
        return refusal
    serializer = SendSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    agent, refusal = _acting(request, organization, data.get("agent", ""))
    if refusal:
        return refusal

    message = serializer.message()
    try:
        validate_message(message)
    except MessageInvalid as exc:
        return Response({"detail": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST)
    request_ = SendRequest.from_message(
        message, sender=agent, skill=data.get("skill", ""), data_classification=data.get("data_classification", ""),
        parent_task_id=data.get("parent_task_id", ""), execution_id=data.get("execution_id", ""),
        environment=data.get("environment", ""), channel=A2AChannel.REST,
    )
    try:
        outcome = A2AGateway.send(
            organization, request_, recipient=data["recipient"], service_account=caller_service_account(request),
        )
    except UnknownRecipient:
        return Response(
            {"error": "unknown_agent", "detail": f"No agent is published at '{data['recipient']}'."},
            status=http_status.HTTP_404_NOT_FOUND,
        )
    except TaskNotFound:
        return Response({"error": "unknown_task", "detail": "No such task."}, status=http_status.HTTP_404_NOT_FOUND)
    except A2AGatewayError as exc:
        return Response({"error": "not_accepted", "detail": str(exc)}, status=http_status.HTTP_409_CONFLICT)

    if outcome.task is None:
        return Response(
            {"error": "not_recorded", "status": outcome.status, "detail": outcome.reason},
            status=http_status.HTTP_502_BAD_GATEWAY,
        )
    if outcome.delivered:
        status = http_status.HTTP_200_OK if outcome.continued is not None else http_status.HTTP_201_CREATED
    else:
        status = outcome.http_status
    return Response(rest_payload(outcome.task), status=status)


@api_view(["GET"])
@permission_classes([IsAuthenticatedOrganizationMember])
def a2a_task(request, task_id):
    """GET /api/v1/a2a-gateway/tasks/{id}/?agent= -- where a task stands, for its sender or its recipient."""
    organization, refusal = _invoking(request)
    if refusal:
        return refusal
    agent, refusal = _acting(request, organization, request.query_params.get("agent", ""))
    if refusal:
        return refusal
    if agent is None:
        return _needs_agent()
    try:
        task = A2AGateway.get(organization, task_id, agent=agent)
    except TaskNotFound:
        return Response({"error": "unknown_task", "detail": "No such task."}, status=http_status.HTTP_404_NOT_FOUND)
    return Response(rest_payload(task))


@api_view(["POST"])
@permission_classes([IsAuthenticatedOrganizationMember])
def a2a_task_cancel(request, task_id):
    """POST /api/v1/a2a-gateway/tasks/{id}/cancel/ -- the sender withdraws a task."""
    organization, refusal = _invoking(request)
    if refusal:
        return refusal
    agent, refusal = _acting(request, organization, str(request.data.get("agent", "") or ""))
    if refusal:
        return refusal
    if agent is None:
        return _needs_agent()
    try:
        task = A2AGateway.cancel(organization, task_id, agent=agent)
    except TaskNotFound:
        return Response({"error": "unknown_task", "detail": "No such task."}, status=http_status.HTTP_404_NOT_FOUND)
    except TaskNotCancelable as exc:
        return Response({"error": "not_cancelable", "detail": str(exc)}, status=http_status.HTTP_409_CONFLICT)
    except A2AGatewayError as exc:
        return Response({"error": "agent_unreachable", "detail": str(exc)}, status=http_status.HTTP_502_BAD_GATEWAY)
    return Response(rest_payload(task))


class AnswerSerializer(serializers.Serializer):
    agent = serializers.CharField(required=False, allow_blank=True, max_length=200)
    state = serializers.ChoiceField(choices=[(state, state) for state in ANSWER_STATES])
    text = serializers.CharField(required=False, allow_blank=True)
    message = serializers.DictField(required=False)
    artifacts = serializers.ListField(child=serializers.DictField(), required=False)


@api_view(["POST"])
@permission_classes([IsAuthenticatedOrganizationMember])
def a2a_task_status(request, task_id):
    """POST /api/v1/a2a-gateway/tasks/{id}/status/ -- a mailbox agent reports progress, or answers."""
    organization, refusal = _invoking(request)
    if refusal:
        return refusal
    serializer = AnswerSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    agent, refusal = _acting(request, organization, data.get("agent", ""))
    if refusal:
        return refusal
    if agent is None:
        return _needs_agent()
    message = data.get("message")
    if message is None and data.get("text"):
        message = {"parts": [{"kind": "text", "text": data["text"]}]}
    artifacts = data.get("artifacts") or []
    if data.get("text") and not artifacts and data["state"] == TaskState.COMPLETED:
        # A plain-text answer is the result: keep it as the task's artifact too.
        artifacts = [{"artifactId": str(uuid.uuid4()), "name": "answer", "parts": [{"kind": "text", "text": data["text"]}]}]
    try:
        task = A2AGateway.respond(
            organization, task_id, agent=agent, state=data["state"], message=message, artifacts=artifacts,
        )
    except TaskNotFound:
        return Response({"error": "unknown_task", "detail": "No such task."}, status=http_status.HTTP_404_NOT_FOUND)
    except A2AGatewayError as exc:
        return Response({"error": "not_accepted", "detail": str(exc)}, status=http_status.HTTP_409_CONFLICT)
    return Response(rest_payload(task))


@api_view(["GET"])
@permission_classes([IsAuthenticatedOrganizationMember])
def a2a_inbox(request, agent_id):
    """GET /api/v1/a2a-gateway/inbox/{agent}/ -- a mailbox agent collects the work waiting for it.

    A pull, not a push: the gateway never calls into a mailbox agent, so one
    that is down cannot slow the gateway, and its work simply waits.
    """
    organization, refusal = _invoking(request)
    if refusal:
        return refusal
    agent, refusal = _acting(request, organization, agent_id)
    if refusal:
        return refusal
    if agent.is_suspended:
        return Response(
            {"error": "agent_suspended", "detail": f"Agent '{agent.name}' is suspended by its kill-switch."},
            status=http_status.HTTP_403_FORBIDDEN,
        )
    tasks = A2AGateway.collect(organization, agent)
    return Response({"agent": agent.agent_id, "count": len(tasks), "tasks": [inbox_entry(task) for task in tasks]})


class A2ACardSerializer(serializers.ModelSerializer):
    agent_id = serializers.CharField(source="agent.agent_id", read_only=True, default=None)
    enabled_skills = serializers.IntegerField(read_only=True)
    pending_skills = serializers.IntegerField(read_only=True)
    contracts = serializers.IntegerField(source="contract_count", read_only=True)

    class Meta:
        model = A2AAgentCard
        fields = [
            "id", "organization", "name", "slug", "description", "origin", "agent_id", "publisher",
            "documentation_url", "transport", "endpoint", "auth_method",
            # The name of a secret, never the secret: the secret has no field at all.
            "credential_ref", "data_classification", "risk_level", "is_approved", "access_mode", "status",
            "health_status", "circuit_state", "protocol_version", "agent_version", "last_synced_at",
            "enabled_skills", "pending_skills", "contracts", "created_at", "updated_at",
        ]
        read_only_fields = fields


class A2ACardViewSet(viewsets.ReadOnlyModelViewSet):
    """The A2A registry, read-only.

    Publishing, approving, reviewing and granting contracts are governed acts
    with an audit trail, made in the Control Tower rather than over this API.
    """

    serializer_class = A2ACardSerializer
    permission_classes = [IsAuthenticatedOrganizationMember, HasOrganizationPermission]
    required_permission = Perm.INVENTORY_VIEW
    queryset = A2AAgentCard.objects.select_related("organization", "agent")
    filterset_fields = ["origin", "transport", "status", "is_approved", "access_mode", "health_status"]
    search_fields = ["name", "slug", "description", "publisher"]
    ordering_fields = ["name", "created_at"]

    def get_queryset(self):
        return (
            self.queryset.for_user(self.request.user)
            .annotate(
                enabled_skills=Count("skills", filter=Q(skills__status=SkillStatus.ENABLED), distinct=True),
                pending_skills=Count("skills", filter=Q(skills__status=SkillStatus.PENDING_REVIEW), distinct=True),
                contract_count=Count("contracts", distinct=True),
            )
            # Meta.ordering does not survive an aggregate, so pages would shuffle.
            .order_by("name")
        )
