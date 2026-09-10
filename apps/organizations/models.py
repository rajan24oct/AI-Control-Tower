"""Organization, Team and membership models (sections 27 & 28)."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.text import slugify

from apps.accounts.roles import Role
from apps.core.constants import LifecycleStatus
from apps.core.models import BaseModel


class Organization(BaseModel):
    """The tenant boundary. Every major AI asset belongs to one organization."""

    name = models.CharField(max_length=200, unique=True)
    slug = models.SlugField(max_length=200, unique=True, blank=True)
    description = models.TextField(blank=True)
    industry = models.CharField(max_length=120, blank=True)
    status = models.CharField(
        max_length=32, choices=LifecycleStatus.choices, default=LifecycleStatus.ACTIVE
    )
    settings = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:200]
        super().save(*args, **kwargs)

    def members(self):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.filter(organization_memberships__organization=self)


class Team(BaseModel):
    """A group inside an organization that owns AI assets."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="teams"
    )
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    department = models.CharField(max_length=150, blank=True)
    lead = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="led_teams",
    )
    status = models.CharField(
        max_length=32, choices=LifecycleStatus.choices, default=LifecycleStatus.ACTIVE
    )

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "slug"], name="uniq_team_slug_per_org")
        ]
        indexes = [models.Index(fields=["organization", "status"])]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:200]
        super().save(*args, **kwargs)


class OrganizationMembership(BaseModel):
    """Binds a user to an organization with exactly one role.

    A user may belong to several organizations with a different role in each.
    """

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organization_memberships",
    )
    role = models.CharField(max_length=40, choices=Role.choices, default=Role.VIEWER)
    is_default = models.BooleanField(
        default=False, help_text="Organization this user lands in after signing in."
    )

    class Meta:
        ordering = ["organization__name", "user__email"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"], name="uniq_membership_per_org_user"
            )
        ]
        indexes = [models.Index(fields=["user", "role"])]

    def __str__(self) -> str:
        return f"{self.user} @ {self.organization} ({self.get_role_display()})"


class TeamMembership(BaseModel):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="team_memberships"
    )

    class Meta:
        ordering = ["team__name", "user__email"]
        constraints = [
            models.UniqueConstraint(fields=["team", "user"], name="uniq_membership_per_team_user")
        ]

    def __str__(self) -> str:
        return f"{self.user} in {self.team}"
