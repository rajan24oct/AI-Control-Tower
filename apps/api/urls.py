from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.api import approval_views, execution_views, gateway_views, views

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
    path("", include(router.urls)),
]
