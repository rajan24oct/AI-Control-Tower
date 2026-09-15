from django.urls import path

from apps.routing import manage, views

app_name = "routing"

urlpatterns = [
    path("", views.RoutingPolicyListView.as_view(), name="list"),
    path("new/", manage.RoutingPolicyFormView.as_view(), name="create"),
    path("simulator/", views.RoutingSimulatorView.as_view(), name="simulator"),
    path("decisions/", views.RoutingDecisionListView.as_view(), name="decisions"),
    path("<uuid:pk>/", views.RoutingPolicyDetailView.as_view(), name="detail"),
    path("<uuid:pk>/edit/", manage.RoutingPolicyFormView.as_view(), name="edit"),
]
