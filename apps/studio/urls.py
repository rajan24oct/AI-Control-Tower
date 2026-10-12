from django.urls import path

from apps.studio import views

app_name = "studio"

urlpatterns = [
    path("", views.RequestListView.as_view(), name="list"),
    path("new/", views.RequestFormView.as_view(), name="create"),
    path("<uuid:pk>/", views.RequestDetailView.as_view(), name="detail"),
    path("<uuid:pk>/edit/", views.RequestFormView.as_view(), name="edit"),
    path("<uuid:pk>/generate/", views.GenerateView.as_view(), name="generate"),
    path("<uuid:pk>/propose/", views.ProposeView.as_view(), name="propose"),
    path("from-use-case/<uuid:pk>/", views.FromUseCaseView.as_view(), name="from_use_case"),
    path("blueprints/<uuid:pk>/sandbox/", views.OpenSandboxView.as_view(), name="open_sandbox"),
    path("sandbox/", views.SessionListView.as_view(), name="sandbox"),
    path("sandbox/new/", views.SessionCreateView.as_view(), name="session_create"),
    path("sandbox/<uuid:pk>/", views.SessionView.as_view(), name="session"),
    path("sandbox/<uuid:pk>/run/", views.RunView.as_view(), name="run"),
    path("sandbox/runs/<uuid:pk>/rate/", views.RateView.as_view(), name="rate"),
]
