"""Forms for people, teams, service accounts and API keys."""

from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils.text import slugify

from apps.access.services import (
    ADMIN_ROLES,
    SERVICE_ACCOUNT_ROLES,
    assignable_roles,
    is_last_admin,
    managed_here,
)
from apps.accounts.roles import Role
from apps.agents.models import AIAgent
from apps.api.models import ServiceAccount
from apps.core.forms import OrganizationModelForm, SectionedFormMixin, StyledFormMixin
from apps.organizations.models import Organization, OrganizationMembership, Team
from apps.organizations.modules import GROUPS as MODULE_GROUPS
from apps.organizations.modules import MODULES, module_states, set_module_states

User = get_user_model()
PROFILE_FIELDS = ("first_name", "last_name", "job_title")


class MemberForm(StyledFormMixin, SectionedFormMixin, forms.Form):
    email = forms.EmailField(label="Work email")
    first_name = forms.CharField(label="First name", max_length=150, required=False)
    last_name = forms.CharField(label="Last name", max_length=150, required=False)
    job_title = forms.CharField(label="Job title", max_length=150, required=False)
    role = forms.ChoiceField(help_text="What they may do in this organization. Roles are per organization.")
    teams = forms.ModelMultipleChoiceField(
        queryset=Team.objects.none(), required=False, widget=forms.CheckboxSelectMultiple,
    )
    send_invitation = forms.BooleanField(
        label="Email them a link to set their password", required=False, initial=True,
        help_text="For someone new to the platform. People who already have an account sign in as usual.",
    )

    sections = (
        ("Person", "", ["email", "first_name", "last_name", "job_title"]),
        ("Access", "", ["role", "teams", "is_default", "send_invitation"]),
    )
    compact_fields = ("first_name", "last_name", "job_title", "role")

    def __init__(self, *args, organization, actor, membership=None, choose_default=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization, self.actor, self.membership = organization, actor, membership
        self.fields["role"].choices = assignable_roles(actor, current=membership.role if membership else None)
        self.fields["teams"].queryset = Team.objects.filter(organization=organization).order_by("name")
        if choose_default:
            # Which organization someone lands in spans all of theirs, so only
            # a site administrator is offered it.
            self.fields["is_default"] = forms.BooleanField(
                label="Their default organization", required=False,
                initial=membership.is_default if membership else False,
                help_text="Where they land after signing in. Someone's first organization always is.",
            )
        if membership is not None:
            user = membership.user
            del self.fields["send_invitation"]
            self.fields["email"].disabled = True
            self.fields["email"].help_text = "Their sign-in. It cannot be changed here."
            self.initial.update({
                "email": user.email, "first_name": user.first_name, "last_name": user.last_name,
                "job_title": user.job_title, "role": membership.role,
                "teams": list(Team.objects.filter(organization=organization, memberships__user=user)
                              .values_list("pk", flat=True)),
            })
            if not managed_here(user, organization):
                for name in PROFILE_FIELDS:
                    self.fields[name].disabled = True
                self.fields["job_title"].help_text = (
                    "They also belong to another organization, so their profile is theirs to change."
                )
        self.apply_styles()

    @property
    def creating(self) -> bool:
        return self.membership is None

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if self.creating and OrganizationMembership.objects.filter(
            organization=self.organization, user__email__iexact=email
        ).exists():
            raise ValidationError("Already a member of this organization.")
        return email

    def clean_role(self):
        role = self.cleaned_data.get("role")
        if not self.creating and is_last_admin(self.membership) and role not in ADMIN_ROLES:
            raise ValidationError(
                "This is the organization's last administrator. Make someone else an administrator first."
            )
        return role


class OrganizationForm(StyledFormMixin, SectionedFormMixin, forms.ModelForm):
    """An organization's own details, for site administrators."""

    class Meta:
        model = Organization
        fields = ["name", "slug", "industry", "description", "status"]
        labels = {"slug": "Short name"}
        help_texts = {
            "slug": "What scripts and commands such as generate_executions call it. Left blank, it is made from the name.",
        }

    sections = (
        ("Organization",
         "The tenant boundary: its agents, applications, policies and audit trail are invisible to every other organization.",
         ["name", "slug", "industry", "description", "status"]),
    )
    compact_fields = ("slug", "industry", "status")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["slug"].required = False
        if not self.creating:
            self.fields["slug"].disabled = True
            self.fields["slug"].help_text = "Fixed once the organization exists: scripts and commands refer to it."
        self._add_module_switches()
        self.apply_styles()

    def _add_module_switches(self):
        """One switch per module, grouped as the menus group them."""
        states = module_states(self.instance)
        module_sections = []
        for group in MODULE_GROUPS:
            names = []
            for module in MODULES:
                if module.group != group:
                    continue
                name = f"module_{module.key}"
                self.fields[name] = forms.BooleanField(
                    label=module.label, required=False, initial=states[module.key], help_text=module.description,
                )
                names.append(name)
            module_sections.append((
                f"Modules: {group}",
                "Switched-off modules leave the menus and their pages for this organization. "
                "Their data is kept, and the API keeps serving." if not module_sections else "",
                names,
            ))
        self.sections = (*type(self).sections, *module_sections)
        self.compact_fields = (*type(self).compact_fields, *(f"module_{m.key}" for m in MODULES))

    def _post_clean(self):
        super()._post_clean()
        set_module_states(self.instance, {
            module.key: self.cleaned_data.get(f"module_{module.key}", False) for module in MODULES
        })

    @property
    def creating(self) -> bool:
        return self.instance._state.adding

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        clash = Organization.objects.filter(name__iexact=name)
        if not self.creating:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise ValidationError("Another organization already has this name.")
        return name

    def clean(self):
        cleaned = super().clean()
        if self.creating and cleaned.get("name") and "slug" not in self.errors:
            slug = slugify(cleaned.get("slug") or cleaned["name"])[:200]
            if not slug:
                self.add_error("slug", "Give it a short name made of letters and digits.")
            elif Organization.objects.filter(slug=slug).exists():
                self.add_error("slug", "Another organization already uses this short name. Choose another.")
            else:
                cleaned["slug"] = slug
        return cleaned


class OrganizationCreateForm(OrganizationForm):
    """A new organization, with the administrator who will run it."""

    admin_email = forms.EmailField(
        label="Work email", help_text="Can be your own. Someone who already has an account simply joins.",
    )
    admin_first_name = forms.CharField(label="First name", max_length=150, required=False)
    admin_last_name = forms.CharField(label="Last name", max_length=150, required=False)
    admin_role = forms.ChoiceField(
        label="Role", initial=Role.ORG_ADMIN,
        choices=[(Role.ORG_ADMIN, Role.ORG_ADMIN.label), (Role.SUPER_ADMIN, Role.SUPER_ADMIN.label)],
        help_text="Both hold every permission in the organization.",
    )
    send_invitation = forms.BooleanField(
        label="Email them a link to set their password", required=False, initial=True,
        help_text="For someone new to the platform.",
    )

    sections = (
        *OrganizationForm.sections,
        ("First administrator",
         "Runs the organization: adds its people, teams and service accounts. An organization always keeps at least one.",
         ["admin_email", "admin_first_name", "admin_last_name", "admin_role", "send_invitation"]),
    )
    compact_fields = (*OrganizationForm.compact_fields, "admin_first_name", "admin_last_name", "admin_role")

    def clean_admin_email(self):
        return (self.cleaned_data.get("admin_email") or "").strip().lower()


class TeamForm(OrganizationModelForm):
    members = forms.ModelMultipleChoiceField(
        queryset=User.objects.all(), required=False, widget=forms.CheckboxSelectMultiple,
        help_text="Budgets, agents and applications can be grouped by team.",
    )

    class Meta:
        model = Team
        fields = ["name", "description", "department", "lead", "status", "members"]
        help_texts = {"status": "Retire a team rather than deleting it once it owns anything."}

    unique_in_organization = ("name",)
    slug_source = "name"
    sections = (
        ("Team", "", ["name", "description", "department", "lead", "status"]),
        ("Members", "", ["members"]),
    )
    compact_fields = ("department", "lead", "status")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.creating:
            self.initial["members"] = list(
                User.objects.filter(team_memberships__team=self.instance).values_list("pk", flat=True)
            )

    def after_save(self, team) -> None:
        from apps.organizations.models import TeamMembership

        wanted = set(self.cleaned_data.get("members") or [])
        current = {membership.user for membership in team.memberships.select_related("user")}
        for user in wanted - current:
            TeamMembership.objects.create(team=team, user=user)
        team.memberships.filter(user__in=current - wanted).delete()


class ServiceAccountForm(OrganizationModelForm):
    agents = forms.ModelMultipleChoiceField(
        queryset=AIAgent.objects.all(), required=False, widget=forms.CheckboxSelectMultiple,
        label="Agents it may act as",
        help_text="Leave empty to let it act for any agent up to Tier 2. Tier 3-4 agents accept only identities bound to them.",
    )

    class Meta:
        model = ServiceAccount
        fields = ["name", "description", "external_system", "role", "status", "agents"]
        labels = {"external_system": "External system"}
        help_texts = {
            "role": "Everything its keys may do. AI Developer covers telemetry and both gateways.",
            "status": "Anything but active stops every one of its keys at once.",
        }

    unique_in_organization = ("name",)
    sections = (
        ("Service account", "One per application or engine, so every call is attributable.",
         ["name", "description", "external_system"]),
        ("Access", "", ["role", "status"]),
        ("Identity", "", ["agents"], {"tag": "IRGF link 02"}),
    )
    compact_fields = ("role", "status")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = list(SERVICE_ACCOUNT_ROLES)
        current = self.instance.role if not self.creating else None
        if current and current not in {value for value, _ in choices}:
            choices.append((current, dict(ServiceAccount._meta.get_field("role").choices)[current]))
        self.fields["role"].choices = choices
        self.fields["agents"].queryset = self.fields["agents"].queryset.order_by("name")
        self.fields["agents"].label_from_instance = (
            lambda agent: f"{agent.name} · Tier {agent.risk_tier}" if agent.risk_tier else f"{agent.name} · unclassified"
        )


EXPIRY_CHOICES = [
    ("", "Never"), ("30", "In 30 days"), ("90", "In 90 days"), ("180", "In 180 days"), ("365", "In a year"),
]


class IssueKeyForm(StyledFormMixin, forms.Form):
    name = forms.CharField(
        label="Key name", max_length=200, initial="default",
        help_text="Where it will be used, e.g. production-worker.",
    )
    expires_in = forms.ChoiceField(label="Expires", choices=EXPIRY_CHOICES, required=False, initial="90")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_styles()
