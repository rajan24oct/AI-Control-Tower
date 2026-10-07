"""IRGF's Control Plane (M12) and drift taxonomy (M13), as data.

The Control Plane observes what each agent actually does -- the models that
serve it, the tools it reaches, the identity it acts under, the environment it
runs in -- and compares that running state with the configuration Gate G3
authorized. Each divergence becomes a drift record in one of IRGF's eight
categories, routed and timed by category and tier (IRGF chapter 19).

Agent chain link 06 limits are enforced before an action; link 07 verifies
every action after it, within a latency ceiling set per tier. IRGF names the
ceiling but leaves its values to the organization: the defaults below are the
platform's, and settings can change them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from django.conf import settings
from django.utils import timezone

CONTROL_PLANE = "M12"
DRIFT = "M13"

# -- The eight drift categories (M13) -------------------------------------------

STRUCTURAL = "structural"
CONFIGURATION = "configuration"
INTEGRATION = "integration"
SECURITY = "security"
DATA_LINEAGE = "data_lineage"
BEHAVIORAL = "behavioral"
POLICY = "policy_regulatory"
COST = "cost"


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    meaning: str
    owner: str


CATEGORIES = {
    category.key: category
    for category in (
        Category(STRUCTURAL, "Structural",
                 "What runs differs from the registry and the approvals behind it.", "Architect"),
        Category(CONFIGURATION, "Configuration",
                 "An approved component runs in a configuration no decision covers.", "System owner"),
        Category(INTEGRATION, "Integration and dependency",
                 "What it connects to or depends on differs from what was approved.", "Architect"),
        Category(SECURITY, "Security",
                 "Its identity, permissions or boundary differ from the approved baseline.",
                 "Security architect"),
        Category(DATA_LINEAGE, "Data lineage and governance",
                 "The data it reaches differs from what was approved.", "Data owner"),
        Category(BEHAVIORAL, "AI-model behavioral",
                 "The model behaves differently: a vendor update or a shift in what it sees.",
                 "ML engineering with Risk"),
        Category(POLICY, "Policy and regulatory",
                 "A policy or regulation that applies to it changed, whatever the system did.",
                 "Risk and Compliance"),
        Category(COST, "Cost", "Spend diverges from the approved budget.", "System owner"),
    )
}

# -- Response windows (IRGF chapter 19) ------------------------------------------

OPERATIONAL = "operational"
PROTECTIVE = "protective"

GROUPS = {
    STRUCTURAL: OPERATIONAL, CONFIGURATION: OPERATIONAL, INTEGRATION: OPERATIONAL, COST: OPERATIONAL,
    SECURITY: PROTECTIVE, DATA_LINEAGE: PROTECTIVE,
    BEHAVIORAL: BEHAVIORAL,
    POLICY: POLICY,
}
GROUP_LABELS = {
    OPERATIONAL: "Structural, configuration, integration, cost",
    PROTECTIVE: "Security, data lineage",
    BEHAVIORAL: "AI-model behavioral",
    POLICY: "Policy and regulatory",
}

DIGEST = "digest"
DIRECT = "direct"
IMMEDIATE = "immediate"
PERIODIC = "periodic"
BATCH = "batch"

ROUTE_LABELS = {
    DIGEST: "Digest to the owner",
    DIRECT: "Direct to the owner",
    IMMEDIATE: "Immediate",
    PERIODIC: "Periodic review",
    BATCH: "Batch review",
}
#: Routes that tell nobody at once: the record waits for the digest or a review.
QUIET_ROUTES = frozenset({DIGEST, PERIODIC, BATCH})

RESCORE = "rescore"
RESCORE_G4 = "rescore_g4"
ASSURANCE_RERUN = "assurance_rerun"
COMPLIANCE_REVIEW = "compliance_review"

ACTION_LABELS = {
    RESCORE: "triggers an S2 re-score",
    RESCORE_G4: "triggers an S2 re-score, and possibly Gate G4",
    ASSURANCE_RERUN: "re-run the compliance assurance",
    COMPLIANCE_REVIEW: "a compliance review",
}


@dataclass(frozen=True)
class Response:
    route: str
    working_days: int | None = None
    same_day: bool = False
    action: str = ""

    @property
    def label(self) -> str:
        parts = [ROUTE_LABELS[self.route]]
        if self.same_day:
            parts.append("same business day")
        elif self.working_days:
            parts.append(f"{self.working_days} working days")
        if self.action:
            parts.append(ACTION_LABELS[self.action])
        return ", ".join(parts)

    @property
    def is_quiet(self) -> bool:
        return self.route in QUIET_ROUTES


RESPONSES = {
    (OPERATIONAL, 1): Response(DIGEST),
    (OPERATIONAL, 2): Response(DIRECT),
    (OPERATIONAL, 3): Response(DIRECT, working_days=5),
    (OPERATIONAL, 4): Response(DIRECT, same_day=True),
    (PROTECTIVE, 1): Response(DIRECT),
    (PROTECTIVE, 2): Response(DIRECT, working_days=10),
    (PROTECTIVE, 3): Response(DIRECT, working_days=5),
    (PROTECTIVE, 4): Response(IMMEDIATE, same_day=True),
    (BEHAVIORAL, 1): Response(PERIODIC),
    (BEHAVIORAL, 2): Response(DIRECT),
    (BEHAVIORAL, 3): Response(DIRECT, action=RESCORE),
    (BEHAVIORAL, 4): Response(DIRECT, action=RESCORE_G4),
    (POLICY, 1): Response(BATCH),
    (POLICY, 2): Response(BATCH),
    (POLICY, 3): Response(DIRECT, action=ASSURANCE_RERUN),
    (POLICY, 4): Response(IMMEDIATE, action=COMPLIANCE_REVIEW),
}

#: An agent crossing its boundary is an incident at any tier.
INCIDENT = Response(IMMEDIATE, same_day=True)

#: Platform default: a system with no S2 classification is triaged as Tier 3
#: until it has one, rather than as the lowest tier it might turn out to be.
UNCLASSIFIED_TIER = 3

#: When a "same business day" or a working day ends, in the server's time zone.
BUSINESS_DAY_END = time(18, 0)


def effective_tier(tier) -> int:
    return tier if tier in (1, 2, 3, 4) else UNCLASSIFIED_TIER


def response_for(category: str, tier, *, incident: bool = False) -> Response:
    if incident:
        return INCIDENT
    return RESPONSES[(GROUPS[category], effective_tier(tier))]


def _business_day(day) -> bool:
    return day.weekday() < 5


def due_at(response: Response, detected_at: datetime) -> datetime | None:
    """When the response window closes, or None where IRGF sets no deadline.

    Working days skip weekends; there is no holiday calendar. Something found
    after the business day ends, or at a weekend, is due by the end of the next
    business day.
    """
    if not (response.same_day or response.working_days):
        return None
    local = timezone.localtime(detected_at)
    day = local.date()
    if not _business_day(day) or local.time() >= BUSINESS_DAY_END:
        day += timedelta(days=1)
        while not _business_day(day):
            day += timedelta(days=1)
    for _ in range(response.working_days or 0):
        day += timedelta(days=1)
        while not _business_day(day):
            day += timedelta(days=1)
    return timezone.make_aware(datetime.combine(day, BUSINESS_DAY_END), timezone.get_current_timezone())


# -- What the Control Plane can find -----------------------------------------------

#: How a divergence of each kind is ratified. "change": record the new state on
#: the agent, which Gate G4 classifies and decides. "authorize": Gate G3.
#: "note": nothing on the agent moves, so a reasoned note closes it.
RATIFY_BY_CHANGE = "change"
RATIFY_BY_AUTHORIZATION = "authorize"
RATIFY_BY_NOTE = "note"


@dataclass(frozen=True)
class Signal:
    key: str
    category: str
    label: str
    incident: bool = False
    #: The kill-switch trigger it fires, if the agent pre-approved one (link 06).
    trigger: str = ""
    ratify: str = RATIFY_BY_CHANGE


SIGNALS = {
    signal.key: signal
    for signal in (
        Signal("unauthorized_deployment", STRUCTURAL, "Running in production without a deployment authorization",
               ratify=RATIFY_BY_AUTHORIZATION),
        Signal("environment_mismatch", STRUCTURAL, "Running outside its authorized environment",
               ratify=RATIFY_BY_AUTHORIZATION),
        Signal("acted_while_suspended", CONFIGURATION, "Acting while its authorization is suspended"),
        Signal("agent_outside_system", STRUCTURAL, "Ran with an agent outside its authorized configuration"),
        Signal("undeclared_delegation", INTEGRATION, "Work delegated along a path no authorization declares"),
        Signal("unauthorized_model", CONFIGURATION, "Served by a model outside its authorized configuration"),
        Signal("tool_definition_changed", INTEGRATION, "A tool it depends on changed after review",
               ratify=RATIFY_BY_NOTE),
        Signal("boundary_divergence", SECURITY, "Reached a tool outside its authorized boundary",
               incident=True, trigger="boundary_divergence"),
        Signal("restricted_tool", SECURITY, "Reached a tool its authorization restricts",
               incident=True, trigger="boundary_divergence"),
        Signal("communication_outside_boundary", SECURITY,
               "Sent work to an agent outside its authorized communication boundary",
               incident=True, trigger="boundary_divergence"),
        Signal("action_limit_breach", SECURITY, "Attempted an action beyond its limits",
               incident=True, trigger="action_limit_breach"),
        Signal("action_limit_exceeded", SECURITY, "Completed an action beyond its limits",
               incident=True, trigger="action_limit_breach"),
        Signal("unbound_identity", SECURITY, "Acted under an identity not bound to it",
               trigger="unbound_identity"),
        Signal("data_above_classification", DATA_LINEAGE, "Reached data above its authorized classification"),
        Signal("evaluation_regression", BEHAVIORAL, "An evaluation after authorization did not pass",
               ratify=RATIFY_BY_NOTE),
        Signal("policy_changed", POLICY, "A policy that applies to it changed after authorization",
               ratify=RATIFY_BY_NOTE),
        Signal("budget_exceeded", COST, "Spend passed its approved budget", ratify=RATIFY_BY_NOTE),
    )
}

# -- Verification (link 07) ------------------------------------------------------------

#: Seconds from the platform learning of an action to its verification, by tier.
DEFAULT_VERIFICATION_CEILINGS = {1: 24 * 3600, 2: 3600, 3: 15 * 60, 4: 5 * 60}

#: How far back the sweep looks for actions nobody has verified yet.
LOOKBACK = timedelta(days=7)

#: A reported run is left this long after its last update before the sweep
#: verifies it, so events still arriving are part of what is checked.
SETTLE = timedelta(seconds=30)


def verification_ceiling(tier) -> int:
    configured = getattr(settings, "CONTROL_PLANE", {}).get("VERIFICATION_CEILING_SECONDS", {})
    tier = effective_tier(tier)
    value = configured.get(tier, configured.get(str(tier), DEFAULT_VERIFICATION_CEILINGS[tier]))
    return int(value)


SENSITIVITY = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
