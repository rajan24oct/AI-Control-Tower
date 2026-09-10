"""Resolves the organization every request operates within."""

from __future__ import annotations

SESSION_KEY = "active_organization_id"


class ActiveOrganizationMiddleware:
    """Attach `request.organization` for the duration of the request.

    Views and querysets read this instead of trusting a parameter from the
    client, so a user cannot reach another tenant's data by editing a URL.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.organization = self._resolve(request)
        return self.get_response(request)

    def _resolve(self, request):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return None

        requested_id = request.session.get(SESSION_KEY)
        if requested_id:
            # Re-validate membership on every request; session state alone is
            # not proof of access.
            organization = user.organizations.filter(pk=requested_id).first()
            if organization:
                return organization
            del request.session[SESSION_KEY]

        organization = user.default_organization
        if organization:
            request.session[SESSION_KEY] = str(organization.pk)
        return organization
