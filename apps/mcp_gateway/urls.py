from django.urls import path

from apps.mcp_gateway import manage, views

app_name = "mcp_gateway"

urlpatterns = [
    path("", views.MCPOverviewView.as_view(), name="overview"),
    path("servers/", views.MCPServerListView.as_view(), name="servers"),
    path("servers/new/", manage.MCPServerFormView.as_view(), name="server_create"),
    path("servers/<uuid:pk>/", views.MCPServerDetailView.as_view(), name="server_detail"),
    path("servers/<uuid:pk>/edit/", manage.MCPServerFormView.as_view(), name="server_edit"),
    path("servers/<uuid:server_pk>/grants/new/", manage.AccessGrantFormView.as_view(), name="grant_create"),
    path("grants/<uuid:pk>/edit/", manage.AccessGrantFormView.as_view(), name="grant_edit"),
    path("grants/<uuid:pk>/revoke/", manage.AccessGrantRevokeView.as_view(), name="grant_revoke"),
    path("servers/<uuid:pk>/sync/", views.SyncServerView.as_view(), name="server_sync"),
    path("servers/<uuid:pk>/health/", views.HealthCheckServerView.as_view(), name="server_health"),
    path("servers/<uuid:pk>/approval/", views.ServerApprovalView.as_view(), name="server_approval"),
    path("tools/", views.MCPToolListView.as_view(), name="tools"),
    path("tools/<uuid:pk>/review/", views.ToolReviewView.as_view(), name="tool_review"),
    path("tools/<uuid:pk>/limits/", views.ToolLimitsView.as_view(), name="tool_limits"),
    path("calls/", views.MCPToolCallListView.as_view(), name="calls"),
]
