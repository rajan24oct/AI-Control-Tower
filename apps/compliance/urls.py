from django.urls import path

from apps.compliance import views

app_name = "compliance"

urlpatterns = [
    path("", views.OverviewView.as_view(), name="overview"),
    path("<str:kind>/<uuid:pk>/", views.SystemView.as_view(), name="system"),
    path("<str:kind>/<uuid:pk>/scope/", views.ScopeView.as_view(), name="scope"),
    path("<str:kind>/<uuid:pk>/attest/", views.AttestView.as_view(), name="attest"),
    path("<str:kind>/<uuid:pk>/pack/", views.PackView.as_view(), name="pack"),
    path("<str:kind>/<uuid:pk>/pack.zip", views.PackView.as_view(), {"fmt": "zip"}, name="pack_zip"),
]
