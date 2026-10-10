"""Assessing a system against each regime, and the organization's overview."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from apps.compliance.evidence import CHECKS, MET, MISSING, NOT_APPLICABLE, PARTIAL, STATUS_LABELS, STATUS_STYLES, Subject
from apps.compliance.frameworks import REGIMES, Regime, Requirement
from apps.compliance.models import Applicability, Attestation, EUCategory, RegulatoryScope
from apps.core.constants import LifecycleStatus

#: EU AI Act categories whose obligations are worked through.
EU_ASSESSED = (EUCategory.HIGH_RISK, EUCategory.LIMITED, EUCategory.TO_ASSESS, EUCategory.PROHIBITED)


@dataclass
class RequirementResult:
    requirement: Requirement
    status: str
    checks: list = field(default_factory=list)
    attestation: Attestation | None = None
    note: str = ""

    @property
    def label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def style(self) -> str:
        return STATUS_STYLES[self.status]


@dataclass
class RegimeAssessment:
    regime: Regime
    scope: RegulatoryScope
    applies: bool
    results: list[RequirementResult]
    suggestion: str = ""

    @property
    def counts(self) -> Counter:
        return Counter(result.status for result in self.results)

    @property
    def applicable(self) -> int:
        return sum(1 for result in self.results if result.status != NOT_APPLICABLE)

    @property
    def coverage(self) -> int:
        """Met counts in full, partly met as half: the share of the way there."""
        if not self.applicable:
            return 100
        counts = self.counts
        return round((counts[MET] + counts[PARTIAL] * 0.5) / self.applicable * 100)

    @property
    def category(self) -> str:
        return self.scope.eu_category if self.regime.key == "eu_ai_act" else ""


def scope_for(subject: Subject, regime: Regime) -> RegulatoryScope:
    scope = RegulatoryScope.objects.filter(**subject.filter, regime=regime.key).first()
    if scope is not None:
        return scope
    if regime.key == "eu_ai_act":
        return RegulatoryScope(organization=subject.organization, **subject.filter, regime=regime.key,
                               applicability=Applicability.APPLIES, eu_category=EUCategory.TO_ASSESS)
    return RegulatoryScope(organization=subject.organization, **subject.filter, regime=regime.key,
                           applicability=Applicability.APPLIES if regime.default_applies else Applicability.DOES_NOT_APPLY)


def eu_suggestion(subject: Subject) -> str:
    """A prompt for the person deciding the category, never the decision itself."""
    tier = subject.tier
    if tier and tier >= 3:
        return (f"IRGF Tier {tier}. Likely high-risk if used in an Annex III area, such as creditworthiness, life "
                "and health insurance pricing, employment or access to essential services. Confirm with legal.")
    if tier:
        return (f"IRGF Tier {tier}. Likely limited or minimal risk; transparency duties (Article 50) apply if it "
                "interacts with people or generates content. Confirm with legal.")
    return "Classify the system at S2 first; its tier helps decide the category."


def _combine(statuses: list[str]) -> str:
    if not statuses:
        return MISSING
    if all(status == MET for status in statuses):
        return MET
    if all(status == MISSING for status in statuses):
        return MISSING
    return PARTIAL


def assess(subject: Subject, regimes=None) -> list[RegimeAssessment]:
    checks: dict = {}
    attestations = {}
    for attestation in Attestation.objects.filter(**subject.filter).order_by("attested_at"):
        if attestation.is_valid:
            attestations[attestation.requirement] = attestation  # the latest valid one wins

    assessments = []
    for regime in (regimes or REGIMES.values()):
        scope = scope_for(subject, regime)
        if regime.key == "eu_ai_act":
            applies = scope.eu_category in EU_ASSESSED
        else:
            applies = scope.applicability == Applicability.APPLIES
        results = []
        for requirement in regime.requirements:
            if not applies:
                results.append(RequirementResult(requirement, NOT_APPLICABLE, note="The regime does not apply."))
                continue
            if (regime.key == "eu_ai_act" and requirement.categories
                    and scope.eu_category not in requirement.categories
                    and scope.eu_category not in (EUCategory.TO_ASSESS, EUCategory.PROHIBITED)):
                results.append(RequirementResult(requirement, NOT_APPLICABLE,
                                                 note=f"Applies to {', '.join(requirement.categories).replace('_', '-')} systems."))
                continue
            attestation = attestations.get(requirement.code)
            if attestation is not None:
                status = MET if attestation.status == "met" else NOT_APPLICABLE
                results.append(RequirementResult(requirement, status, attestation=attestation,
                                                 note="Attested by a person."))
                continue
            results_for = []
            for key in requirement.checks:
                if key not in checks:
                    checks[key] = CHECKS[key](subject)
                results_for.append(checks[key])
            if requirement.checks:
                results.append(RequirementResult(requirement, _combine([c.status for c in results_for]), results_for))
            else:
                results.append(RequirementResult(requirement, MISSING, note="Needs an attestation."))
        assessments.append(RegimeAssessment(regime, scope, applies, results,
                                            eu_suggestion(subject) if regime.key == "eu_ai_act" else ""))
    return assessments


def systems(organization) -> list[Subject]:
    from apps.agents.models import AIAgent
    from apps.applications_registry.models import AIApplication

    live = [LifecycleStatus.ACTIVE, LifecycleStatus.PENDING_REVIEW, LifecycleStatus.DRAFT]
    return ([Subject(app) for app in AIApplication.objects.for_organization(organization).filter(status__in=live)
             .select_related("business_owner", "technical_owner")]
            + [Subject(agent) for agent in AIAgent.objects.for_organization(organization).filter(status__in=live)
               .select_related("owner", "provider", "primary_model")])


def overview(organization) -> dict:
    rows, gaps = [], Counter()
    for subject in systems(organization):
        assessments = assess(subject)
        rows.append({"subject": subject, "assessments": assessments})
        for assessment in assessments:
            for result in assessment.results:
                if result.status in (MISSING, PARTIAL):
                    gaps[(assessment.regime.short, result.requirement.reference, result.requirement.title,
                          result.status)] += 1
    totals = {}
    for regime in REGIMES.values():
        in_scope = [a for row in rows for a in row["assessments"] if a.regime.key == regime.key and a.applies]
        totals[regime.key] = {
            "regime": regime,
            "systems": len(in_scope),
            "coverage": round(sum(a.coverage for a in in_scope) / len(in_scope)) if in_scope else None,
        }
    eu_categories = Counter(
        a.scope.eu_category for row in rows for a in row["assessments"] if a.regime.key == "eu_ai_act"
    )
    return {
        "rows": rows,
        "totals": totals,
        "eu_categories": {EUCategory(key).label: count for key, count in eu_categories.most_common()},
        "gaps": [
            {"regime": regime, "reference": reference, "title": title, "status": status, "systems": count}
            for (regime, reference, title, status), count in gaps.most_common(10)
        ],
    }
