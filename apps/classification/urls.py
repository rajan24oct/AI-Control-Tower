from django.urls import path

from apps.classification import views

app_name = "classification"

urlpatterns = [
    path("", views.ClassificationRegisterView.as_view(), name="register"),
    path("<uuid:pk>/", views.ClassificationDetailView.as_view(), name="detail"),
    path("<uuid:pk>/countersign/", views.CountersignView.as_view(), name="countersign"),
    path("<str:kind>/<uuid:pk>/score/", views.ScoreView.as_view(), name="score"),
]
