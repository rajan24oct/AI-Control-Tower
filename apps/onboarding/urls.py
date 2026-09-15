from django.urls import path

from apps.onboarding import views

app_name = "onboarding"

urlpatterns = [
    path("", views.OnboardingHubView.as_view(), name="hub"),
]
