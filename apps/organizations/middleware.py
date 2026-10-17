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


class ModuleGateMiddleware:
    """Answer a switched-off module's pages with a short "not enabled" page.

    Runs after the active organization is known. The module's data and its API
    are left alone: switching a module off tidies the product, it does not cut
    agents off from a gateway they already call.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        from django.shortcuts import render

        from apps.organizations.modules import is_enabled, module_for_app

        organization = getattr(request, "organization", None)
        match = getattr(request, "resolver_match", None)
        if organization is None or match is None:
            return None
        module = module_for_app(match.app_name)
        if module is None or is_enabled(organization, module.key):
            return None
        return render(
            request,
            "organizations/module_disabled.html",
            {"module": module, "page_title": module.label, "page_subtitle": "Not enabled for this organization"},
            status=404,
        )
