"""Audit trail views (sections 29 and 31)."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.utils import timezone
from django.views.generic import DetailView, ListView, View

from apps.accounts.roles import Perm
from apps.audit.models import AuditLog, AuditSource
from apps.audit.services import diff

EXPORT_LIMIT = 10_000


class AuditAccessMixin(LoginRequiredMixin):
    """Only roles granted `audit.view` may read the trail."""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not request.user.has_org_permission(
            request.organization, Perm.AUDIT_VIEW
        ):
            raise PermissionDenied("Your role does not grant access to the audit trail.")
        return super().dispatch(request, *args, **kwargs)


class AuditFilterMixin:
    """Shared filtering so the list and the export never disagree."""

    def filtered_queryset(self):
        organization = self.request.organization
        queryset = AuditLog.objects.select_related("organization", "actor")

        # Superusers may look across tenants; everyone else is confined.
        if not self.request.user.is_superuser:
            queryset = queryset.filter(organization=organization)
        elif organization is not None:
            queryset = queryset.filter(organization=organization)

        params = self.request.GET
        search = params.get("q", "").strip()
        if search:
            queryset = queryset.filter(
                Q(actor_label__icontains=search)
                | Q(resource_label__icontains=search)
                | Q(resource_id__icontains=search)
                | Q(action__icontains=search)
                | Q(resource_type__icontains=search)
            )

        for param, field in (
            ("action", "action"),
            ("source", "source"),
            ("resource_type", "resource_type"),
        ):
            value = params.get(param, "").strip()
            if value:
                queryset = queryset.filter(**{field: value})

        days = params.get("days", "").strip()
        if days.isdigit():
            queryset = queryset.filter(
                created_at__gte=timezone.now() - timedelta(days=int(days))
            )

        return queryset.order_by("-created_at")


class AuditLogListView(AuditAccessMixin, AuditFilterMixin, ListView):
    model = AuditLog
    template_name = "audit/list.html"
    context_object_name = "entries"
    paginate_by = 50
    extra_context = {
        "page_title": "Audit Trail",
        "page_subtitle": "Who changed what, and when",
    }

    def get_queryset(self):
        return self.filtered_queryset()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        scope = self.filtered_queryset()

        context.update(
            {
                "search_query": self.request.GET.get("q", ""),
                "active_filters": {
                    key: self.request.GET.get(key, "")
                    for key in ("action", "source", "resource_type", "days")
                },
                # `order_by()` must be cleared first: the model's default
                # ordering puts created_at into the SELECT, which makes every
                # row distinct and defeats DISTINCT entirely.
                "actions": sorted(
                    scope.order_by().values_list("action", flat=True).distinct()
                ),
                "resource_types": sorted(
                    scope.order_by().values_list("resource_type", flat=True).distinct()
                ),
                "sources": AuditSource.choices,
                "total_count": scope.count(),
                "export_query": self.request.GET.urlencode(),
            }
        )
        return context


class AuditLogDetailView(AuditAccessMixin, DetailView):
    model = AuditLog
    template_name = "audit/detail.html"
    context_object_name = "entry"
    extra_context = {"page_title": "Audit Entry"}

    def get_queryset(self):
        queryset = AuditLog.objects.select_related("organization", "actor")
        if self.request.user.is_superuser:
            return queryset
        return queryset.filter(organization=self.request.organization)

    def get_object(self, queryset=None):
        try:
            return super().get_object(queryset)
        except Http404:
            raise Http404("Not found in the active organization.")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        entry = self.object

        old_value, new_value = diff(entry.old_value, entry.new_value)
        context["changes"] = [
            {"field": field, "before": old_value.get(field), "after": new_value.get(field)}
            for field in sorted(set(old_value) | set(new_value))
        ]
        context["verified"] = entry.verify()
        return context


class AuditExportView(AuditAccessMixin, AuditFilterMixin, View):
    """CSV export so an auditor can take evidence away.

    Honours whatever filters the list is showing, and is capped so a stray
    request cannot try to stream the entire history.
    """

    def get(self, request, *args, **kwargs):
        queryset = self.filtered_queryset()[:EXPORT_LIMIT]

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="audit-trail-{stamp}.csv"'

        writer = csv.writer(response)
        writer.writerow([
            "timestamp", "organization", "actor", "source", "action",
            "resource_type", "resource_id", "resource_label",
            "ip_address", "old_value", "new_value", "metadata",
            "entry_hash", "integrity",
        ])
        for entry in queryset.iterator():
            writer.writerow([
                entry.created_at.isoformat(),
                entry.organization.name if entry.organization else "",
                entry.actor_label,
                entry.source,
                entry.action,
                entry.resource_type,
                entry.resource_id,
                entry.resource_label,
                entry.ip_address or "",
                entry.old_value or "",
                entry.new_value or "",
                entry.metadata or "",
                entry.entry_hash,
                "verified" if entry.verify() else "MODIFIED",
            ])

        return response
