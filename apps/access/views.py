"""People, teams and service accounts, managed in the web UI.

Every page here needs `user.manage` -- held by organization administrators --
because each one decides who can do what in the organization. Site
administrators reach the member pages of any organization through
`apps.access.site_admin`.
"""

from __future__ import annotations

from datetime import timedelta
from functools import cached_property

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView, ListView

from apps.access.forms import PROFILE_FIELDS, IssueKeyForm, MemberForm, ServiceAccountForm, TeamForm
from apps.access.services import (
    add_member,
    is_last_admin,
    managed_here,
    member_status,
    password_link_message,
    send_password_link,
)
from apps.accounts.roles import Perm, Role
from apps.api.models import APIKey, ServiceAccount
from apps.audit.models import AuditAction
from apps.audit.services import AuditService
from apps.core.manage import ManagedFormView, audit_snapshot, can
from apps.organizations.models import OrganizationMembership, Team, TeamMembership

DENIED = "People, teams and service accounts are managed by holders of user.manage."


class PeopleAccessMixin(LoginRequiredMixin):
    denied_message = DENIED

    def has_access(self) -> bool:
        return can(self.request, Perm.USER_MANAGE)

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not self.has_access():
            raise PermissionDenied(self.denied_message)
        return super().dispatch(request, *args, **kwargs)


def _log(request, *, action, record, label="", old=None, new=None, metadata=None, organization=None):
    AuditService.log(
        action=action, resource_type=record._meta.label, resource_id=record.pk,
        resource_label=label or str(record), organization=organization or request.organization,
        actor=request.user, old_value=old, new_value=new, metadata=metadata,
    )


# -- Members ------------------------------------------------------------------------


class MemberListView(PeopleAccessMixin, ListView):
    template_name = "access/members.html"
    context_object_name = "memberships"
    paginate_by = 50
    extra_context = {"page_title": "Users", "page_subtitle": "Who belongs to the organization, and what each may do"}

    def base(self):
        return OrganizationMembership.objects.filter(organization=self.request.organization)

    def get_queryset(self):
        queryset = self.base().select_related("user").order_by("user__first_name", "user__last_name", "user__email")
        search = self.request.GET.get("q", "").strip()
        if search:
            queryset = queryset.filter(
                Q(user__email__icontains=search) | Q(user__first_name__icontains=search)
                | Q(user__last_name__icontains=search) | Q(user__job_title__icontains=search)
            )
        role = self.request.GET.get("role", "").strip()
        return queryset.filter(role=role) if role else queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        memberships = list(context["memberships"])
        teams: dict = {}
        for user_id, name in TeamMembership.objects.filter(
            team__organization=self.request.organization, user_id__in=[m.user_id for m in memberships]
        ).values_list("user_id", "team__name").order_by("team__name"):
            teams.setdefault(user_id, []).append(name)
        for membership in memberships:
            membership.team_names = teams.get(membership.user_id, [])
            membership.status_label, membership.status_tone = member_status(membership.user)
        context.update({
            "memberships": memberships, "roles": Role.choices, "search_query": self.request.GET.get("q", ""),
            "active_role": self.request.GET.get("role", ""), "total_count": self.base().count(),
            "admin_roles": {Role.SUPER_ADMIN, Role.ORG_ADMIN},
        })
        return context


class _MemberFormPage(PeopleAccessMixin, View):
    #: Site administrators use these pages for any organization, from its own
    #: page, and also choose which organization someone lands in.
    site_admin = False

    @cached_property
    def organization(self):
        return self.request.organization

    def url(self, name: str, *args) -> str:
        return reverse(f"access:{name}", args=args)

    def form(self, data=None, *, membership=None):
        return MemberForm(
            data, organization=self.organization, actor=self.request.user, membership=membership,
            choose_default=self.site_admin,
        )

    def render(self, form, *, membership=None, status=200):
        creating = membership is None
        where = str(self.organization) if self.site_admin else "this organization"
        context = {
            "form": form, "object": membership, "creating": creating, "noun": "member",
            "heading": "Add a member" if creating else f"Edit {membership.user}",
            "lead": f"Give someone access to {where}. Someone new gets an email to set their password." if creating else "",
            "page_title": "Add a member" if creating else "Edit member",
            "back_url": self.url("members"),
            "back_label": str(self.organization) if self.site_admin else "Users",
            "submit_label": "Add member" if creating else "Save changes",
            "guide_template": "access/_member_guide.html",
            "notices": [],
            "site_admin": self.site_admin,
            "can_send_link": membership is not None and managed_here(membership.user, self.organization),
            "remove_url": self.url("member_remove", membership.pk) if membership else "",
            "password_link_url": self.url("member_password_link", membership.pk) if membership else "",
        }
        return render(self.request, "manage/form.html", context, status=status)


class MemberCreateView(_MemberFormPage):
    def get(self, request, **kwargs):
        return self.render(self.form())

    def post(self, request, **kwargs):
        form = self.form(request.POST)
        if not form.is_valid():
            return self.render(form, status=400)
        data, organization = form.cleaned_data, self.organization
        with transaction.atomic():
            membership, new_account = add_member(
                organization, email=data["email"], role=data["role"], teams=data["teams"],
                profile={name: data.get(name, "") for name in PROFILE_FIELDS},
                make_default=bool(data.get("is_default")),
            )
        user = membership.user
        _log(request, action=AuditAction.CREATE, record=membership, organization=organization, new={
            "email": user.email, "role": membership.role, "teams": sorted(team.name for team in data["teams"]),
            "new_account": new_account, "is_default": membership.is_default,
        })
        text = f"Added {user} to {organization} as {membership.get_role_display()}."
        if new_account and data.get("send_invitation"):
            link = send_password_link(user, request=request, organization=organization, actor=request.user, invitation=True)
            text += " " + password_link_message(user, link, invitation=True)
        elif not new_account:
            text += " They already have an account and sign in as usual."
        messages.success(request, text)
        return redirect(self.url("member_edit", membership.pk))


class MemberEditView(_MemberFormPage):
    def membership(self, pk):
        membership = get_object_or_404(
            OrganizationMembership.objects.select_related("user"), organization=self.organization, pk=pk
        )
        if membership.role == Role.SUPER_ADMIN and not self.request.user.is_superuser:
            raise PermissionDenied("Only a superuser changes a super administrator's access.")
        return membership

    @staticmethod
    def state(membership) -> dict:
        user = membership.user
        return {
            "role": membership.role, "is_default": membership.is_default,
            **{name: getattr(user, name) for name in PROFILE_FIELDS},
            "teams": sorted(Team.objects.filter(organization=membership.organization, memberships__user=user)
                            .values_list("name", flat=True)),
        }

    def get(self, request, pk, **kwargs):
        membership = self.membership(pk)
        return self.render(self.form(membership=membership), membership=membership)

    def post(self, request, pk, **kwargs):
        membership = self.membership(pk)
        form = self.form(request.POST, membership=membership)
        if not form.is_valid():
            return self.render(form, membership=membership, status=400)
        before, data, user, organization = self.state(membership), form.cleaned_data, membership.user, self.organization
        with transaction.atomic():
            membership.role = data["role"]
            if "is_default" in form.fields and data["is_default"] != membership.is_default:
                if data["is_default"]:
                    user.organization_memberships.exclude(pk=membership.pk).update(is_default=False)
                membership.is_default = data["is_default"]
            membership.save(update_fields=["role", "is_default", "updated_at"])
            if managed_here(user, organization):
                for name in PROFILE_FIELDS:
                    setattr(user, name, data.get(name, ""))
                user.save(update_fields=list(PROFILE_FIELDS))
            wanted = set(data["teams"])
            current = set(Team.objects.filter(organization=organization, memberships__user=user))
            for team in wanted - current:
                TeamMembership.objects.create(team=team, user=user)
            TeamMembership.objects.filter(user=user, team__in=current - wanted).delete()
        after = self.state(membership)
        changed = sorted(key for key in after if after[key] != before[key])
        if changed:
            _log(
                request, action=AuditAction.ROLE_CHANGE if "role" in changed else AuditAction.UPDATE,
                record=membership, organization=organization,
                old={key: before[key] for key in changed}, new={key: after[key] for key in changed},
            )
            messages.success(request, f"Saved changes to {user}.")
        else:
            messages.success(request, f"Nothing changed for {user}.")
        if not self.has_access():
            return redirect("dashboard:home")  # they changed their own role
        return redirect(self.url("member_edit", membership.pk))


class MemberRemoveView(MemberEditView):
    def get(self, request, pk, **kwargs):  # pragma: no cover - POST only
        return redirect(self.url("member_edit", pk))

    def post(self, request, pk, **kwargs):
        membership = self.membership(pk)
        user, organization = membership.user, self.organization
        if user == request.user:
            messages.error(request, "You cannot remove yourself. Ask another administrator.")
            return redirect(self.url("member_edit", pk))
        if is_last_admin(membership):
            messages.error(request, "This is the organization's last administrator and cannot be removed.")
            return redirect(self.url("member_edit", pk))
        before = self.state(membership)
        with transaction.atomic():
            # Logged first, while the membership still has its id: deleting clears it.
            _log(request, action=AuditAction.DELETE, record=membership, label=str(user), organization=organization,
                 old={"email": user.email, **before})
            TeamMembership.objects.filter(user=user, team__organization=organization).delete()
            membership.delete()
        messages.success(request, f"Removed {user} from {organization}. Any other organization they belong to is unaffected.")
        return redirect(self.url("members"))


class MemberPasswordLinkView(MemberEditView):
    def get(self, request, pk, **kwargs):  # pragma: no cover - POST only
        return redirect(self.url("member_edit", pk))

    def post(self, request, pk, **kwargs):
        membership = self.membership(pk)
        user, organization = membership.user, self.organization
        if not managed_here(user, organization):
            messages.error(request, f"{user} also belongs to another organization or is a superuser; they reset their own password.")
            return redirect(self.url("member_edit", pk))
        invitation = not user.has_usable_password() and user.last_login is None
        link = send_password_link(user, request=request, organization=organization, actor=request.user, invitation=invitation)
        _log(request, action=AuditAction.UPDATE, record=membership, organization=organization,
             metadata={"password_link_sent": True, "invitation": invitation})
        messages.success(request, password_link_message(user, link, invitation=invitation))
        return redirect(self.url("member_edit", pk))


# -- Teams ----------------------------------------------------------------------------


class TeamListView(PeopleAccessMixin, ListView):
    template_name = "access/teams.html"
    context_object_name = "teams"
    extra_context = {"page_title": "Teams", "page_subtitle": "Groups that own agents, applications and budgets"}

    def get_queryset(self):
        return (
            Team.objects.filter(organization=self.request.organization).select_related("lead")
            .annotate(
                member_count=Count("memberships", distinct=True), agent_count=Count("agents", distinct=True),
                application_count=Count("applications", distinct=True), budget_count=Count("budgets", distinct=True),
            ).order_by("name")
        )


class TeamFormView(PeopleAccessMixin, ManagedFormView):
    model = Team
    form_class = TeamForm
    permission = Perm.USER_MANAGE
    denied_message = DENIED
    noun = "team"
    create_heading = "New team"
    create_lead = "A group inside the organization that owns agents, applications, MCP servers and budgets."
    create_submit_label = "Create team"
    list_url_name = "access:teams"
    list_label = "Teams"
    guide_template = "access/_team_guide.html"

    def get_object(self):
        pk = self.kwargs.get("pk")
        return None if pk is None else get_object_or_404(Team, organization=self.request.organization, pk=pk)

    def snapshot(self, obj) -> dict:
        data = audit_snapshot(obj)
        data["members"] = sorted(obj.memberships.values_list("user__email", flat=True))
        return data

    def detail_url(self, obj) -> str:
        return reverse("access:teams")


class TeamDeleteView(PeopleAccessMixin, View):
    def post(self, request, pk):
        team = get_object_or_404(Team, organization=request.organization, pk=pk)
        owned = {
            "agents": team.agents.count(), "applications": team.applications.count(),
            "budgets": team.budgets.count(), "MCP servers": team.mcp_servers.count(),
        }
        owned = {noun: count for noun, count in owned.items() if count}
        if owned:
            listing = ", ".join(f"{count} {noun}" for noun, count in owned.items())
            messages.error(request, f"{team} still owns {listing}. Retire it instead, or move those first.")
            return redirect("access:team_edit", pk=pk)
        before = audit_snapshot(team)
        before["members"] = sorted(team.memberships.values_list("user__email", flat=True))
        with transaction.atomic():
            # Logged first, while the team still has its id: deleting clears it.
            _log(request, action=AuditAction.DELETE, record=team, old=before)
            team.delete()
        messages.success(request, f"Deleted the team {team}.")
        return redirect("access:teams")


# -- Service accounts and keys -------------------------------------------------------------


class ServiceAccountListView(PeopleAccessMixin, ListView):
    template_name = "access/service_accounts.html"
    context_object_name = "accounts"
    extra_context = {
        "page_title": "Integrations",
        "page_subtitle": "Service accounts: the identities applications and engines call the platform as",
    }

    def get_queryset(self):
        now = timezone.now()
        return (
            ServiceAccount.objects.for_organization(self.request.organization)
            .annotate(
                agent_count=Count("agents", distinct=True),
                usable_keys=Count(
                    "api_keys", distinct=True,
                    filter=Q(api_keys__revoked_at__isnull=True)
                    & (Q(api_keys__expires_at__isnull=True) | Q(api_keys__expires_at__gt=now)),
                ),
                last_used=Max("api_keys__last_used_at"),
            ).order_by("name")
        )


class ServiceAccountDetailView(PeopleAccessMixin, DetailView):
    template_name = "access/service_account_detail.html"
    context_object_name = "account"

    def get_queryset(self):
        return ServiceAccount.objects.for_organization(self.request.organization)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        account = self.object
        context.update({
            "page_title": "Service account",
            "agents": account.agents.order_by("name"),
            "keys": account.api_keys.order_by("-created_at"),
            "issue_form": IssueKeyForm(),
        })
        return context


class ServiceAccountFormView(PeopleAccessMixin, ManagedFormView):
    model = ServiceAccount
    form_class = ServiceAccountForm
    permission = Perm.USER_MANAGE
    denied_message = DENIED
    noun = "service account"
    create_heading = "New service account"
    create_lead = "The identity one application or engine calls the platform as. Its keys carry its role."
    create_submit_label = "Create service account"
    list_url_name = "access:service_accounts"
    list_label = "Integrations"
    detail_url_name = "access:service_account_detail"
    guide_template = "access/_service_account_guide.html"

    def notices(self):
        if self.object is None:
            return []
        from apps.lifecycle.services import AuthorizationService

        authorized = [agent for agent in self.object.agents.all() if AuthorizationService.baseline(agent)]
        if not authorized:
            return []
        names = ", ".join(agent.name for agent in authorized)
        return [{
            "tone": "warning", "icon": "bi-patch-check",
            "text": (
                f"It is a bound identity of {names}, authorized at Gate G3. Binding or unbinding an agent "
                "changes that agent's authorized configuration and is recorded for Gate G4."
            ),
        }]

    def success_message(self, obj, creating, changed):
        if creating:
            return f"Service account “{obj.name}” created. Issue its first API key below."
        return super().success_message(obj, creating, changed)


class IssueKeyView(PeopleAccessMixin, View):
    def post(self, request, pk):
        account = get_object_or_404(ServiceAccount.objects.for_organization(request.organization), pk=pk)
        if not account.is_active:
            messages.error(request, f"{account.name} is not active, so a key for it would not work. Activate it first.")
            return redirect("access:service_account_detail", pk=pk)
        form = IssueKeyForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Give the key a name.")
            return redirect("access:service_account_detail", pk=pk)
        days = form.cleaned_data.get("expires_in")
        expires_at = timezone.now() + timedelta(days=int(days)) if days else None
        api_key, raw_key = APIKey.generate(account, name=form.cleaned_data["name"], expires_at=expires_at)
        # The secret never enters the audit trail: its prefix identifies the key.
        _log(request, action=AuditAction.CREATE, record=api_key, new={
            "service_account": account.name, "name": api_key.name, "prefix": api_key.prefix,
            "expires_at": expires_at.isoformat() if expires_at else None,
        })
        # Shown in this one response and nowhere else: not a message, not the
        # session, and not cached.
        response = render(request, "access/key_issued.html", {
            "page_title": "New API key", "account": account, "api_key": api_key, "raw_key": raw_key,
            "api_base": request.build_absolute_uri("/api/v1/"),
        })
        response["Cache-Control"] = "no-store"
        return response


class RevokeKeyView(PeopleAccessMixin, View):
    def post(self, request, pk):
        api_key = get_object_or_404(
            APIKey.objects.select_related("service_account"),
            service_account__organization=request.organization, pk=pk,
        )
        account = api_key.service_account
        if api_key.is_revoked:
            messages.info(request, f"Key {api_key.prefix} was already revoked.")
        else:
            api_key.revoke()
            _log(request, action=AuditAction.UPDATE, record=api_key, old={"revoked_at": None},
                 new={"revoked_at": api_key.revoked_at.isoformat()}, metadata={"decision": "revoke"})
            messages.success(request, f"Revoked key {api_key.prefix}. Calls made with it are refused from now on.")
        return redirect("access:service_account_detail", pk=account.pk)
