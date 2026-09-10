from django.urls import path

from apps.routing import views

app_name = "routing"

urlpatterns = [
    path("", views.RoutingPolicyListView.as_view(), name="list"),
    path("simulator/", views.RoutingSimulatorView.as_view(), name="simulator"),
    path("decisions/", views.RoutingDecisionListView.as_view(), name="decisions"),
    path("<uuid:pk>/", views.RoutingPolicyDetailView.as_view(), name="detail"),
]
