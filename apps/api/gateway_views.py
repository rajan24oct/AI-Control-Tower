"""AI Gateway endpoints.

Unlike the rest of the API, these are in the caller's critical path: an agent
is blocked on the response. So the contract is deliberately narrow and the
failure modes are explicit -- a caller must be able to tell "you may not"
(403), "wait for a human" (202), "you are out of budget" (402), "nothing is
available" (503) and "everything I tried broke" (502) apart, because the right
client behaviour differs for each.
"""

from __future__ import annotations

import json

from django.http import StreamingHttpResponse
from rest_framework import serializers, status as http_status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.accounts.roles import Perm
from apps.api.execution_views import caller_organization, caller_service_account
from apps.api.identity import IdentityRefused, acting_agent
from apps.api.permissions import IsAuthenticatedOrganizationMember
from apps.core.constants import Environment
from apps.executions.services import AssetResolver
from apps.gateway.messaging import MessageBlocked, MessageRequest, MessagingService
from apps.gateway.models import GatewayMessage
from apps.gateway.services import GatewayError, GatewayRequest, GatewayService
from apps.models_registry.models import ModelType


class ChatSerializer(serializers.Serializer):
    """A model call, in the shape an agent already speaks."""

    agent = serializers.CharField(required=False, allow_blank=True, max_length=200)
    messages = serializers.ListField(child=serializers.DictField(), required=False)
    prompt = serializers.CharField(required=False, allow_blank=True)
    model = serializers.CharField(
        required=False, allow_blank=True, max_length=200,
        help_text="Pin a model. Without it the platform chooses and falls back.",
    )
    model_type = serializers.ChoiceField(choices=ModelType.choices, required=False)
    environment = serializers.ChoiceField(choices=Environment.choices, required=False)
    max_tokens = serializers.IntegerField(required=False, min_value=1)
    temperature = serializers.FloatField(required=False, min_value=0, max_value=2)
    tools = serializers.ListField(child=serializers.DictField(), required=False)
    trace_id = serializers.CharField(required=False, allow_blank=True, max_length=200)
    execution_id = serializers.CharField(required=False, allow_blank=True, max_length=200)
    metadata = serializers.JSONField(required=False)
    stream = serializers.BooleanField(
        required=False, default=False,
        help_text="Return server-sent events instead of one JSON body.",
    )

    def validate(self, attrs):
        if not attrs.get("messages") and not attrs.get("prompt"):
            raise serializers.ValidationError("Provide either 'messages' or 'prompt'.")
        return attrs


def _denied(request, organization, permission):
    return not request.user.has_org_permission(organization, permission)


def _identity_refused(exc: IdentityRefused) -> Response:
    return Response(
        {"error": exc.code, "detail": exc.message}, status=http_status.HTTP_403_FORBIDDEN
    )


@api_view(["POST"])
@permission_classes([IsAuthenticatedOrganizationMember])
def chat(request):
    """POST /api/v1/gateway/chat/ -- run a model call through the platform."""
    organization = caller_organization(request)
    if organization is None:
        return Response(
            {"detail": "No organization resolved for this caller."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    if _denied(request, organization, Perm.GATEWAY_INVOKE):
        return Response(
            {"detail": "Your role does not grant gateway access."},
            status=http_status.HTTP_403_FORBIDDEN,
        )

    serializer = ChatSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    service_account = caller_service_account(request)
    resolver = AssetResolver(organization)
    try:
        agent = acting_agent(
            service_account, resolver.agent(data.get("agent")), requested=bool(data.get("agent"))
        )
    except IdentityRefused as exc:
        return _identity_refused(exc)

    gateway_request = GatewayRequest(
        agent=agent,
        messages=data.get("messages") or [],
        prompt=data.get("prompt", ""),
        model=data.get("model", ""),
        model_type=data.get("model_type") or ModelType.CHAT,
        environment=data.get("environment")
        or getattr(agent, "environment", None)
        or Environment.PRODUCTION,
        max_tokens=data.get("max_tokens"),
        temperature=data.get("temperature"),
        tools=data.get("tools") or [],
        trace_id=data.get("trace_id", ""),
        external_id=data.get("execution_id", ""),
        metadata=data.get("metadata") or {},
    )

    if data.get("stream"):
        return _stream_response(organization, gateway_request, service_account)

    try:
        response = GatewayService.complete(
            organization, gateway_request, service_account=service_account
        )
    except GatewayError as exc:
        return Response(
            {"error": exc.code, "detail": exc.message, **exc.detail},
            status=exc.status_code,
        )

    return Response(response.as_payload())


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def _stream_response(organization, gateway_request, service_account):
    """Server-sent events, with refusals kept as real HTTP statuses.

    The generator is started here rather than handed straight to Django,
    because everything the platform enforces happens before the first byte:
    once a 200 and a content type are on the wire, a policy refusal can only
    be reported as text inside a successful response, which no client should
    have to parse to find out it was denied.
    """
    events = GatewayService.stream(
        organization, gateway_request, service_account=service_account
    )

    try:
        first = next(events)
    except StopIteration:
        return Response(
            {"error": "empty_stream", "detail": "The provider produced no output."},
            status=http_status.HTTP_502_BAD_GATEWAY,
        )
    except GatewayError as exc:
        return Response(
            {"error": exc.code, "detail": exc.message, **exc.detail},
            status=exc.status_code,
        )

    def body():
        yield _sse(*first)
        for event, payload in events:
            yield _sse(event, payload)

    response = StreamingHttpResponse(body(), content_type="text/event-stream")
    # Proxies that buffer would defeat the point of streaming at all.
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


class MessageSerializer(serializers.Serializer):
    sender = serializers.CharField(max_length=200)
    recipient = serializers.CharField(max_length=200)
    body = serializers.CharField()
    subject = serializers.CharField(required=False, allow_blank=True, max_length=255)
    correlation_id = serializers.CharField(required=False, allow_blank=True, max_length=200)
    environment = serializers.ChoiceField(choices=Environment.choices, required=False)
    metadata = serializers.JSONField(required=False)


class GatewayMessageSerializer(serializers.ModelSerializer):
    sender = serializers.CharField(source="sender.agent_id", read_only=True)
    recipient = serializers.CharField(source="recipient.agent_id", read_only=True)

    class Meta:
        model = GatewayMessage
        fields = [
            "id", "sender", "recipient", "subject", "body", "correlation_id",
            "metadata", "status", "blocked_reason", "created_at", "read_at",
        ]
        read_only_fields = fields


@api_view(["POST"])
@permission_classes([IsAuthenticatedOrganizationMember])
def send_message(request):
    """POST /api/v1/gateway/messages/ -- one agent writes to another."""
    organization = caller_organization(request)
    if organization is None:
        return Response(
            {"detail": "No organization resolved for this caller."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    if _denied(request, organization, Perm.GATEWAY_INVOKE):
        return Response(
            {"detail": "Your role does not grant gateway access."},
            status=http_status.HTTP_403_FORBIDDEN,
        )

    serializer = MessageSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    service_account = caller_service_account(request)
    resolver = AssetResolver(organization)
    sender = resolver.agent(data["sender"])
    if sender is not None:
        # The sender is the agent acting, so it is the one the identity must hold.
        try:
            acting_agent(service_account, sender, requested=True)
        except IdentityRefused as exc:
            return _identity_refused(exc)

    try:
        message = MessagingService.send(
            organization,
            MessageRequest(
                sender=sender,
                recipient=resolver.agent(data["recipient"]),
                body=data["body"],
                subject=data.get("subject", ""),
                correlation_id=data.get("correlation_id", ""),
                environment=data.get("environment") or Environment.PRODUCTION,
                metadata=data.get("metadata") or {},
            ),
            service_account=service_account,
        )
    except MessageBlocked as exc:
        return Response(
            {"error": "message_blocked", "detail": exc.message, "policies": exc.policies},
            status=http_status.HTTP_403_FORBIDDEN,
        )

    return Response(
        GatewayMessageSerializer(message).data, status=http_status.HTTP_201_CREATED
    )


@api_view(["GET"])
@permission_classes([IsAuthenticatedOrganizationMember])
def collect_messages(request, agent_id):
    """GET /api/v1/gateway/messages/{agent}/ -- an agent collects its mail.

    A pull, not a push: the gateway never calls into an agent, so an agent that
    is down cannot slow the gateway and its messages simply wait.
    """
    organization = caller_organization(request)
    if organization is None:
        return Response(
            {"detail": "No organization resolved for this caller."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )
    if _denied(request, organization, Perm.GATEWAY_INVOKE):
        return Response(
            {"detail": "Your role does not grant gateway access."},
            status=http_status.HTTP_403_FORBIDDEN,
        )

    agent = AssetResolver(organization).agent(agent_id)
    if agent is None:
        return Response({"detail": "Unknown agent."}, status=http_status.HTTP_404_NOT_FOUND)
    try:
        acting_agent(caller_service_account(request), agent, requested=True)
    except IdentityRefused as exc:
        return _identity_refused(exc)
    if agent.is_suspended:
        return Response(
            {
                "error": "agent_suspended",
                "detail": f"Agent '{agent.name}' is suspended by its kill-switch.",
            },
            status=http_status.HTTP_403_FORBIDDEN,
        )

    messages = MessagingService.collect(organization, agent)
    return Response(
        {
            "agent": agent.agent_id,
            "count": len(messages),
            "messages": GatewayMessageSerializer(messages, many=True).data,
        }
    )
