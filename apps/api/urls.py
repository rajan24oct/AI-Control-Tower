from django.urls import include, path, re_path
from rest_framework.routers import DefaultRouter

from apps.api import (
    a2a_views,
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
router.register("a2a-cards", a2a_views.A2ACardViewSet, basename="a2a-card")
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
    # The A2A Gateway: every published agent at an address of its own, with its
    # Agent Card at the well-known path. Slashless forms for the same reason as MCP.
    path("a2a/", a2a_views.a2a_directory, name="a2a-directory"),
    path("a2a/<slug:address>/", a2a_views.a2a_endpoint, name="a2a-endpoint"),
    re_path(r"^a2a/(?P<address>[-\w]+)$", a2a_views.a2a_endpoint),
    path("a2a/<slug:address>/.well-known/agent-card.json", a2a_views.a2a_card, name="a2a-card"),
    path("a2a/<slug:address>/.well-known/agent.json", a2a_views.a2a_card),
    path("a2a-gateway/messages/", a2a_views.a2a_send, name="a2a-send"),
    path("a2a-gateway/tasks/<uuid:task_id>/", a2a_views.a2a_task, name="a2a-task"),
    path("a2a-gateway/tasks/<uuid:task_id>/cancel/", a2a_views.a2a_task_cancel, name="a2a-task-cancel"),
    path("a2a-gateway/tasks/<uuid:task_id>/status/", a2a_views.a2a_task_status, name="a2a-task-status"),
    path("a2a-gateway/inbox/<str:agent_id>/", a2a_views.a2a_inbox, name="a2a-inbox"),
    path("", include(router.urls)),
]
