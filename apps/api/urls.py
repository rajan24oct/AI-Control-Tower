from django.urls import include, path, re_path
from rest_framework.routers import DefaultRouter

from apps.api import (
    approval_views,
    controlplane_views,
    execution_views,
    gateway_views,
    lifecycle_views,
    mcp_views,
    views,
)

app_name = "api"

router = DefaultRouter()
router.register("providers", views.ProviderViewSet, basename="provider")
router.register("models", views.ModelViewSet, basename="model")
router.register("agents", views.AgentViewSet, basename="agent")
router.register("applications", views.ApplicationViewSet, basename="application")
router.register("organizations", views.OrganizationViewSet, basename="organization")
router.register("executions", execution_views.ExecutionViewSet, basename="execution")
router.register(
    "approval-requests", approval_views.ApprovalRequestViewSet, basename="approval-request"
)
router.register("mcp-servers", mcp_views.MCPServerViewSet, basename="mcp-server")
router.register("drift-records", controlplane_views.DriftRecordViewSet, basename="drift-record")
router.register(
    "deployment-authorizations", lifecycle_views.DeploymentAuthorizationViewSet,
    basename="deployment-authorization",
)

urlpatterns = [
    path("health/", views.health, name="health"),
    path("adapters/", views.adapter_catalog, name="adapter-catalog"),
    path("telemetry/webhook/", execution_views.telemetry_webhook, name="telemetry-webhook"),
    path("policy/check/", approval_views.policy_check, name="policy-check"),
    # The gateway: the one part of the API that is in the caller's critical path.
    path("gateway/chat/", gateway_views.chat, name="gateway-chat"),
    path("gateway/messages/", gateway_views.send_message, name="gateway-send-message"),
    path(
        "gateway/messages/<str:agent_id>/",
        gateway_views.collect_messages,
        name="gateway-collect-messages",
    ),
    # The MCP Gateway: one MCP endpoint in front of every governed server. The
    # slashless forms exist because MCP clients are rarely configured with a
    # trailing slash, and a redirect cannot carry a POST body.
    path("mcp/", mcp_views.mcp_endpoint, name="mcp-endpoint"),
    re_path(r"^mcp$", mcp_views.mcp_endpoint),
    path("mcp/<slug:agent_id>/", mcp_views.mcp_endpoint, name="mcp-endpoint-agent"),
    re_path(r"^mcp/(?P<agent_id>[-\w]+)$", mcp_views.mcp_endpoint),
    path("mcp-gateway/tools/", mcp_views.mcp_tools, name="mcp-tools"),
    path("mcp-gateway/tools/call/", mcp_views.mcp_call, name="mcp-tool-call"),
    path("", include(router.urls)),
]
