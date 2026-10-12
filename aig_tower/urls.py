from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("apps.accounts.urls")),
    path("", include("apps.dashboard.urls")),
    path("inventory/", include("apps.inventory.urls")),
    path("onboarding/", include("apps.onboarding.urls")),
    path("access/", include("apps.access.urls")),
    path("executions/", include("apps.executions.urls")),
    path("costs/", include("apps.costs.urls")),
    path("risks/", include("apps.risks.urls")),
    path("approvals/", include("apps.approvals.urls")),
    path("policies/", include("apps.policies.urls")),
    path("classification/", include("apps.classification.urls")),
    path("lifecycle/", include("apps.lifecycle.urls")),
    path("control-plane/", include("apps.controlplane.urls")),
    path("routing/", include("apps.routing.urls")),
    path("gateway/", include("apps.gateway.urls")),
    path("mcp-gateway/", include("apps.mcp_gateway.urls")),
    path("a2a-gateway/", include("apps.a2a.urls")),
    path("evaluations/", include("apps.evaluations.urls")),
    path("notifications/", include("apps.notifications.urls")),
    path("audit/", include("apps.audit.urls")),
    path("intake/", include("apps.intake.urls")),
    path("compliance/", include("apps.compliance.urls")),
    path("guardrails/", include("apps.guardrails.urls")),
    path("studio/", include("apps.studio.urls")),
    path("assistant/", include("apps.assistant.urls")),
    path("api/v1/", include("apps.api.urls")),
]

admin.site.site_header = "AI Control Tower"
admin.site.site_title = "AI Control Tower"
admin.site.index_title = "Control Tower Administration"
