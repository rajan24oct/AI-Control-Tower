"""Site administration: organizations, and who belongs to each.

A site administrator is a superuser. They create organizations, each with its
first administrator, and staff any organization from its page -- whether or
not they belong to it, and whichever organization is active for them.
Everything else inside an organization stays with its own administrators.
"""

from __future__ import annotations

from functools import cached_property

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.generic import DetailView, ListView

from apps.access.forms import OrganizationCreateForm, OrganizationForm
from apps.access.services import (
    ADMIN_ROLES,
    add_member,
    is_site_admin,
    member_status,
    password_link_message,
    send_password_link,
)
from apps.access.views import (
    MemberCreateView,
    MemberEditView,
    MemberPasswordLinkView,
    MemberRemoveView,
    PeopleAccessMixin,
    _log,
)
from apps.agents.models import AIAgent
from apps.api.models import ServiceAccount
from apps.audit.models import AuditAction
from apps.core.manage import ManagedFormView
from apps.organizations.models import Organization, TeamMembership

DENIED = "Organizations are created and staffed by site administrators (superusers)."


class SiteAdminMixin(PeopleAccessMixin):
    denied_message = DENIED

    def has_access(self) -> bool:
        return is_site_admin(self.request.user)


class OrganizationListView(SiteAdminMixin, ListView):
    template_name = "access/organizations.html"
    context_object_name = "organizations"
    paginate_by = 50
    extra_context = {"page_title": "Organizations", "page_subtitle": "Every tenant on this installation, and who runs each one"}

    def get_queryset(self):
        queryset = Organization.objects.annotate(
            member_count=Count("memberships", distinct=True),
            admin_count=Count("memberships", filter=Q(memberships__role__in=ADMIN_ROLES), distinct=True),
            team_count=Count("teams", distinct=True),
        ).order_by("name")
        search = self.request.GET.get("q", "").strip()
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | Q(slug__icontains=search) | Q(industry__icontains=search)
            )
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({"search_query": self.request.GET.get("q", ""), "total_count": Organization.objects.count()})
        return context


class OrganizationDetailView(SiteAdminMixin, DetailView):
    model = Organization
    template_name = "access/organization_detail.html"
    context_object_name = "org"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        org = self.object
        memberships = list(
            org.memberships.select_related("user").order_by("user__first_name", "user__last_name", "user__email")
        )
        teams: dict = {}
        for user_id, name in (
            TeamMembership.objects.filter(team__organization=org)
            .values_list("user_id", "team__name").order_by("team__name")
        ):
            teams.setdefault(user_id, []).append(name)
        for membership in memberships:
            membership.team_names = teams.get(membership.user_id, [])
            membership.status_label, membership.status_tone = member_status(membership.user)
        context.update({
            "page_title": "Organization",
            "memberships": memberships,
            "admin_roles": ADMIN_ROLES,
            "admin_count": sum(1 for membership in memberships if membership.role in ADMIN_ROLES),
            "counts": {
                "Members": len(memberships),
                "Teams": org.teams.count(),
                "Agents": AIAgent.objects.filter(organization=org).count(),
                "Service accounts": ServiceAccount.objects.filter(organization=org).count(),
            },
            "is_active_here": org == self.request.organization,
        })
        return context


class OrganizationFormView(SiteAdminMixin, ManagedFormView):
    model = Organization
    form_class = OrganizationForm
    denied_message = DENIED
    noun = "organization"
    create_heading = "New organization"
    create_lead = "A new tenant, with the person who will run it. Nothing in it is visible to any other organization."
    create_submit_label = "Create organization"
    list_url_name = "access:organizations"
    list_label = "Organizations"
    detail_url_name = "access:organization_detail"
    guide_template = "access/_organization_guide.html"

    def get_object(self):
        pk = self.kwargs.get("pk")
        return None if pk is None else get_object_or_404(Organization, pk=pk)

    def check_permission(self, obj) -> None:
        if not self.has_access():
            raise PermissionDenied(self.denied_message)

    def new_instance(self):
        return Organization()

    def get_form(self, data=None):
        if self.object is None:
            return OrganizationCreateForm(data, instance=self.new_instance())
        return OrganizationForm(data, instance=self.object)

    def audit_organization(self, obj):
        return obj

    def after_save(self, obj, form, creating: bool) -> None:
        if creating:
            data = form.cleaned_data
            self.first_admin, self.new_account = add_member(
                obj, email=data["admin_email"], role=data["admin_role"],
                profile={"first_name": data.get("admin_first_name", ""), "last_name": data.get("admin_last_name", "")},
            )

    def after_commit(self, obj, form, creating: bool) -> None:
        if not creating:
            return
        membership, user = self.first_admin, self.first_admin.user
        _log(self.request, action=AuditAction.CREATE, record=membership, organization=obj, new={
            "email": user.email, "role": membership.role, "teams": [], "new_account": self.new_account,
            "is_default": membership.is_default,
        })
        text = f"{user} is its {membership.get_role_display()}."
        if self.new_account and form.cleaned_data.get("send_invitation"):
            link = send_password_link(user, request=self.request, organization=obj, actor=self.request.user, invitation=True)
            text += " " + password_link_message(user, link, invitation=True)
        elif not self.new_account:
            text += " They already have an account and sign in as usual."
        messages.success(self.request, text)

    def success_message(self, obj, creating: bool, changed: bool) -> str:
        if creating:
            return f"Organization “{obj}” created."
        return super().success_message(obj, creating, changed)


# -- Memberships of any organization --------------------------------------------------


class SiteMemberMixin(SiteAdminMixin):
    """The member pages, for the organization in the URL rather than the active one."""

    site_admin = True

    @cached_property
    def organization(self):
        return get_object_or_404(Organization, pk=self.kwargs["org"])

    def url(self, name: str, *args) -> str:
        name = "organization_detail" if name == "members" else f"organization_{name}"
        return reverse(f"access:{name}", args=[self.organization.pk, *args])


class SiteMemberCreateView(SiteMemberMixin, MemberCreateView):
    pass


class SiteMemberEditView(SiteMemberMixin, MemberEditView):
    pass


class SiteMemberRemoveView(SiteMemberMixin, MemberRemoveView):
    pass


class SiteMemberPasswordLinkView(SiteMemberMixin, MemberPasswordLinkView):
    pass
