from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.views.generic import ListView, TemplateView, View

from apps.core.responses import action_response
from apps.notifications.models import (
    DEFAULT_CHANNELS,
    Channel,
    Notification,
    NotificationCategory,
)
from apps.notifications.services import NotificationService


class NotificationListView(LoginRequiredMixin, ListView):
    """The user's own inbox. Never shows anyone else's notifications."""

    model = Notification
    template_name = "notifications/list.html"
    context_object_name = "notifications"
    paginate_by = 50
    extra_context = {
        "page_title": "Notifications",
        "page_subtitle": "What the Control Tower needs you to know",
    }

    def get_queryset(self):
        queryset = Notification.objects.for_recipient(self.request.user).select_related(
            "organization"
        )
        if self.request.organization is not None:
            queryset = queryset.filter(organization=self.request.organization)

        category = self.request.GET.get("category", "").strip()
        if category:
            queryset = queryset.filter(category=category)
        if self.request.GET.get("unread") == "1":
            queryset = queryset.unread()

        return queryset.order_by("-created_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "categories": NotificationCategory.choices,
                "active_category": self.request.GET.get("category", ""),
                "unread_only": self.request.GET.get("unread") == "1",
                "unread_count": NotificationService.unread_count(
                    self.request.user, self.request.organization
                ),
            }
        )
        return context


class NotificationBadgeView(LoginRequiredMixin, TemplateView):
    """HTMX polling target for the topbar bell."""

    template_name = "notifications/partials/_bell.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization

        queryset = Notification.objects.for_recipient(self.request.user)
        if organization is not None:
            queryset = queryset.filter(organization=organization)

        context["unread_count"] = queryset.unread().count()
        context["recent"] = queryset.order_by("-created_at")[:8]
        return context


class MarkReadView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        notification_id = kwargs.get("pk")
        redirect_to = request.POST.get("next") or reverse("notifications:list")

        if notification_id:
            count = NotificationService.mark_read(request.user, [notification_id])
            message = "Marked as read." if count else "That notification was already read."
        else:
            count = NotificationService.mark_all_read(request.user, request.organization)
            message = f"Marked {count} notification{'' if count == 1 else 's'} as read."

        return action_response(
            request,
            message=message,
            tone="success" if count else "warning",
            redirect_to=redirect_to,
            payload={"unread": NotificationService.unread_count(
                request.user, request.organization
            )},
        )


class NotificationPreferencesView(LoginRequiredMixin, TemplateView):
    template_name = "notifications/preferences.html"
    extra_context = {
        "page_title": "Notification Settings",
        "page_subtitle": "Choose what reaches you, and how",
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization
        if organization is None:
            return context

        preference = NotificationService.preference_for(self.request.user, organization)
        context["rows"] = [
            {
                "category": category,
                "label": label,
                "in_app": preference.wants(category, Channel.IN_APP),
                "email": preference.wants(category, Channel.EMAIL),
                "default_email": DEFAULT_CHANNELS.get(category, {}).get("email", False),
            }
            for category, label in NotificationCategory.choices
        ]
        return context

    def post(self, request, *args, **kwargs):
        organization = request.organization
        if organization is None:
            return HttpResponseRedirect(reverse("notifications:preferences"))

        preference = NotificationService.preference_for(request.user, organization)
        for category, _ in NotificationCategory.choices:
            for channel in (Channel.IN_APP, Channel.EMAIL):
                preference.set_channel(
                    category, channel, f"{category}:{channel}" in request.POST
                )
        preference.save(update_fields=["settings", "updated_at"])

        messages.success(request, "Notification settings saved.")
        return HttpResponseRedirect(reverse("notifications:preferences"))
