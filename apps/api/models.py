"""Service identities for external AI execution engines (section 30).

Every engine that reports telemetry authenticates as a service account, so the
Control Tower always knows which external system submitted what.
"""

from __future__ import annotations

import hashlib
import secrets

from django.db import models
from django.utils import timezone

from apps.accounts.roles import Role, role_has_permission
from apps.core.models import BaseModel, OrganizationScopedModel

KEY_PREFIX_LENGTH = 12
KEY_SECRET_BYTES = 32


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class ServiceAccount(OrganizationScopedModel):
    """A non-human identity belonging to one organization.

    Duck-types the parts of the user interface that permission checks rely on,
    so views and permissions treat humans and machines uniformly.
    """

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    external_system = models.CharField(
        max_length=200, blank=True, help_text="The engine or platform this identity represents."
    )
    role = models.CharField(max_length=40, choices=Role.choices, default=Role.AI_DEVELOPER)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"], name="uniq_service_account_name_per_org"
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} (service account)"

    # -- User-compatible surface for permission checks ---------------------

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_anonymous(self) -> bool:
        return False

    is_superuser = False
    is_staff = False

    @property
    def is_service_account(self) -> bool:
        return True

    @property
    def organizations(self):
        from apps.organizations.models import Organization

        return Organization.objects.filter(pk=self.organization_id)

    @property
    def default_organization(self):
        return self.organization

    def role_in(self, organization) -> str | None:
        if organization is None or str(organization.pk) != str(self.organization_id):
            return None
        return self.role

    def has_org_permission(self, organization, permission: str) -> bool:
        role = self.role_in(organization)
        return bool(role) and role_has_permission(role, permission)


class APIKey(BaseModel):
    """A credential for a service account.

    Only a SHA-256 hash is stored. The raw key is shown once, at creation, and
    is not recoverable afterwards.
    """

    service_account = models.ForeignKey(
        ServiceAccount, on_delete=models.CASCADE, related_name="api_keys"
    )
    name = models.CharField(max_length=200, default="default")
    prefix = models.CharField(max_length=KEY_PREFIX_LENGTH, unique=True, db_index=True)
    key_hash = models.CharField(max_length=64)

    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "API key"

    def __str__(self) -> str:
        return f"{self.service_account.name}:{self.prefix}"

    @classmethod
    def generate(cls, service_account: ServiceAccount, name: str = "default", **kwargs):
        """Create a key. Returns `(api_key, raw_key)`; the raw key is shown once."""
        prefix = secrets.token_hex(KEY_PREFIX_LENGTH // 2)
        secret = secrets.token_urlsafe(KEY_SECRET_BYTES)
        raw_key = f"act_{prefix}_{secret}"

        api_key = cls.objects.create(
            service_account=service_account,
            name=name,
            prefix=prefix,
            key_hash=hash_secret(secret),
            **kwargs,
        )
        return api_key, raw_key

    @staticmethod
    def split_raw(raw_key: str) -> tuple[str, str] | None:
        """Split `act_<prefix>_<secret>` into its parts."""
        parts = (raw_key or "").split("_", 2)
        if len(parts) != 3 or parts[0] != "act" or not parts[1] or not parts[2]:
            return None
        return parts[1], parts[2]

    @property
    def is_expired(self) -> bool:
        return bool(self.expires_at and self.expires_at <= timezone.now())

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_usable(self) -> bool:
        return not self.is_revoked and not self.is_expired

    def verify(self, secret: str) -> bool:
        return secrets.compare_digest(self.key_hash, hash_secret(secret))

    def revoke(self) -> None:
        self.revoked_at = timezone.now()
        self.save(update_fields=["revoked_at", "updated_at"])
