from django.urls import path

from apps.inventory import actions, views

app_name = "inventory"

urlpatterns = [
    path("", views.InventoryIndexView.as_view(), name="index"),
    path("applications/", views.ApplicationListView.as_view(), name="applications"),
    path("applications/<uuid:pk>/", views.ApplicationDetailView.as_view(), name="application_detail"),
    path("agents/", views.AgentListView.as_view(), name="agents"),
    path("agents/<uuid:pk>/", views.AgentDetailView.as_view(), name="agent_detail"),
    # IRGF agent chain link 06: the kill-switch.
    path("agents/<uuid:pk>/suspend/", actions.AgentSuspendView.as_view(), name="agent_suspend"),
    path("agents/<uuid:pk>/resume/", actions.AgentResumeView.as_view(), name="agent_resume"),
    path("models/", views.ModelListView.as_view(), name="models"),
    path("models/<uuid:pk>/", views.ModelDetailView.as_view(), name="model_detail"),
    path("providers/", views.ProviderListView.as_view(), name="providers"),
    path("providers/<uuid:pk>/", views.ProviderDetailView.as_view(), name="provider_detail"),
]
