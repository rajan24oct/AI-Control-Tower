"""Approval and policy-check APIs (sections 18, 19, 20).

External engines use these to ask "may I proceed?" before acting, to raise a
request when a human must decide, and to poll for the outcome.
"""

from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers, status as http_status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response

from apps.accounts.roles import Perm
from apps.agents.models import AIAgent
from apps.api.execution_views import caller_organization, caller_service_account
from apps.api.permissions import HasOrganizationPermission, IsAuthenticatedOrganizationMember
from apps.applications_registry.models import AIApplication
from apps.approvals.models import ApprovalRequest, ApprovalStatus
from apps.approvals.services import ApprovalContext, ApprovalRuleEngine, ApprovalService
from apps.executions.models import Execution
from apps.executions.services import AssetResolver
from apps.policies.services import PolicyContext, PolicyEngine


class ApprovalRequestSerializer(serializers.ModelSerializer):
    agent_name = serializers.CharField(source="agent.name", read_only=True, default=None)
    execution_id = serializers.CharField(
        source="execution.external_id", read_only=True, default=None
    )

    class Meta:
        model = ApprovalRequest
        fields = [
            "id",
            "request_id",
            "organization",
            "execution",
            "execution_id",
            "agent",
            "agent_name",
            "application",
            "requested_action",
            "reason",
            "risk_level",
            "requested_by_system",
            "requested_at",
            "assigned_approver",
            "status",
            "expires_at",
            "decision_at",
            "decision_by",
            "decision_notes",
            "matched_rules",
            "context",
        ]
        read_only_fields = fields


class PolicyCheckSerializer(serializers.Serializer):
    """Input for a policy check. All identifiers are loose (section 18)."""

    agent = serializers.CharField(required=False, allow_blank=True, max_length=200)
    application = serializers.CharField(required=False, allow_blank=True, max_length=200)
    model = serializers.CharField(required=False, allow_blank=True, max_length=200)
    provider = serializers.CharField(required=False, allow_blank=True, max_length=200)
    execution_id = serializers.CharField(required=False, allow_blank=True, max_length=200)
    action = serializers.CharField(required=False, allow_blank=True, max_length=200)
    tool = serializers.CharField(required=False, allow_blank=True, max_length=200)
    data_classification = serializers.CharField(required=False, allow_blank=True, max_length=40)
    environment = serializers.CharField(required=False, allow_blank=True, max_length=40)
    estimated_cost = serializers.DecimalField(
        max_digits=12, decimal_places=4, required=False, default=Decimal("0")
    )


class ApprovalCreateSerializer(PolicyCheckSerializer):
    requested_action = serializers.CharField(max_length=255)
    reason = serializers.CharField(required=False, allow_blank=True)
    expires_in_hours = serializers.IntegerField(required=False, min_value=0, max_value=720)


def build_policy_context(organization, data: dict) -> PolicyContext:
    """Resolve loose identifiers into registry records for evaluation."""
    resolver = AssetResolver(organization)
    agent = resolver.agent(data.get("agent"))

    execution = None
    if data.get("execution_id"):
        execution = (
            Execution.objects.for_organization(organization)
            .filter(external_id=data["execution_id"])
            .first()
        )

    application = resolver.application(data.get("application"))
    if application is None and execution is not None:
        application = execution.application

    return PolicyContext(
        agent=agent or (execution.agent if execution else None),
        application=application,
        model=resolver.model(data.get("model")),
        provider=resolver.provider(data.get("provider")),
        execution=execution,
        action=data.get("action", ""),
        tool=data.get("tool", ""),
        data_classification=data.get("data_classification", ""),
        environment=data.get("environment", ""),
        estimated_cost=data.get("estimated_cost") or Decimal("0"),
    )


def build_context(organization, data: dict) -> ApprovalContext:
    """Approval-rule view of the same request."""
    return build_policy_context(organization, data).to_approval_context()


class ApprovalRequestViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ApprovalRequestSerializer
    permission_classes = [IsAuthenticatedOrganizationMember, HasOrganizationPermission]
    required_permission = Perm.APPROVAL_VIEW
    required_write_permission = Perm.TELEMETRY_INGEST

    queryset = ApprovalRequest.objects.select_related("agent", "application", "execution")
    filterset_fields = ["status", "risk_level", "agent", "application"]
    search_fields = ["request_id", "requested_action", "reason"]
    ordering_fields = ["requested_at", "risk_level"]

    def get_queryset(self):
        return self.queryset.for_user(self.request.user)

    def create(self, request, *args, **kwargs):
        """POST /api/v1/approval-requests/ -- an engine asks for a decision."""
        serializer = ApprovalCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)

        organization = caller_organization(request)
        if organization is None:
            return Response(
                {"detail": "No organization resolved for this caller."},
                status=http_status.HTTP_400_BAD_REQUEST,
            )

        context = build_context(organization, data)
        service_account = caller_service_account(request)

        approval = ApprovalService.request(
            organization=organization,
            requested_action=data["requested_action"],
            context=context,
            reason=data.get("reason", ""),
            requested_by=None if service_account else request.user,
            requested_by_system=getattr(service_account, "name", ""),
            expires_in_hours=data.get("expires_in_hours", 24),
            matched_rules=ApprovalRuleEngine.evaluate(organization, context).as_metadata(),
        )
        return Response(
            ApprovalRequestSerializer(approval).data, status=http_status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["get"], url_path="status", url_name="status")
    def decision_status(self, request, pk=None):
        """Lightweight poll target for a waiting engine (section 19)."""
        approval = self.get_object()
        return Response(
            {
                "request_id": approval.request_id,
                "status": approval.status,
                "decided": approval.is_decided,
                "approved": approval.status == ApprovalStatus.APPROVED,
                "decision_at": approval.decision_at,
                "decision_notes": approval.decision_notes,
                "expires_at": approval.expires_at,
            }
        )


@api_view(["POST"])
@permission_classes([IsAuthenticatedOrganizationMember])
def policy_check(request):
    """POST /api/v1/policy/check/ -- may this action proceed? (section 18)

    Backed by the policy engine, which can deny outright as well as route to a
    human. Denials and warnings are recorded, so an engine that ignores a
    `allowed: false` verdict still leaves an auditable trail.
    """
    organization = caller_organization(request)
    if organization is None:
        return Response(
            {"detail": "No organization resolved for this caller."},
            status=http_status.HTTP_400_BAD_REQUEST,
        )

    serializer = PolicyCheckSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = dict(serializer.validated_data)

    context = build_policy_context(organization, data)
    decision = PolicyEngine.check(
        organization, context, requested_action=data.get("action", "")
    )
    return Response(decision.as_payload())
