"""Rules for managing people and machine identities in one organization.

Two protections run through this module. An organization always keeps an
administrator, so nobody can lock it out of its own settings. And an
organization administrator only ever controls accounts that belong to their
organization alone: someone who is also a member elsewhere keeps their profile
and their password to themselves.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.roles import Role
from apps.organizations.models import OrganizationMembership, TeamMembership

ADMIN_ROLES = {Role.SUPER_ADMIN, Role.ORG_ADMIN}


def is_site_admin(user) -> bool:
    """A site administrator creates organizations and staffs any of them: a superuser."""
    return bool(getattr(user, "is_authenticated", False) and user.is_superuser and user.is_active)


def add_member(organization, *, email: str, role: str, profile: dict | None = None, teams=(), make_default: bool = False):
    """Give `email` a membership of `organization`, creating the account if it is new.

    Returns (membership, new_account). A new account has no usable password
    until its owner sets one through a link. Someone's first organization is
    their default; `make_default` moves the default here.
    """
    users = get_user_model().objects
    user = users.filter(email__iexact=email).first()
    new_account = user is None
    if new_account:
        user = users.create_user(email=email, password=None, **(profile or {}))
    is_default = make_default or not user.organization_memberships.exists()
    if is_default:
        user.organization_memberships.update(is_default=False)
    membership = OrganizationMembership.objects.create(
        organization=organization, user=user, role=role, is_default=is_default
    )
    for team in teams:
        TeamMembership.objects.get_or_create(team=team, user=user)
    return membership, new_account

#: Backends that do not actually deliver mail. With one of these, an invitation
#: link would otherwise reach nobody, so the inviting administrator is shown it.
UNDELIVERED_BACKENDS = {
    "django.core.mail.backends.console.EmailBackend",
    "django.core.mail.backends.locmem.EmailBackend",
    "django.core.mail.backends.dummy.EmailBackend",
    "django.core.mail.backends.filebased.EmailBackend",
}


def assignable_roles(actor, *, current: str | None = None) -> list[tuple[str, str]]:
    """Roles `actor` may hand out. Only a superuser creates super administrators."""
    return [
        (value, label) for value, label in Role.choices
        if value != Role.SUPER_ADMIN or actor.is_superuser or value == current
    ]


#: A service account's role is capped below administrator: a leaked key should
#: never be able to hand out access.
SERVICE_ACCOUNT_ROLES = [
    (value, label) for value, label in Role.choices if value not in (Role.SUPER_ADMIN, Role.ORG_ADMIN)
]


def is_last_admin(membership: OrganizationMembership) -> bool:
    if membership.role not in ADMIN_ROLES:
        return False
    return not OrganizationMembership.objects.filter(
        organization_id=membership.organization_id, role__in=ADMIN_ROLES
    ).exclude(pk=membership.pk).exists()


def managed_here(user, organization) -> bool:
    """Whether this organization alone governs the account.

    True for someone who belongs to no other organization and is not a
    superuser. Only then may this organization's administrators change their
    profile or send them a link that sets their password.
    """
    if user.is_superuser:
        return False
    return not user.organization_memberships.exclude(organization=organization).exists()


def member_status(user) -> tuple[str, str]:
    """(label, badge tone) for the members list."""
    if not user.is_active:
        return "Deactivated", "secondary"
    if not user.has_usable_password() and user.last_login is None:
        return "Invited", "warning"
    return "Active", "success"


def email_is_delivered() -> bool:
    return settings.EMAIL_BACKEND not in UNDELIVERED_BACKENDS


def password_link_message(user, link: str, *, invitation: bool) -> str:
    """What the sender is told once a link to set a password has gone out."""
    if email_is_delivered():
        return f"Emailed {user.email} a link to set their password."
    if invitation:
        # Nothing is delivered in this installation: the new member would
        # never see the email, so hand the link to the person inviting them.
        return (
            f"This installation prints email to the server console rather than sending it. "
            f"Pass this link to {user.email} to set their password: {link}"
        )
    return f"A link to set a new password was written to the server console for {user.email}."


def password_link(user, request) -> str:
    path = reverse(
        "accounts:set_password",
        kwargs={
            "uidb64": urlsafe_base64_encode(force_bytes(user.pk)),
            "token": default_token_generator.make_token(user),
        },
    )
    base = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    return f"{base}{path}" if base else request.build_absolute_uri(path)


def send_password_link(user, *, request, organization, actor, invitation: bool) -> str:
    """Email `user` a single-use link to set a password. Returns the link."""
    link = password_link(user, request)
    days = max(1, settings.PASSWORD_RESET_TIMEOUT // 86400)
    sender = actor.get_full_name() or actor.email
    if invitation:
        membership = OrganizationMembership.objects.filter(organization=organization, user=user).first()
        role = membership.get_role_display() if membership else "a member"
        subject = f"[AI Control Tower] You have been added to {organization.name}"
        body = (
            f"{sender} has added you to {organization.name} on AI Control Tower as {role}.\n\n"
            f"Set your password here. The link works once, for {days} days:\n{link}\n\n"
            f"Then sign in with {user.email}."
        )
    else:
        subject = "[AI Control Tower] Set a new password"
        body = (
            f"{sender}, an administrator of {organization.name}, has sent you a link to set a new "
            f"password for {user.email}.\n\nThe link works once, for {days} days:\n{link}\n\n"
            "If you did not expect this, tell your administrator."
        )
    send_mail(
        subject=subject, message=body, from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email], fail_silently=False,
    )
    return link
