from django.urls import path

from apps.dashboard import views

app_name = "dashboard"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="home"),
    path("control-tower/", views.ControlTowerView.as_view(), name="control_tower"),
    path("switch-organization/", views.SwitchOrganizationView.as_view(), name="switch_organization"),
    path("docs/integration/", views.IntegrationHandbookView.as_view(), name="integration_handbook"),
]
