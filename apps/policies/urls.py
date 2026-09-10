from django.urls import path

from apps.policies import views

app_name = "policies"

urlpatterns = [
    path("", views.PolicyListView.as_view(), name="list"),
    path("<uuid:pk>/", views.PolicyDetailView.as_view(), name="detail"),
    path(
        "violations/<uuid:pk>/acknowledge/",
        views.AcknowledgeViolationView.as_view(),
        name="acknowledge_violation",
    ),
]
