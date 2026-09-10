"""Template context shared by every Control Tower page."""

from __future__ import annotations


def organization_context(request):
    user = getattr(request, "user", None)
    organization = getattr(request, "organization", None)

    if not user or not user.is_authenticated:
        return {"active_organization": None, "available_organizations": [], "active_role": None}

    return {
        "active_organization": organization,
        "available_organizations": user.organizations,
        "active_role": user.role_in(organization) if organization else None,
    }
