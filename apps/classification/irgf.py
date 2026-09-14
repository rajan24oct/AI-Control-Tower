"""IRGF risk classification: the five dimensions and the tiering rule.

Implements two modules of the Intent-to-Runtime Governance Framework (IRGF
v1.0, https://www.irgframework.org/): M4, which scores a system on five risk
dimensions, and M5, which resolves those scores into one of four tiers. The
record they produce is the output of lifecycle stage S2: provisional scores are
what Gate G1 asks for, and a countersigned (confirmed) tier is what Gate G2
requires.

Kept free of Django, so the rule can be tested and reused on its own.
"""

from __future__ import annotations

from dataclasses import dataclass

STAGE = "S2"
STAGE_LABEL = "S2 · Risk classification"
#: Gate G1 takes provisional D1–D5; Gate G2 needs the tier confirmed.
PROVISIONAL_GATE = "G1"
CONFIRMED_GATE = "G2"

SCORES = (1, 2, 3, 4)
TIERS = (1, 2, 3, 4)

#: Tiers from which IRGF expects an annual re-score even without a trigger.
ANNUAL_REVIEW_FROM_TIER = 3
REVIEW_INTERVAL_DAYS = 365

#: The safety-override floor: a system meeting either condition is never below
#: this tier, whatever the matrix says.
FLOOR_TIER = 3


@dataclass(frozen=True)
class Dimension:
    code: str
    field: str
    name: str
    question: str
    #: What a score of 1, 2, 3 and 4 means, in that order.
    anchors: tuple[str, str, str, str]
    rule: str = ""

    def anchor(self, score: int) -> str:
        return self.anchors[score - 1] if score in SCORES else ""


D1 = Dimension(
    "D1", "d1_consequence", "Decision consequence severity",
    "How bad is it if the output is wrong and someone acts on it?",
    (
        "Minutes wasted; nobody outside the team affected",
        "Rework or minor cost, recoverable in normal operations",
        "Material loss, a customer disadvantaged, or regulatory concern",
        "Harm to people, a right or benefit denied, or major legal exposure",
    ),
    "Score the cost of being wrong, assuming the output is acted on.",
)
D2 = Dimension(
    "D2", "d2_autonomy", "Autonomy",
    "How much human judgement sits between the output and its effect?",
    (
        "A person makes every decision; the system only informs",
        "The system proposes; a person approves each action first",
        "The system acts; a person monitors and can step in soon after",
        "The system acts with no human review; the effect is immediate",
    ),
    "Score the highest-autonomy path, not the reviewed one.",
)
D3 = Dimension(
    "D3", "d3_reversibility", "Reversibility deficit",
    "How hard is it to undo a wrong outcome in the world?",
    (
        "Undone at once at no cost; nothing leaves the system",
        "Undoing takes process, cost or delay, but restores the prior state",
        "Some consequences can be remedied; others cannot",
        "The consequence stands: money moved, information disclosed, a decision told",
    ),
    "Judge the consequence in the world, not whether system state can be rolled back.",
)
D4 = Dimension(
    "D4", "d4_exposure", "Exposure and scale",
    "How many are affected, how far, and can the effect cascade?",
    (
        "One user or a small team; nothing downstream",
        "One business function's internal users; effects stay inside",
        "Enterprise-wide, or a limited and defined external population",
        "A large external population, or feeds systems that spread the effect",
    ),
    "Count the reach of the consequence, not the list of users who log in.",
)
D5_DATA = Dimension(
    "D5", "d5_data_sensitivity", "Data sensitivity",
    "How sensitive is the data the system reads or writes?",
    (
        "Public, or non-sensitive internal data",
        "Internal confidential data with no personal data",
        "Personal, commercially sensitive or regulated data",
        "Special-category data, or data under legal restriction",
    ),
)
D5_MODEL = Dimension(
    "D5", "d5_model_uncertainty", "Model uncertainty",
    "How well understood is the model's behaviour?",
    (
        "Deterministic, interpretable and stable",
        "Understood, with documented and predictable failure modes",
        "Generative or complex; failure modes known only in general",
        "Opaque reasoning that produces confident errors",
    ),
)

#: The four stand-alone dimensions. D5 is two sub-scores; the composite is the
#: higher of the two, never their average.
DIMENSIONS = (D1, D2, D3, D4)
D5_PARTS = (D5_DATA, D5_MODEL)
ALL_FIELDS = tuple(d.field for d in (*DIMENSIONS, *D5_PARTS))

#: M5's matrix, indexed [impact][control deficit].
TIER_MATRIX = {
    1: {1: 1, 2: 1, 3: 2, 4: 2},
    2: {1: 1, 2: 2, 3: 2, 4: 3},
    3: {1: 2, 2: 2, 3: 3, 4: 4},
    4: {1: 3, 2: 3, 3: 4, 4: 4},
}

#: What changes as the tier rises. The gates themselves never change; evidence
#: depth, decision authority, monitoring cadence and automation do.
TIER_SUMMARY = {
    1: "Lightest evidence: gates are self-certified and drift arrives as a digest.",
    2: "Adds data lineage, a light assurance summary and an authorization record.",
    3: "Adds independent review, full assurance, a control matrix and an annual re-score.",
    4: "Adds preventive controls for standing-authority agents, a pre-approved kill-switch "
       "and dual authorization.",
}

#: Card and badge tone per tier, matching the Control Tower's risk colours.
TIER_TONE = {1: "low", 2: "moderate", 3: "high", 4: "critical"}

#: Suggested D2 from the platform's autonomy ladder (0 advisory … 4 autonomous).
#: A suggestion only: IRGF asks for the highest-autonomy path, which the scorer
#: may know to be higher than the configured level.
AUTONOMY_TO_D2 = {0: 1, 1: 1, 2: 2, 3: 3, 4: 4}

#: Suggested D5 data-sensitivity score from the platform's data classification.
CLASSIFICATION_TO_D5_DATA = {"public": 1, "internal": 2, "confidential": 3, "restricted": 4}

#: Generative models have known failure modes but are not fully understood.
DEFAULT_D5_MODEL = 3


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


@dataclass(frozen=True)
class TierResult:
    d5: int
    impact: int
    control_deficit: int
    matrix_tier: int
    tier: int
    floor_applied: bool

    @property
    def tone(self) -> str:
        return TIER_TONE[self.tier]


def _check(name: str, value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value not in SCORES:
        raise ValueError(f"{name} must be a whole number from 1 to 4, not {value!r}.")
    return value


def compute(d1: int, d2: int, d3: int, d4: int, d5_data: int, d5_model: int) -> TierResult:
    """Resolve the five dimensions into a tier.

    Impact is the rounded-up average of D1, D4 and D5; control deficit is the
    rounded-up average of D2 and D3. The two axes are kept apart rather than
    averaged together, so a catastrophic factor cannot be diluted by calm ones.
    """
    d1, d2, d3, d4 = (_check(n, v) for n, v in (("D1", d1), ("D2", d2), ("D3", d3), ("D4", d4)))
    d5 = max(_check("D5 data sensitivity", d5_data), _check("D5 model uncertainty", d5_model))

    impact = _ceil_div(d1 + d4 + d5, 3)
    control_deficit = _ceil_div(d2 + d3, 2)
    matrix_tier = TIER_MATRIX[impact][control_deficit]

    floor_applies = d1 == 4 or (d2 == 4 and d3 == 4)
    tier = max(matrix_tier, FLOOR_TIER) if floor_applies else matrix_tier
    return TierResult(
        d5=d5,
        impact=impact,
        control_deficit=control_deficit,
        matrix_tier=matrix_tier,
        tier=tier,
        floor_applied=floor_applies and tier > matrix_tier,
    )


def floor_applies(d1: int, d2: int, d3: int) -> bool:
    """Whether the safety-override floor binds, independent of the matrix."""
    return d1 == 4 or (d2 == 4 and d3 == 4)
