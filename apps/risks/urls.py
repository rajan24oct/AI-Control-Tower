from django.urls import path

from apps.risks import views

app_name = "risks"

urlpatterns = [
    path("", views.RiskListView.as_view(), name="list"),
    path("matrix/", views.RiskMatrixView.as_view(), name="matrix"),
    path("<uuid:pk>/", views.RiskDetailView.as_view(), name="detail"),
]
