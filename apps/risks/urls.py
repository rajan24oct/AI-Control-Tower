from django.urls import path

from apps.risks import manage, views

app_name = "risks"

urlpatterns = [
    path("", views.RiskListView.as_view(), name="list"),
    path("new/", manage.RiskFormView.as_view(), name="create"),
    path("matrix/", views.RiskMatrixView.as_view(), name="matrix"),
    path("<uuid:pk>/", views.RiskDetailView.as_view(), name="detail"),
    path("<uuid:pk>/edit/", manage.RiskFormView.as_view(), name="edit"),
]
