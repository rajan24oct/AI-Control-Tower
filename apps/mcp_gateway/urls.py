from django.urls import path

from apps.mcp_gateway import views

app_name = "mcp_gateway"

urlpatterns = [
    path("", views.MCPOverviewView.as_view(), name="overview"),
    path("servers/", views.MCPServerListView.as_view(), name="servers"),
    path("servers/<uuid:pk>/", views.MCPServerDetailView.as_view(), name="server_detail"),
    path("servers/<uuid:pk>/sync/", views.SyncServerView.as_view(), name="server_sync"),
    path("servers/<uuid:pk>/health/", views.HealthCheckServerView.as_view(), name="server_health"),
    path("servers/<uuid:pk>/approval/", views.ServerApprovalView.as_view(), name="server_approval"),
    path("tools/", views.MCPToolListView.as_view(), name="tools"),
    path("tools/<uuid:pk>/review/", views.ToolReviewView.as_view(), name="tool_review"),
    path("calls/", views.MCPToolCallListView.as_view(), name="calls"),
]
