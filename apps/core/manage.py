"""Create and edit pages for the organization's records, outside the admin.

One view class serves every managed record. It checks the permission, scopes
the form to the active organization, saves, and writes the audit entry the
admin writes -- so a change made on these pages is as attributable as one made
anywhere else, and the IRGF change detection that listens to saves sees it in
the same way.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from apps.audit.models import AuditAction
from apps.audit.services import AuditService, diff, snapshot


def can(request, permission: str) -> bool:
    organization = getattr(request, "organization", None)
    return organization is not None and request.user.has_org_permission(organization, permission)


def audit_snapshot(instance) -> dict:
    """The admin's snapshot, plus many-to-many members, which it does not capture."""
    data = snapshot(instance)
    for field in instance._meta.many_to_many:
        data[field.name] = sorted(
            str(pk) for pk in getattr(instance, field.name).values_list("pk", flat=True)
        )
    return data


class ManagedFormView(LoginRequiredMixin, View):
    """Create a record (no `pk` in the URL) or edit one (with a `pk`)."""

    model = None
    form_class = None
    permission = ""
    denied_message = "Your role does not allow this change."
    template_name = "manage/form.html"

    #: What the record is called in headings and messages, e.g. "agent".
    noun = ""
    create_heading = ""
    create_lead = ""
    create_submit_label = "Save"
    list_url_name = ""
    list_label = ""
    detail_url_name = ""
    #: A template shown beside the form.
    guide_template = ""
    audit_update_action = AuditAction.CONFIG_CHANGE

    # -- Lookup and access ---------------------------------------------------

    def get_object(self):
        pk = self.kwargs.get("pk")
        if pk is None:
            return None
        return get_object_or_404(self.model.objects.for_organization(self.request.organization), pk=pk)

    def check_permission(self, obj) -> None:
        if not can(self.request, self.permission):
            raise PermissionDenied(self.denied_message)

    def new_instance(self):
        return self.model(organization=self.request.organization)

    # -- Form ------------------------------------------------------------------

    def get_form_kwargs(self) -> dict:
        return {}

    def get_form(self, data=None):
        instance = self.object if self.object is not None else self.new_instance()
        return self.form_class(
            data, instance=instance, organization=self.request.organization, **self.get_form_kwargs()
        )

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        self.check_permission(self.object)
        return self.render(self.get_form())

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        self.check_permission(self.object)
        form = self.get_form(request.POST)
        if form.is_valid():
            return self.form_valid(form)
        return self.render(form, status=400)

    def form_valid(self, form):
        creating = self.object is None
        before = None if creating else self.snapshot(self.model.objects.get(pk=self.object.pk))
        self.before_edit(form, creating)
        with transaction.atomic():
            obj = form.save(commit=False)
            if creating and hasattr(obj, "created_by_id"):
                obj.created_by = self.request.user
            if hasattr(obj, "updated_by_id"):
                obj.updated_by = self.request.user
            self.before_save(obj, form, creating)
            obj.save()
            form.save_m2m()
            if hasattr(form, "after_save"):
                form.after_save(obj)
            self.after_save(obj, form, creating)
        changed = self.audit(obj, before, creating)
        messages.success(self.request, self.success_message(obj, creating, changed))
        self.after_commit(obj, form, creating)
        return redirect(self.success_url(obj))

    # -- Hooks -------------------------------------------------------------------

    def before_edit(self, form, creating: bool) -> None:
        """Before anything is written, while the old state is still readable."""

    def before_save(self, obj, form, creating: bool) -> None:
        """Adjust the instance before it is written."""

    def after_save(self, obj, form, creating: bool) -> None:
        """Inside the transaction, after the instance and its relations are written."""

    def after_commit(self, obj, form, creating: bool) -> None:
        """After the audit entry, for messages about what the save set off."""

    def snapshot(self, obj) -> dict:
        """What the audit entry records about the record; views add relations it cannot see."""
        return audit_snapshot(obj)

    def audit_organization(self, obj):
        """The organization whose audit trail records the change."""
        return getattr(obj, "organization", None)

    def audit(self, obj, before, creating: bool) -> bool:
        after = self.snapshot(obj)
        if creating:
            old_value, new_value, action = None, after, AuditAction.CREATE
        else:
            old_value, new_value = diff(before, after)
            if not new_value:
                return False
            action = self.audit_update_action
        AuditService.log(
            action=action,
            resource_type=obj._meta.label,
            resource_id=obj.pk,
            resource_label=str(obj),
            organization=self.audit_organization(obj),
            actor=self.request.user,
            old_value=old_value,
            new_value=new_value,
        )
        return True

    def success_message(self, obj, creating: bool, changed: bool) -> str:
        if creating:
            return f"{self.noun.capitalize()} “{obj}” onboarded."
        if not changed:
            return f"Nothing changed on “{obj}”."
        return f"Saved changes to “{obj}”."

    # -- URLs and page -------------------------------------------------------------

    def list_url(self) -> str:
        return reverse(self.list_url_name)

    def detail_url(self, obj) -> str:
        return reverse(self.detail_url_name, args=[obj.pk]) if self.detail_url_name else self.list_url()

    def success_url(self, obj) -> str:
        return self.detail_url(obj)

    def notices(self) -> list[dict]:
        """Things to know before saving: {"tone", "icon", "text"}."""
        return []

    def get_context_data(self) -> dict:
        return {}

    def render(self, form, status: int = 200):
        creating = self.object is None
        context = {
            "form": form,
            "object": self.object,
            "creating": creating,
            "noun": self.noun,
            "heading": self.create_heading if creating else f"Edit {self.object}",
            "lead": self.create_lead if creating else "",
            "page_title": self.create_heading if creating else f"Edit {self.noun}",
            "back_url": self.list_url() if creating else self.detail_url(self.object),
            "back_label": self.list_label if creating else str(self.object),
            "submit_label": self.create_submit_label if creating else "Save changes",
            "guide_template": self.guide_template,
            "notices": self.notices(),
        }
        context.update(self.get_context_data())
        return render(self.request, self.template_name, context, status=status)
