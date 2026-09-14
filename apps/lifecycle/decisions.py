"""Who may sign a gate decision (IRGF M15 and M16).

Every G3, G4 and M17 decision collects the signatures IRGF's decision-rights
matrix asks for at the record's tier: one at Tier 1-3, two from different
people at Tier 4. The rules live here, once, so the three records cannot drift
apart on who may decide what.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.accounts.roles import Perm
from apps.lifecycle import irgf

_SEAT_PERMISSIONS = {
    irgf.RISK_LEAD.key: Perm.RISK_MANAGE,
    irgf.BODY.key: Perm.GATE_DECIDE,
    irgf.SPONSOR.key: Perm.GATE_SPONSOR,
}


@dataclass
class SignCheck:
    ok: bool
    reason: str = ""
    seat: irgf.Seat | None = None


def _is_person(user) -> bool:
    return bool(user and getattr(user, "pk", None) and not getattr(user, "is_service_account", False))


def holds(seat: irgf.Seat, user, *, organization, owner_id) -> bool:
    if seat.key == irgf.OWNER.key:
        return bool(owner_id) and user.pk == owner_id
    return user.has_org_permission(organization, _SEAT_PERMISSIONS[seat.key])


def slots(record) -> tuple[tuple[irgf.Seat, ...], ...]:
    return irgf.rights(record.gate, record.tier)


def remaining(record) -> int:
    return max(0, len(slots(record)) - len(record.signatures or []))


def is_complete(record) -> bool:
    return remaining(record) == 0


def next_seats(record) -> tuple[irgf.Seat, ...]:
    index = len(record.signatures or [])
    available = slots(record)
    return available[index] if index < len(available) else ()


def _someone_else_holds(seat: irgf.Seat, record, *, excluded: set[str], owner_id) -> bool:
    """Whether anyone outside `excluded` could give this seat's signature."""
    if seat.key == irgf.OWNER.key:
        return bool(owner_id) and str(owner_id) not in excluded
    members = get_user_model().objects.filter(
        organization_memberships__organization=record.organization, is_active=True
    ).distinct()
    return any(
        str(member.pk) not in excluded
        and holds(seat, member, organization=record.organization, owner_id=owner_id)
        for member in members
    )


def _stranded_seat(record, user, *, owner_id, requester_id) -> irgf.Seat | None:
    """A later seat nobody but this person could fill, were they to sign now.

    Tier 4 takes the AI Governance Body and then the Enterprise Governance
    Sponsor, two different people. An administrator holds both seats; if they
    are the only sponsor and sign first, the second seat is left to nobody and
    the decision can neither be completed nor refused.
    """
    later = slots(record)[len(record.signatures or []) + 1:]
    if not later:
        return None
    excluded = {str(user.pk), *(entry.get("user") for entry in record.signatures or [])}
    if record.gate != irgf.AUTHORIZATION and requester_id:
        excluded.add(str(requester_id))
    if record.tier >= irgf.INDEPENDENT_FROM_TIER:
        excluded.update(str(pk) for pk in (owner_id, requester_id) if pk)
    for seats in later:
        if not any(
            _someone_else_holds(seat, record, excluded=excluded, owner_id=owner_id) for seat in seats
        ):
            return seats[0]
    return None


def check(record, user, *, approving: bool = True) -> SignCheck:
    """Whether this person may give the record's next signature, and as what.

    `approving=False` asks only whether they may refuse it: a refusal ends the
    decision, so it cannot strand a later seat.
    """
    if not _is_person(user):
        return SignCheck(False, "Gate decisions are signed by people, not service accounts.")
    if is_complete(record):
        return SignCheck(False, "Every signature this decision needs has been given.")
    if any(str(user.pk) == entry.get("user") for entry in record.signatures or []):
        return SignCheck(
            False, "You have already signed. Tier 4 needs a second, different person."
        )

    owner_id = record.subject_owner_id
    requester_id = record.requester_id
    if record.gate != irgf.AUTHORIZATION and requester_id and user.pk == requester_id:
        return SignCheck(False, "Whoever made or requested this cannot also decide it.")
    if record.tier >= irgf.INDEPENDENT_FROM_TIER and user.pk in {owner_id, requester_id}:
        return SignCheck(
            False,
            f"Tier {record.tier}: IRGF takes this decision out of the delivery line, so the "
            "agent's owner and the requester cannot sign it.",
        )

    seats = next_seats(record)
    for seat in seats:
        if holds(seat, user, organization=record.organization, owner_id=owner_id):
            stranded = approving and _stranded_seat(
                record, user, owner_id=owner_id, requester_id=requester_id
            )
            if not stranded:
                return SignCheck(True, seat=seat)
            if holds(stranded, user, organization=record.organization, owner_id=owner_id):
                return SignCheck(
                    False,
                    f"You are the only {stranded.label} who could sign after this. "
                    f"Let someone else sign as {seat.label} first, then add yours.",
                )
            return SignCheck(
                False,
                f"Nobody else can sign as {stranded.label} ({stranded.held_by}) after this, "
                "so the decision could not be completed.",
            )
    return SignCheck(
        False,
        f"This signature needs {' or '.join(f'{s.label} ({s.held_by})' for s in seats)}.",
    )


def signature(user, seat: irgf.Seat, note: str = "") -> dict:
    name = user.get_full_name() if hasattr(user, "get_full_name") else ""
    return {
        "user": str(user.pk),
        "name": name or getattr(user, "email", "") or str(user),
        "seat": seat.key,
        "seat_label": seat.label,
        "at": timezone.now().isoformat(),
        "note": note,
    }


def describe(record) -> str:
    return irgf.describe_rights(record.gate, record.tier)
