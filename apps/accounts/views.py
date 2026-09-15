"""Setting a password from an invitation or a reset link."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.urls import reverse_lazy


class SetPasswordView(auth_views.PasswordResetConfirmView):
    """The page an invitation or reset email links to.

    Django's reset confirmation, so the link is single-use, signed against the
    account's current password and last sign-in, and expires after
    PASSWORD_RESET_TIMEOUT (three days by default).
    """

    template_name = "registration/set_password.html"
    success_url = reverse_lazy("accounts:login")
    post_reset_login = False

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, "Your password is set. Sign in with your work email and the new password.")
        return response
