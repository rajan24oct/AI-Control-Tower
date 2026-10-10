"""The regulatory and standards catalog: requirements, and the evidence that answers them.

Each requirement names the platform evidence checks that speak to it (see
`apps.compliance.evidence`). Descriptions are short summaries for orientation,
not the text of the law or standard: the mapping is indicative, and whether a
system is in scope -- above all, whether it is high-risk under the EU AI Act --
is a judgement people record per system, with a rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DISCLAIMER = (
    "Indicative mapping for evidence gathering, not legal advice. Requirement summaries are paraphrased; "
    "read the regulation or standard itself, and have scope decisions confirmed by your legal and compliance teams."
)


@dataclass(frozen=True)
class Requirement:
    code: str
    reference: str
    title: str
    summary: str
    checks: tuple[str, ...] = ()
    #: Evidence only a person can give (training records, an impact assessment).
    attestation: str = ""
    #: EU AI Act: the risk categories the requirement applies to. Empty: every system in scope.
    categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class Regime:
    key: str
    name: str
    short: str
    url: str
    description: str
    requirements: tuple[Requirement, ...] = field(default_factory=tuple)
    #: Whether in-scope is the default (voluntary standards), or must be decided.
    default_applies: bool = True


HIGH = ("high_risk",)
HIGH_OR_LIMITED = ("high_risk", "limited")

EU_AI_ACT = Regime(
    key="eu_ai_act",
    name="EU AI Act (Regulation (EU) 2024/1689)",
    short="EU AI Act",
    url="https://eur-lex.europa.eu/eli/reg/2024/1689/oj",
    description="Risk-based obligations for AI systems placed on the EU market or used in the EU. Most "
                "obligations below apply to high-risk systems; transparency duties apply more widely.",
    default_applies=False,
    requirements=(
        Requirement("EU-AIA-4", "Article 4", "AI literacy",
                    "Staff who operate or use AI systems have sufficient AI literacy for their role.",
                    attestation="Record the training or briefing given to the people operating this system."),
        Requirement("EU-AIA-9", "Article 9", "Risk management system",
                    "A continuous process to identify, analyse, evaluate and treat the system's risks.",
                    ("classification", "risks"), categories=HIGH),
        Requirement("EU-AIA-10", "Article 10", "Data and data governance",
                    "Data used by the system is governed: relevant, examined for bias, with its provenance known.",
                    ("data_governance",), categories=HIGH),
        Requirement("EU-AIA-11", "Article 11", "Technical documentation",
                    "Documentation describing the system, its purpose, design and controls is kept up to date.",
                    ("documentation", "authorization"), categories=HIGH),
        Requirement("EU-AIA-12", "Article 12", "Record-keeping",
                    "The system automatically records events (logs) over its lifetime, enabling traceability.",
                    ("record_keeping",), categories=HIGH),
        Requirement("EU-AIA-13", "Article 13", "Transparency and information to deployers",
                    "The system comes with information on its intended purpose, capabilities and limits.",
                    ("transparency",), categories=HIGH),
        Requirement("EU-AIA-14", "Article 14", "Human oversight",
                    "People can effectively oversee the system, understand its output, and intervene or stop it.",
                    ("human_oversight", "kill_switch"), categories=HIGH),
        Requirement("EU-AIA-15", "Article 15", "Accuracy, robustness and cybersecurity",
                    "The system achieves appropriate accuracy and robustness, tested and declared.",
                    ("evaluation",), categories=HIGH),
        Requirement("EU-AIA-26", "Article 26", "Obligations of deployers",
                    "Use per instructions, assign competent human oversight, monitor operation, keep logs.",
                    ("accountability", "monitoring"), categories=HIGH),
        Requirement("EU-AIA-27", "Article 27", "Fundamental rights impact assessment",
                    "Certain deployers assess the impact on fundamental rights before first use.",
                    attestation="Attach or reference the fundamental rights impact assessment, or record why it "
                                "does not apply to this deployer.", categories=HIGH),
        Requirement("EU-AIA-50", "Article 50", "Transparency for certain AI systems",
                    "People are told when they interact with an AI system, and AI-generated content is marked.",
                    ("transparency",), categories=HIGH_OR_LIMITED),
        Requirement("EU-AIA-72", "Article 72", "Post-market monitoring",
                    "Performance is monitored in operation, and changes are controlled.",
                    ("monitoring", "change_control"), categories=HIGH),
        Requirement("EU-AIA-73", "Article 73", "Reporting of serious incidents",
                    "Serious incidents are identified, handled and reported to authorities within deadlines.",
                    ("incidents",), categories=HIGH),
    ),
)

ISO_42001 = Regime(
    key="iso_42001",
    name="ISO/IEC 42001:2023 · AI management system",
    short="ISO/IEC 42001",
    url="https://www.iso.org/standard/81230.html",
    description="An AI management system standard: policy, risk and impact assessment, lifecycle and "
                "operational controls, and performance evaluation.",
    requirements=(
        Requirement("ISO42001-5.2", "Clause 5.2", "AI policy",
                    "An AI policy is established and applied to AI systems.", ("policies",)),
        Requirement("ISO42001-6.1.2", "Clause 6.1.2", "AI risk assessment",
                    "AI risks are assessed against defined criteria.", ("classification", "risks")),
        Requirement("ISO42001-6.1.4", "Clause 6.1.4", "AI system impact assessment",
                    "The consequences of the system for individuals and society are assessed.",
                    ("classification",)),
        Requirement("ISO42001-A.6", "Annex A.6", "AI system life cycle",
                    "The life cycle is defined and controlled: requirements, verification, deployment, change.",
                    ("authorization", "change_control")),
        Requirement("ISO42001-A.7", "Annex A.7", "Data for AI systems",
                    "Data for AI systems is managed: provenance, quality and preparation.", ("data_governance",)),
        Requirement("ISO42001-A.9", "Annex A.9", "Use of AI systems",
                    "AI systems are used responsibly, with human oversight and defined accountability.",
                    ("human_oversight", "accountability")),
        Requirement("ISO42001-A.10", "Annex A.10", "Third-party and customer relationships",
                    "Responsibilities with suppliers of models and services are managed.", ("third_party",)),
        Requirement("ISO42001-9.1", "Clause 9.1", "Monitoring, measurement, analysis and evaluation",
                    "The AI system's performance is monitored and evaluated.", ("monitoring", "evaluation")),
    ),
)

NIST_AI_RMF = Regime(
    key="nist_ai_rmf",
    name="NIST AI Risk Management Framework 1.0",
    short="NIST AI RMF",
    url="https://www.nist.gov/itl/ai-risk-management-framework",
    description="A voluntary framework organised in four functions: Govern, Map, Measure and Manage.",
    requirements=(
        Requirement("NIST-GOVERN", "GOVERN", "Govern",
                    "Policies, accountability and a risk-aware culture are in place across the lifecycle.",
                    ("policies", "accountability")),
        Requirement("NIST-MAP", "MAP", "Map",
                    "Context is established and the system's risks are identified and categorised.",
                    ("classification", "transparency")),
        Requirement("NIST-MEASURE", "MEASURE", "Measure",
                    "Risks are analysed, assessed and tracked with appropriate methods and metrics.",
                    ("evaluation", "monitoring")),
        Requirement("NIST-MANAGE", "MANAGE", "Manage",
                    "Risks are prioritised and acted on; incidents are responded to and recovered from.",
                    ("risks", "incidents", "kill_switch")),
    ),
)

REGIMES = {regime.key: regime for regime in (EU_AI_ACT, ISO_42001, NIST_AI_RMF)}
REQUIREMENTS = {req.code: req for regime in REGIMES.values() for req in regime.requirements}


def regime_of(code: str) -> Regime | None:
    for regime in REGIMES.values():
        if any(req.code == code for req in regime.requirements):
            return regime
    return None
