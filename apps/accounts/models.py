"""Custom user model for the Control Tower."""

from __future__ import annotations

import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone

from apps.accounts.roles import Role, role_has_permission


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra):
        if not email:
            raise ValueError("Users must have an email address")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        if extra.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True")
        if extra.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True")
        return self._create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True, db_index=True)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    job_title = models.CharField(max_length=150, blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        ordering = ["email"]

    def __str__(self) -> str:
        return self.get_full_name() or self.email

    def get_full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def get_short_name(self) -> str:
        return self.first_name or self.email.split("@")[0]

    @property
    def initials(self) -> str:
        parts = [p for p in (self.first_name, self.last_name) if p]
        if parts:
            return "".join(p[0].upper() for p in parts)[:2]
        return self.email[:2].upper()

    # -- Organization-scoped RBAC ------------------------------------------

    def membership_for(self, organization):
        """Return this user's membership in `organization`, or None."""
        if organization is None:
            return None
        return self.organization_memberships.filter(organization=organization).first()

    def role_in(self, organization) -> str | None:
        membership = self.membership_for(organization)
        return membership.role if membership else None

    def has_org_permission(self, organization, permission: str) -> bool:
        """Authoritative permission check for an organization-scoped action."""
        if self.is_superuser:
            return True
        role = self.role_in(organization)
        if role is None:
            return False
        return role_has_permission(role, permission)

    def is_org_admin(self, organization) -> bool:
        return self.is_superuser or self.role_in(organization) in {
            Role.SUPER_ADMIN,
            Role.ORG_ADMIN,
        }

    @property
    def organizations(self):
        from apps.organizations.models import Organization

        if self.is_superuser:
            return Organization.objects.all()
        return Organization.objects.filter(memberships__user=self).distinct()

    @property
    def default_organization(self):
        membership = self.organization_memberships.order_by("-is_default", "created_at").first()
        if membership:
            return membership.organization
        return self.organizations.first()
