"""Immutable audit trail (section 29)."""

from __future__ import annotations

import hashlib
import json

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import UUIDModel


class AuditAction(models.TextChoices):
    CREATE = "create", "Create"
    UPDATE = "update", "Update"
    DELETE = "delete", "Delete"
    APPROVE = "approve", "Approve"
    REJECT = "reject", "Reject"
    LOGIN = "login", "Login"
    LOGOUT = "logout", "Logout"
    ROLE_CHANGE = "role_change", "Role Change"
    POLICY_CHANGE = "policy_change", "Policy Change"
    CONFIG_CHANGE = "config_change", "Configuration Change"
    MODEL_APPROVAL = "model_approval", "Model Approval"
    HEALTH_CHECK = "health_check", "Health Check"
    TELEMETRY_INGEST = "telemetry_ingest", "Telemetry Ingest"


class AuditSource(models.TextChoices):
    UI = "ui", "Web UI"
    API = "api", "API"
    SYSTEM = "system", "System"
    ADMIN = "admin", "Django Admin"


class AuditLogError(RuntimeError):
    """Raised when something attempts to mutate the audit trail."""


class AuditLogQuerySet(models.QuerySet):
    """Closes the bulk routes around the per-instance immutability guard.

    `Model.delete()` is not called by `queryset.delete()`, and `update()`
    skips `save()` entirely -- so both have to be refused here as well.
    """

    def delete(self):
        raise AuditLogError("Audit log entries are immutable and cannot be deleted.")

    def update(self, **kwargs):
        raise AuditLogError("Audit log entries are immutable and cannot be modified.")


class AuditLog(UUIDModel):
    """Append-only record of a consequential action.

    Rows cannot be updated or deleted through the ORM, and each carries a
    content hash so a change made *around* the ORM -- straight against the
    database -- is still detectable. `actor_label` is denormalized so the trail
    stays readable after a user is removed.

    Deliberately not a TimeStampedModel: an audit entry is written once and
    never updated, so a single `created_at` we control (and can therefore
    include in the hash) is more truthful than an auto-managed pair.
    """

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    entry_hash = models.CharField(
        max_length=64,
        blank=True,
        help_text="SHA-256 of the entry's content, set once at write time.",
    )

    organization = models.ForeignKey(
        "organizations.Organization",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    actor_label = models.CharField(max_length=255, blank=True)
    source = models.CharField(max_length=20, choices=AuditSource.choices, default=AuditSource.SYSTEM)

    action = models.CharField(max_length=64, db_index=True)
    resource_type = models.CharField(max_length=100, db_index=True)
    resource_id = models.CharField(max_length=100, blank=True, db_index=True)
    resource_label = models.CharField(max_length=255, blank=True)

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    old_value = models.JSONField(null=True, blank=True)
    new_value = models.JSONField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Audit log entry"
        verbose_name_plural = "Audit log"
        indexes = [
            models.Index(fields=["organization", "-created_at"]),
            models.Index(fields=["resource_type", "resource_id"]),
            models.Index(fields=["actor", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.actor_label or 'system'} {self.action} {self.resource_type}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AuditLogError("Audit log entries are immutable and cannot be modified.")
        if not self.entry_hash:
            self.entry_hash = self.compute_hash()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditLogError("Audit log entries are immutable and cannot be deleted.")

    # -- Tamper evidence ----------------------------------------------------

    def canonical_content(self) -> str:
        """Stable serialization of everything the entry asserts.

        Field order is fixed and dictionaries are key-sorted, so the same entry
        always hashes to the same value regardless of how it was loaded.
        """
        return json.dumps(
            {
                "id": str(self.id),
                "created_at": self.created_at.isoformat() if self.created_at else "",
                "organization": str(self.organization_id or ""),
                "actor": str(self.actor_id or ""),
                "actor_label": self.actor_label,
                "source": self.source,
                "action": self.action,
                "resource_type": self.resource_type,
                "resource_id": self.resource_id,
                "resource_label": self.resource_label,
                "ip_address": self.ip_address or "",
                "user_agent": self.user_agent,
                "old_value": self.old_value,
                "new_value": self.new_value,
                "metadata": self.metadata,
            },
            sort_keys=True,
            default=str,
        )

    def compute_hash(self) -> str:
        return hashlib.sha256(self.canonical_content().encode("utf-8")).hexdigest()

    @property
    def is_verifiable(self) -> bool:
        """False for entries written before hashing existed."""
        return bool(self.entry_hash)

    def verify(self) -> bool:
        """True when the stored content still matches its hash."""
        if not self.is_verifiable:
            return True
        return self.entry_hash == self.compute_hash()
