from django.urls import path

from apps.policies import manage, views

app_name = "policies"

urlpatterns = [
    path("", views.PolicyListView.as_view(), name="list"),
    path("new/", manage.PolicyFormView.as_view(), name="create"),
    path("<uuid:pk>/", views.PolicyDetailView.as_view(), name="detail"),
    path("<uuid:pk>/edit/", manage.PolicyFormView.as_view(), name="edit"),
    path(
        "violations/<uuid:pk>/acknowledge/",
        views.AcknowledgeViolationView.as_view(),
        name="acknowledge_violation",
    ),
]
