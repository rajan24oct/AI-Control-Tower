from django.urls import path

from apps.executions import views

app_name = "executions"

urlpatterns = [
    path("", views.ExecutionListView.as_view(), name="list"),
    path("live/", views.LiveMonitorView.as_view(), name="live"),
    path("live/table/", views.LiveMonitorTableView.as_view(), name="live_table"),
    path("<uuid:pk>/", views.ExecutionDetailView.as_view(), name="detail"),
]
