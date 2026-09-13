"""Reusable view mixins that keep tenant isolation automatic."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Case, F, IntegerField, Q, Value, When
from django.http import Http404


def ranked(field: str, values) -> Case:
    """Order a choice field by what it means rather than how it is spelt.

    Alphabetically "critical" sorts before "low"; ranked, it is low < moderate
    < high < critical -- the order anyone reading a severity column expects.
    """
    return Case(
        *[When(**{field: value}, then=Value(rank)) for rank, value in enumerate(values)],
        default=Value(len(values)),
        output_field=IntegerField(),
    )


class SortableListMixin:
    """Column sorting for a list view, over the whole result set.

    Views declare `sort_fields`, mapping a column key to what it sorts by: an
    ORM path, a list of them (ties broken left to right), an expression, or a
    `(field, [values in rank order])` pair for a choice field whose values
    carry an order. `?sort=key` sorts ascending and `?sort=-key` descending; an
    unknown key leaves the view's own order untouched.

    The sort is applied to the final list, after every filter a view adds, so
    a subclass that reshapes its queryset cannot quietly undo it -- and it
    applies whether or not the view paginates.
    """

    sort_fields: dict = {}

    def requested_sort(self) -> str:
        requested = self.request.GET.get("sort", "").strip()
        return requested if requested.lstrip("-") in self.sort_fields else ""

    def apply_sort(self, queryset):
        requested = self.requested_sort()
        if not requested or not hasattr(queryset, "order_by"):
            return queryset

        descending = requested.startswith("-")
        spec = self.sort_fields[requested.lstrip("-")]
        terms = spec if isinstance(spec, list) else [spec]

        ordering = []
        for position, term in enumerate(terms):
            if isinstance(term, tuple):
                term = ranked(*term)
            elif isinstance(term, str):
                term = F(term)
            if getattr(term, "contains_aggregate", False):
                # Django orders by an aggregate only once it is annotated.
                alias = f"sort_value_{position}"
                queryset = queryset.annotate(**{alias: term})
                term = F(alias)
            ordering.append(term.desc(nulls_last=True) if descending else term.asc(nulls_last=True))
        # Rows that tie must still keep one order, or they swap between pages.
        ordering.append(F("pk").desc() if descending else F("pk").asc())
        return queryset.order_by(*ordering)

    def get_context_data(self, **kwargs):
        if "object_list" not in kwargs and hasattr(self, "object_list"):
            kwargs["object_list"] = self.apply_sort(self.object_list)
        context = super().get_context_data(**kwargs)
        context["current_sort"] = self.requested_sort()
        return context


class OrganizationScopedMixin(SortableListMixin, LoginRequiredMixin):
    """Confines a list view's queryset to the request's active organization.

    Subclasses declare `search_fields` and `filter_fields` (a mapping of query
    parameter to ORM lookup) and get search plus filtering for free, and
    `sort_fields` for column sorting (see `SortableListMixin`).
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
