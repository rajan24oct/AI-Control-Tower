"""IRGF gates G3 and G4, and module M17: decision rights, the change table, limits.

The Intent-to-Runtime Governance Framework (IRGF v1.0,
https://www.irgframework.org/) authorizes a system for deployment at stage S7
(Gate G3), governs every later change at S9 (Gate G4), and allows time-boxed
exceptions under module M17. What changes with the risk tier is who decides and
how much evidence they need; the gates themselves never change.

Kept free of Django, like the S2 tier rule, so the rules can be read and tested
on their own.
"""

from __future__ import annotations

from dataclasses import dataclass

AUTHORIZATION = "G3"
CHANGE = "G4"
EXCEPTION = "M17"

GATE_LABELS = {
    AUTHORIZATION: "S7 · Gate G3 · Deployment authorization",
    CHANGE: "S9 · Gate G4 · Material change",
    EXCEPTION: "M17 · Exception",
}

#: Exceptions are time-boxed at every tier, and Tier 4 ones cannot be renewed:
#: the system goes back through G3 instead.
MAX_EXCEPTION_DAYS = 90
NO_RENEWAL_FROM_TIER = 4
#: Approvers are warned this long before an exception lapses.
EXCEPTION_WARNING_DAYS = 7

#: How recent a passing evaluation must be to count as assurance at G3.
ASSURANCE_MAX_AGE_DAYS = {2: 365, 3: 180, 4: 180}

#: From this tier, signers must come from outside the system's delivery line.
INDEPENDENT_FROM_TIER = 3


@dataclass(frozen=True)
class Seat:
    key: str
    label: str
    held_by: str


OWNER = Seat("owner", "System owner", "the system's named accountable owner")
RISK_LEAD = Seat("risk_lead", "Risk and Compliance Lead", "a holder of risk.manage")
BODY = Seat("body", "AI Governance Body", "a holder of gate.decide")
SPONSOR = Seat("sponsor", "Enterprise Governance Sponsor", "a holder of gate.sponsor")

#: The signatures each decision needs, by gate and tier. Each entry is one
#: signature, listing the seats that may give it; every signature must come from
#: a different person. Tier 4 is IRGF's dual authorization.
DECISION_RIGHTS: dict[str, dict[int, tuple[tuple[Seat, ...], ...]]] = {
    AUTHORIZATION: {
        1: ((OWNER, BODY),),
        2: ((OWNER, BODY),),
        3: ((BODY,),),
        4: ((BODY,), (SPONSOR,)),
    },
    CHANGE: {
        1: ((RISK_LEAD, BODY),),
        2: ((RISK_LEAD, BODY),),
        3: ((BODY,),),
        4: ((BODY,), (SPONSOR,)),
    },
    EXCEPTION: {
        1: ((RISK_LEAD, BODY),),
        2: ((RISK_LEAD, BODY),),
        3: ((BODY,),),
        4: ((BODY,), (SPONSOR,)),
    },
}


def rights(gate: str, tier: int) -> tuple[tuple[Seat, ...], ...]:
    tiers = DECISION_RIGHTS[gate]
    return tiers.get(tier) or tiers[max(tiers)]


def describe_rights(gate: str, tier: int) -> str:
    slots = rights(gate, tier)
    parts = [" or ".join(seat.label for seat in seats) for seats in slots]
    text = ", then ".join(parts)
    return f"{text} (two different people)" if len(slots) > 1 else text


# -- The change table (S9 · Gate G4) -------------------------------------------

COSMETIC = "cosmetic"
MINOR = "minor"
MATERIAL = "material"
MAJOR = "major"
CLASSES = (COSMETIC, MINOR, MATERIAL, MAJOR)
#: Classes that must pass Gate G4.
GATED = (MATERIAL, MAJOR)

CHANGE_TYPES = {
    "model_version": "Model version, same provider and family",
    "model_family": "Model family or provider",
    "fallback_removed": "Fallback model removed",
    "autonomy_increase": "Autonomy increased",
    "autonomy_decrease": "Autonomy decreased",
    "oversight_removed": "Human approval removed",
    "oversight_added": "Human approval added",
    "tool_added": "Tool or permission added",
    "tool_removed": "Tool or permission removed",
    "grounding_added": "Grounding source added or data sensitivity raised",
    "grounding_removed": "Grounding source removed or data sensitivity lowered",
    "prompt": "System prompt or instruction",
    "limits_raised": "Action limits raised or removed",
    "limits_lowered": "Action limits tightened",
    "kill_switch": "Kill-switch conditions",
    "identity": "Identity bindings",
    "population": "Environment, and so the affected population",
    "tier_increase": "Risk tier raised",
    "tier_decrease": "Risk tier lowered",
    # Systems of several agents (IRGF chapter 16).
    "model_removed": "Model or provider removed",
    "agent_added": "Agent added to the system",
    "agent_removed": "Agent removed from the system",
    "delegation_added": "Delegation added between agents",
    "delegation_removed": "Delegation removed",
}

#: Types whose automatic class a person may lower, with a reason. At Tier 3-4
#: a second person must confirm the lower class.
RECLASSIFIABLE = frozenset({"prompt", "grounding_added", "kill_switch", "identity", "model_version"})


@dataclass(frozen=True)
class ChangeClass:
    classification: str
    reason: str
    #: IRGF re-enters S2 (re-score) and G2 (confirmed tier) before G4.
    requires_rescore: bool = False
    #: G4 is not enough: the system goes back through G3.
    requires_reauthorization: bool = False


def classify(change_type: str, tier: int) -> ChangeClass:
    """IRGF's change-type table, applied to one change at the system's tier."""
    high = tier >= 3
    if change_type == "model_version":
        return ChangeClass(
            MATERIAL if high else MINOR,
            "Same provider and family: minor at Tier 1-2, material at Tier 3-4.",
        )
    if change_type == "model_family":
        return ChangeClass(MAJOR, "A new model family or provider is a major change (G2 and G4).", True)
    if change_type in ("autonomy_increase",):
        return ChangeClass(MAJOR, "Any autonomy increase is major (S2, G2 and G4).", True)
    if change_type == "oversight_removed":
        return ChangeClass(MAJOR, "Removing a human checkpoint widens the boundary: major.", True)
    if change_type == "limits_raised":
        return ChangeClass(MAJOR, "Raising or removing an action limit widens the boundary: major.", True)
    if change_type == "tool_added":
        if high:
            return ChangeClass(MAJOR, "A new tool or permission at Tier 3-4 is major (S2, G2 and G4).", True)
        return ChangeClass(MATERIAL, "A new tool or permission at Tier 1-2 is material (G4).")
    if change_type in ("agent_added", "delegation_added"):
        # IRGF chapter 16: the composite boundary is what a system can reach
        # through all its agents, so widening it is treated like a new tool.
        what = "An agent added to the system" if change_type == "agent_added" else "A new delegation between agents"
        if high:
            return ChangeClass(MAJOR, f"{what} widens the composite boundary at Tier 3-4: major (S2, G2 and G4).", True)
        return ChangeClass(MATERIAL, f"{what} widens the composite boundary: material at Tier 1-2 (G4).")
    if change_type == "grounding_added":
        return ChangeClass(MATERIAL, "A new grounding source, or more sensitive data, is material (G4).")
    if change_type == "prompt":
        return ChangeClass(
            MATERIAL,
            "A substantive prompt change is material. If it is only wording, it can be reclassified "
            "as cosmetic, with a second person confirming at Tier 3-4.",
        )
    if change_type == "kill_switch":
        return ChangeClass(MATERIAL, "The pre-approved kill-switch conditions changed: material (G4).")
    if change_type == "identity":
        return ChangeClass(MATERIAL, "The identities allowed to act as the agent changed: material (G4).")
    if change_type == "population":
        return ChangeClass(
            MAJOR, "A different environment means a different affected population: S2 and G3.",
            True, True,
        )
    if change_type == "tier_increase":
        return ChangeClass(
            MAJOR, "A higher tier needs the higher tier's decision authority: re-authorize at G3.",
            False, True,
        )
    # Everything that only narrows what the agent can do.
    return ChangeClass(MINOR, "Narrows the approved boundary: minor, recorded without a gate.")
