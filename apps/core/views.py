"""Reusable view mixins that keep tenant isolation automatic."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http import Http404


class OrganizationScopedMixin(LoginRequiredMixin):
    """Confines a list view's queryset to the request's active organization.

    Subclasses declare `search_fields` and `filter_fields` (a mapping of query
    parameter to ORM lookup) and get search plus filtering for free.
    """

    model = None
    search_fields: list[str] = []
    filter_fields: dict[str, str] = {}

    def get_base_queryset(self):
        return self.model.objects.for_organization(self.request.organization)

    def get_queryset(self):
        queryset = self.get_base_queryset()

        search = self.request.GET.get("q", "").strip()
        if search and self.search_fields:
            condition = Q()
            for field in self.search_fields:
                condition |= Q(**{f"{field}__icontains": search})
            queryset = queryset.filter(condition)

        for param, field in self.filter_fields.items():
            value = self.request.GET.get(param, "").strip()
            if value:
                queryset = queryset.filter(**{field: value})

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["search_query"] = self.request.GET.get("q", "")
        context["active_filters"] = {
            param: self.request.GET.get(param, "") for param in self.filter_fields
        }
        context["total_count"] = self.get_base_queryset().count()
        return context


class ScopedDetailMixin(LoginRequiredMixin):
    """Detail views must never resolve an object from another tenant."""

    def get_queryset(self):
        return self.model.objects.for_organization(self.request.organization)

    def get_object(self, queryset=None):
        try:
            return super().get_object(queryset)
        except Http404:
            raise Http404("Not found in the active organization.")
