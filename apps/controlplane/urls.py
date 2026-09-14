from django.urls import path

from apps.controlplane import views

app_name = "controlplane"

urlpatterns = [
    path("", views.OverviewView.as_view(), name="overview"),
    path("run/", views.RunView.as_view(), name="run"),
    path("drift/", views.DriftListView.as_view(), name="drift"),
    path("drift/<uuid:pk>/", views.DriftDetailView.as_view(), name="drift_detail"),
    path("drift/<uuid:pk>/resolve/", views.DriftResolveView.as_view(), name="drift_resolve"),
    path("verifications/", views.VerificationListView.as_view(), name="verifications"),
]
