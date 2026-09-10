"""Delivery channels.

Kept behind a small interface so Slack, Teams or a webhook can be added later
without touching anything that raises a notification -- the same adapter
principle the rest of the platform uses for AI providers.
"""

from __future__ import annotations

import abc
import logging

from django.conf import settings
from django.core.mail import send_mail

from apps.notifications.models import Channel, Notification

logger = logging.getLogger(__name__)


class BaseNotificationChannel(abc.ABC):
    key: str = ""

    @abc.abstractmethod
    def send(self, notification: Notification) -> bool:
        """Deliver the notification. Return True when it actually went out."""


class InAppChannel(BaseNotificationChannel):
    """The stored row is the delivery, so there is nothing more to do."""

    key = Channel.IN_APP

    def send(self, notification: Notification) -> bool:
        return True


class EmailChannel(BaseNotificationChannel):
    key = Channel.EMAIL

    def send(self, notification: Notification) -> bool:
        address = getattr(notification.recipient, "email", "")
        if not address:
            return False

        base = getattr(settings, "SITE_URL", "").rstrip("/")
        link = f"{base}{notification.url}" if base and notification.url else notification.url
        body = notification.body or notification.title
        if link:
            body = f"{body}\n\nOpen in the Control Tower: {link}"

        try:
            send_mail(
                subject=f"[AI Control Tower] {notification.title}",
                message=body,
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "ai-control-tower@localhost"),
                recipient_list=[address],
                fail_silently=False,
            )
        except Exception:
            # Delivery failure must never take down the operation that raised
            # the notification; the in-app row still stands.
            logger.exception("Could not email notification %s", notification.pk)
            return False
        return True


CHANNELS: dict[str, BaseNotificationChannel] = {
    Channel.IN_APP: InAppChannel(),
    Channel.EMAIL: EmailChannel(),
}
