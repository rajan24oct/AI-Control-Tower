from django.urls import path

from apps.audit import views

app_name = "audit"

urlpatterns = [
    path("", views.AuditLogListView.as_view(), name="list"),
    path("export/", views.AuditExportView.as_view(), name="export"),
    path("<uuid:pk>/", views.AuditLogDetailView.as_view(), name="detail"),
]
