from django.urls import path

from apps.inventory import actions, manage, views

app_name = "inventory"

urlpatterns = [
    path("", views.InventoryIndexView.as_view(), name="index"),
    path("applications/", views.ApplicationListView.as_view(), name="applications"),
    path("applications/new/", manage.ApplicationFormView.as_view(), name="application_create"),
    path("applications/<uuid:pk>/", views.ApplicationDetailView.as_view(), name="application_detail"),
    path("applications/<uuid:pk>/edit/", manage.ApplicationFormView.as_view(), name="application_edit"),
    path("agents/", views.AgentListView.as_view(), name="agents"),
    path("agents/new/", manage.AgentFormView.as_view(), name="agent_create"),
    path("agents/<uuid:pk>/", views.AgentDetailView.as_view(), name="agent_detail"),
    path("agents/<uuid:pk>/edit/", manage.AgentFormView.as_view(), name="agent_edit"),
    # IRGF agent chain link 06: the kill-switch.
    path("agents/<uuid:pk>/suspend/", actions.AgentSuspendView.as_view(), name="agent_suspend"),
    path("agents/<uuid:pk>/resume/", actions.AgentResumeView.as_view(), name="agent_resume"),
    path("models/", views.ModelListView.as_view(), name="models"),
    path("models/new/", manage.ModelFormView.as_view(), name="model_create"),
    path("models/<uuid:pk>/", views.ModelDetailView.as_view(), name="model_detail"),
    path("models/<uuid:pk>/edit/", manage.ModelFormView.as_view(), name="model_edit"),
    path("models/<uuid:pk>/approval/", manage.ModelApprovalView.as_view(), name="model_approval"),
    path("providers/", views.ProviderListView.as_view(), name="providers"),
    path("providers/new/", manage.ProviderFormView.as_view(), name="provider_create"),
    path("providers/<uuid:pk>/", views.ProviderDetailView.as_view(), name="provider_detail"),
    path("providers/<uuid:pk>/edit/", manage.ProviderFormView.as_view(), name="provider_edit"),
    path("providers/<uuid:pk>/health/", manage.ProviderHealthCheckView.as_view(), name="provider_health"),
    path(
        "providers/<uuid:pk>/capabilities/", manage.ProviderCapabilitiesView.as_view(),
        name="provider_capabilities",
    ),
    path("providers/<uuid:pk>/approval/", manage.ProviderApprovalView.as_view(), name="provider_approval"),
]
