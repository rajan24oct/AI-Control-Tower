"""Organization-aware DRF permissions."""

from __future__ import annotations

from rest_framework import permissions


class IsAuthenticatedOrganizationMember(permissions.BasePermission):
    """Authenticated and attached to at least one organization."""

    message = "You must belong to an organization to use this API."

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if getattr(user, "is_superuser", False):
            return True
        return user.organizations.exists()

    def has_object_permission(self, request, view, obj) -> bool:
        user = request.user
        if getattr(user, "is_superuser", False):
            return True

        organization = getattr(obj, "organization", None)
        if organization is None:
            return True
        return user.organizations.filter(pk=organization.pk).exists()


class HasOrganizationPermission(permissions.BasePermission):
    """Checks a Control Tower capability against the caller's role.

    Views declare `required_permission` (and optionally
    `required_write_permission` for unsafe methods).
    """

    message = "Your role does not grant this action."

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if getattr(user, "is_superuser", False):
            return True

        required = self._required(request, view)
        if not required:
            return True

        organization = getattr(request, "organization", None) or user.default_organization
        if organization is None:
            return False
        return user.has_org_permission(organization, required)

    def has_object_permission(self, request, view, obj) -> bool:
        user = request.user
        if getattr(user, "is_superuser", False):
            return True

        required = self._required(request, view)
        organization = getattr(obj, "organization", None)
        if organization is None or not required:
            return True
        return user.has_org_permission(organization, required)

    @staticmethod
    def _required(request, view) -> str | None:
        if request.method not in permissions.SAFE_METHODS:
            write_permission = getattr(view, "required_write_permission", None)
            if write_permission:
                return write_permission
        return getattr(view, "required_permission", None)
