"""Notification routing and delivery (section 23)."""

from __future__ import annotations

import logging
from typing import Iterable

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.notifications.channels import CHANNELS
from apps.notifications.models import (
    CATEGORY_PERMISSIONS,
    Channel,
    Notification,
    NotificationCategory,
    NotificationPreference,
    NotificationSeverity,
)

logger = logging.getLogger(__name__)
User = get_user_model()


class NotificationService:
    """Creates notifications, routes them to the right people, delivers them."""

    # -- Routing ------------------------------------------------------------

    @staticmethod
    def recipients_for(organization, category: str) -> list:
        """Everyone whose role grants the capability this category maps to.

        Routing by permission rather than by role means a new role that gains
        `approval.decide` starts receiving approval notifications without any
        change here.
        """
        if organization is None:
            return []

        permission = CATEGORY_PERMISSIONS.get(category)
        if permission is None:
            return []

        members = User.objects.filter(
            organization_memberships__organization=organization, is_active=True
        ).distinct()
        return [user for user in members if user.has_org_permission(organization, permission)]

    @staticmethod
    def preference_for(user, organization) -> NotificationPreference:
        preference, _ = NotificationPreference.objects.get_or_create(
            user=user, organization=organization
        )
        return preference

    # -- Raising ------------------------------------------------------------

    @classmethod
    def notify(
        cls,
        *,
        organization,
        category: str,
        title: str,
        body: str = "",
        url: str = "",
        severity: str = NotificationSeverity.INFO,
        recipients: Iterable | None = None,
        dedupe_key: str = "",
        resource_type: str = "",
        resource_id: str = "",
    ) -> list[Notification]:
        """Raise a notification for the routed (or explicitly given) recipients.

        Never raises. A notification failing must not break whatever produced
        it -- the same rule the audit trail follows.
        """
        try:
            return cls._notify(
                organization=organization,
                category=category,
                title=title,
                body=body,
                url=url,
                severity=severity,
                recipients=recipients,
                dedupe_key=dedupe_key,
                resource_type=resource_type,
                resource_id=resource_id,
            )
        except Exception:
            logger.exception("Failed to raise %s notification: %s", category, title)
            return []

    @classmethod
    def _notify(
        cls, *, organization, category, title, body, url, severity,
        recipients, dedupe_key, resource_type, resource_id,
    ) -> list[Notification]:
        targets = list(recipients) if recipients is not None else cls.recipients_for(
            organization, category
        )
        targets = [user for user in targets if user is not None and getattr(user, "pk", None)]
        if not targets:
            return []

        created: list[Notification] = []
        for user in {user.pk: user for user in targets}.values():
            preference = cls.preference_for(user, organization)
            if not preference.wants(category, Channel.IN_APP):
                continue

            try:
                with transaction.atomic():
                    notification = Notification.objects.create(
                        organization=organization,
                        recipient=user,
                        category=category,
                        severity=severity,
                        title=title[:255],
                        body=body,
                        url=url[:500],
                        dedupe_key=dedupe_key[:255],
                        resource_type=resource_type[:100],
                        resource_id=str(resource_id)[:100],
                    )
            except IntegrityError:
                # Already notified about this exact thing.
                continue

            cls._deliver(notification, preference)
            created.append(notification)

        return created

    @staticmethod
    def _deliver(notification: Notification, preference: NotificationPreference) -> None:
        delivered = []
        for key, channel in CHANNELS.items():
            if not preference.wants(notification.category, key):
                continue
            if channel.send(notification):
                delivered.append(key)

        if delivered:
            notification.delivered_channels = delivered
            notification.save(update_fields=["delivered_channels", "updated_at"])

    # -- Reading ------------------------------------------------------------

    @staticmethod
    def unread_count(user, organization=None) -> int:
        queryset = Notification.objects.for_recipient(user).unread()
        if organization is not None:
            queryset = queryset.filter(organization=organization)
        return queryset.count()

    @staticmethod
    def mark_read(user, notification_ids: Iterable) -> int:
        return (
            Notification.objects.for_recipient(user)
            .unread()
            .filter(pk__in=list(notification_ids))
            .update(read_at=timezone.now())
        )

    @staticmethod
    def mark_all_read(user, organization=None) -> int:
        queryset = Notification.objects.for_recipient(user).unread()
        if organization is not None:
            queryset = queryset.filter(organization=organization)
        return queryset.update(read_at=timezone.now())
