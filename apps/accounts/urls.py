from django.contrib.auth import views as auth_views
from django.urls import path

from apps.accounts.views import SetPasswordView

app_name = "accounts"

urlpatterns = [
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="registration/login.html", redirect_authenticated_user=True
        ),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    # Invitations and administrator-sent resets both land here.
    path("set-password/<uidb64>/<token>/", SetPasswordView.as_view(), name="set_password"),
]
