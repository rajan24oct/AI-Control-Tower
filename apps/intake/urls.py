from django.urls import path

from apps.intake import views

app_name = "intake"

urlpatterns = [
    path("", views.UseCaseListView.as_view(), name="list"),
    path("new/", views.UseCaseFormView.as_view(), name="create"),
    path("<uuid:pk>/", views.UseCaseDetailView.as_view(), name="detail"),
    path("<uuid:pk>/edit/", views.UseCaseFormView.as_view(), name="edit"),
    path("<uuid:pk>/submit/", views.SubmitView.as_view(), name="submit"),
    path("<uuid:pk>/decide/", views.DecideView.as_view(), name="decide"),
    path("<uuid:pk>/register/", views.RegisterView.as_view(), name="register"),
    path("<uuid:pk>/withdraw/", views.WithdrawView.as_view(), name="withdraw"),
    path("<uuid:pk>/conditions/<int:index>/met/", views.ConditionMetView.as_view(), name="condition_met"),
]
