"""The agent kill-switch, and the IRGF agent governance chain.

IRGF (Intent-to-Runtime Governance Framework, module M10) governs an agent
through eight links: agent, identity, permission, tool, policy, action,
verification and audit. `governance_chain` reports where one agent stands on
each; `KillSwitch` is link 06's stop control.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditAction
from apps.audit.services import AuditService


class KillSwitchError(ValueError):
    """A suspend or resume the rules do not allow."""


def _person(actor):
    """The actor as a user record, or None for machines and the platform."""
    if actor is None or getattr(actor, "is_service_account", False) or not getattr(actor, "pk", None):
        return None
    return actor


def _label(actor) -> str:
    if actor is None:
        return "the platform"
    if getattr(actor, "is_service_account", False):
        return str(actor)
    name = actor.get_full_name() if hasattr(actor, "get_full_name") else ""
    return name or getattr(actor, "email", "") or str(actor)


class KillSwitch:
    """Suspend and resume an agent (IRGF agent chain link 06).

    Suspension takes effect on the next request: both gateways and agent
    messaging check it before anything else. Machines may pull the switch --
    a monitor that sees an agent misbehave should not have to wait for a
    person -- but only a person may release it.
    """

    @staticmethod
    @transaction.atomic
    def suspend(agent, *, actor, reason: str):
        reason = (reason or "").strip()
        if not reason:
            raise KillSwitchError(
                "Say why the agent is being suspended; the reason goes to its managers "
                "and the audit trail."
            )
        if agent.is_suspended:
            raise KillSwitchError(f"{agent.name} is already suspended.")

        person = _person(actor)
        agent.suspended_at = timezone.now()
        agent.suspended_by = person
        agent.suspended_by_label = _label(actor)
        agent.suspension_reason = reason
        agent.save(update_fields=[
            "suspended_at", "suspended_by", "suspended_by_label", "suspension_reason", "updated_at",
        ])

        AuditService.log(
            action=AuditAction.AGENT_SUSPEND,
            resource_type=agent._meta.label,
            resource_id=agent.pk,
            resource_label=agent.name,
            organization=agent.organization,
            actor=person,
            new_value={"suspended": True, "reason": reason},
            metadata={"irgf": "agent chain link 06: kill-switch", "by": agent.suspended_by_label},
        )

        from apps.notifications import emitters

        emitters.agent_suspended(agent)
        return agent

    @staticmethod
    @transaction.atomic
    def resume(agent, *, actor, note: str = ""):
        person = _person(actor)
        if person is None:
            raise KillSwitchError(
                "Only a person can release a kill-switch: machines may stop an agent, "
                "people decide when it runs again."
            )
        if not agent.is_suspended:
            raise KillSwitchError(f"{agent.name} is not suspended.")

        previous = {
            "suspended_at": agent.suspended_at.isoformat(),
            "suspended_by": agent.suspended_by_label,
            "reason": agent.suspension_reason,
        }
        agent.suspended_at = None
        agent.suspended_by = None
        agent.suspended_by_label = ""
        agent.suspension_reason = ""
        agent.save(update_fields=[
            "suspended_at", "suspended_by", "suspended_by_label", "suspension_reason", "updated_at",
        ])

        note = (note or "").strip()
        AuditService.log(
            action=AuditAction.AGENT_RESUME,
            resource_type=agent._meta.label,
            resource_id=agent.pk,
            resource_label=agent.name,
            organization=agent.organization,
            actor=person,
            old_value=previous,
            new_value={"suspended": False, "note": note},
            metadata={"irgf": "agent chain link 06: kill-switch"},
        )

        from apps.notifications import emitters

        emitters.agent_resumed(agent, resumed_by=_label(person), note=note)
        return agent


# -- The governance chain ----------------------------------------------------


@dataclass
class ChainLink:
    number: str
    name: str
    #: met | partial | gap | planned
    state: str
    detail: str

    @property
    def icon(self) -> str:
        return {
            "met": "bi-check-circle-fill text-success",
            "partial": "bi-dash-circle-fill text-warning",
            "gap": "bi-x-circle-fill text-danger",
            "planned": "bi-clock text-muted",
        }.get(self.state, "bi-dot")


def governance_chain(agent) -> list[ChainLink]:
    """Where one agent stands on each of IRGF's eight links."""
    from apps.mcp_gateway.models import MCPAccessGrant
    from apps.policies.models import Policy, PolicyEffect
    from apps.policies.services import in_tier_scope

    tier = agent.risk_tier
    accounts = list(agent.service_accounts.values_list("name", flat=True))
    grants = MCPAccessGrant.objects.filter(agent=agent).count()
    contracts = agent.a2a_contracts.count()
    enforcing = [
        policy
        for policy in Policy.objects.for_organization(agent.organization).filter(
            status="active", effect__in=[PolicyEffect.DENY, PolicyEffect.REQUIRE_APPROVAL]
        )
        if in_tier_scope(policy, tier)
    ]
    limits = bool(agent.action_limits)
    conditions = bool((agent.kill_switch_conditions or "").strip())

    if agent.business_purpose and agent.out_of_scope:
        scope = ChainLink("01", "Agent", "met", "Scope and exclusions are stated.")
    elif agent.business_purpose:
        scope = ChainLink("01", "Agent", "partial", "Purpose stated; say what it is not for.")
    else:
        scope = ChainLink("01", "Agent", "gap", "State in one sentence what the agent is for.")

    if accounts:
        identity = ChainLink(
            "02", "Identity", "met", f"Acts under {', '.join(accounts)}; other keys are refused."
        )
    elif tier and tier >= 3:
        identity = ChainLink(
            "02", "Identity", "gap",
            f"Tier {tier}: the gateways refuse it until a service account is bound to it.",
        )
    else:
        identity = ChainLink(
            "02", "Identity", "partial",
            "No identity of its own: any unbound key may act for it while it is Tier 1-2.",
        )

    from apps.lifecycle.services import AuthorizationService

    authorization = AuthorizationService.baseline(agent)
    if authorization is None:
        approved = " No G3 authorization of this configuration yet."
    elif authorization.is_in_force:
        approved = f" Authorized at Gate G3 for {authorization.get_environment_display().lower()}."
    else:
        approved = " Its G3 authorization is suspended."
    permission = ChainLink(
        "03", "Permission", "met" if authorization and authorization.is_in_force else "partial",
        f"Autonomy level {agent.autonomy_level}; approval "
        f"{'required' if agent.approval_required else 'not required'}; "
        f"{grants} MCP access grant{'s' if grants != 1 else ''}; "
        f"{contracts} A2A contract{'s' if contracts != 1 else ''}.{approved}",
    )

    if agent.allowed_tools:
        tool = ChainLink(
            "04", "Tool", "met",
            f"Allow-list of {len(agent.allowed_tools)}; MCP grants enforce theirs at the gateway.",
        )
    else:
        tool = ChainLink("04", "Tool", "gap", "No allow-list. Deny-lists fail open.")

    policy = (
        ChainLink("05", "Policy", "met",
                  f"{len(enforcing)} deny or approval polic{'ies' if len(enforcing) != 1 else 'y'} "
                  "enforced by both gateways.")
        if enforcing
        else ChainLink("05", "Policy", "gap", "No active policy can stop this agent.")
    )

    automatic = len(agent.kill_switch_triggers or [])
    if agent.is_suspended:
        action = ChainLink("06", "Action", "gap", f"Suspended: {agent.suspension_reason}")
    elif limits and conditions and (automatic or not (tier and tier >= 3)):
        action = ChainLink(
            "06", "Action", "met",
            "Limits enforced before each tool call; kill-switch armed"
            + (f", {automatic} trigger{'s' if automatic != 1 else ''} automatic." if automatic else " with conditions."),
        )
    elif limits and conditions:
        action = ChainLink(
            "06", "Action", "partial", f"Tier {tier}: at least one kill-switch trigger must fire automatically."
        )
    elif limits or conditions:
        action = ChainLink(
            "06", "Action", "partial",
            "Kill-switch armed; declare " + ("kill-switch conditions." if limits else "action limits."),
        )
    else:
        action = ChainLink(
            "06", "Action", "partial", "Kill-switch armed; no action limits or conditions declared."
        )

    from datetime import timedelta

    from apps.controlplane import irgf as control_irgf

    ceiling = control_irgf.verification_ceiling(tier)
    ceiling_text = f"{ceiling // 60} min" if ceiling < 3600 else f"{ceiling // 3600} h"
    recent = agent.verifications.filter(verified_at__gte=timezone.now() - timedelta(days=1))
    checked, late = recent.count(), recent.filter(late=True).count()
    if authorization is None:
        verification = ChainLink(
            "07", "Verification", "partial",
            f"Actions are checked within {ceiling_text}, but with no G3 authorization there is no approved state to check them against.",
        )
    elif late:
        verification = ChainLink(
            "07", "Verification", "gap",
            f"{late} of {checked} action{'s' if checked != 1 else ''} in the last day verified after the {ceiling_text} ceiling.",
        )
    else:
        verification = ChainLink(
            "07", "Verification", "met",
            f"Every action is checked against the authorized state within {ceiling_text}"
            + (f"; {checked} in the last day." if checked else "."),
        )
    audit = ChainLink(
        "08", "Audit", "met", "Every gateway call, tool call and change is in the hash-verified trail."
    )
    return [scope, identity, permission, tool, policy, action, verification, audit]
