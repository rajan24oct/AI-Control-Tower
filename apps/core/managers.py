"""Query managers that make organization isolation the default, not an option."""

from __future__ import annotations

from django.db import models


class OrganizationQuerySet(models.QuerySet):
    def for_organization(self, organization) -> "OrganizationQuerySet":
        """Restrict to a single organization.

        Accepts an Organization instance or a primary key.
        """
        if organization is None:
            return self.none()
        return self.filter(organization=organization)

    def for_user(self, user) -> "OrganizationQuerySet":
        """Restrict to every organization the identity may see.

        Accepts any authenticated identity exposing an `organizations`
        queryset -- a human User or a ServiceAccount -- so machine callers are
        confined exactly like people are. Superusers see everything; anonymous
        callers see nothing. This is the backend enforcement required by
        section 27; UI filtering alone is never sufficient.
        """
        if not user or not user.is_authenticated:
            return self.none()
        if getattr(user, "is_superuser", False):
            return self
        return self.filter(organization__in=user.organizations)

    def active(self) -> "OrganizationQuerySet":
        return self.filter(status="active")


class OrganizationManager(models.Manager.from_queryset(OrganizationQuerySet)):
    """Default manager for organization-scoped models."""
