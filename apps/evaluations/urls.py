from django.urls import path

from apps.evaluations import views

app_name = "evaluations"

urlpatterns = [
    path("", views.EvaluationHomeView.as_view(), name="home"),
    path("suites/<uuid:pk>/", views.SuiteDetailView.as_view(), name="suite"),
    path("suites/<uuid:pk>/run/", views.StartRunView.as_view(), name="start_run"),
    path("runs/", views.RunListView.as_view(), name="runs"),
    path("runs/<uuid:pk>/", views.RunDetailView.as_view(), name="run"),
    path("runs/<uuid:pk>/progress/", views.RunProgressView.as_view(), name="run_progress"),
]
