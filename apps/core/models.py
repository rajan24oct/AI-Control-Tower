"""Abstract base models used by every Control Tower business entity."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from apps.core.constants import LifecycleStatus
from apps.core.managers import OrganizationManager


class UUIDModel(models.Model):
    """UUID primary keys for business entities (section 26)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ActorStampedModel(models.Model):
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="%(app_label)s_%(class)s_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="%(app_label)s_%(class)s_updated",
    )

    class Meta:
        abstract = True


class BaseModel(UUIDModel, TimeStampedModel, ActorStampedModel):
    """id / created_at / updated_at / created_by / updated_by."""

    class Meta:
        abstract = True


class OrganizationOwnedModel(BaseModel):
    """A record that belongs to exactly one organization.

    Multi-tenancy is enforced in the data model rather than bolted on at the
    view layer. Records with their own status vocabulary (executions, for
    example) extend this directly; registry assets extend
    `OrganizationScopedModel` below and inherit a lifecycle status.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
    )

    objects = OrganizationManager()

    class Meta:
        abstract = True


class OrganizationScopedModel(OrganizationOwnedModel):
    """An org-owned asset that moves through a draft/active/retired lifecycle."""

    status = models.CharField(
        max_length=32, choices=LifecycleStatus.choices, default=LifecycleStatus.ACTIVE, db_index=True
    )

    class Meta:
        abstract = True

    @property
    def is_active(self) -> bool:
        return self.status == LifecycleStatus.ACTIVE
