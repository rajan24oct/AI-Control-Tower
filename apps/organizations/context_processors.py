"""Template context shared by every Control Tower page."""

from __future__ import annotations

from apps.accounts.roles import Role, role_permissions


def organization_context(request):
    user = getattr(request, "user", None)
    organization = getattr(request, "organization", None)

    if not user or not user.is_authenticated:
        return {
            "active_organization": None, "available_organizations": [], "active_role": None,
            "org_perms": frozenset(),
        }

    role = user.role_in(organization) if organization else None
    if organization is None:
        permissions = frozenset()
    elif user.is_superuser:
        permissions = frozenset(role_permissions(Role.SUPER_ADMIN))
    else:
        permissions = frozenset(role_permissions(role)) if role else frozenset()

    return {
        "active_organization": organization,
        "available_organizations": user.organizations,
        "active_role": role,
        # What the user may do here, so pages show only the buttons that work:
        # {% if "agent.manage" in org_perms %}. Views still check every action.
        "org_perms": permissions,
    }
