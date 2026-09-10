"""Risk services, including the 5x5 heat map (section 17)."""

from __future__ import annotations

from django.db.models import Count

from apps.core.constants import RiskLevel
from apps.risks.models import OPEN_STATUSES, SCALE, Risk, RiskCategory, level_for_score


class RiskService:
    """Register analytics for the risk dashboard."""

    def __init__(self, organization):
        self.organization = organization

    @property
    def risks(self):
        return Risk.objects.for_organization(self.organization)

    @property
    def open_risks(self):
        return self.risks.filter(status__in=OPEN_STATUSES)

    def summary(self) -> dict:
        open_risks = list(self.open_risks)
        by_level: dict[str, int] = {level: 0 for level, _ in RiskLevel.choices}
        for risk in open_risks:
            by_level[risk.effective_level] = by_level.get(risk.effective_level, 0) + 1

        mitigated = [r for r in open_risks if r.residual_risk_score]
        return {
            "total": self.risks.count(),
            "open": len(open_risks),
            "critical": by_level.get(RiskLevel.CRITICAL, 0),
            "high": by_level.get(RiskLevel.HIGH, 0),
            "by_level": by_level,
            "unmitigated": len([r for r in open_risks if not r.mitigation.strip()]),
            "average_reduction": (
                round(sum(r.reduction for r in mitigated) / len(mitigated), 1) if mitigated else 0
            ),
        }

    def heat_map(self, residual: bool = False) -> list[list[dict]]:
        """A 5x5 grid, impact descending down the rows.

        Each cell carries its own score, level and the risks that land in it,
        so the template can render the matrix without recomputing anything.
        """
        likelihood_field = "residual_likelihood" if residual else "likelihood"
        impact_field = "residual_impact" if residual else "impact"

        buckets: dict[tuple[int, int], list[Risk]] = {}
        for risk in self.open_risks.select_related("agent", "application"):
            likelihood = getattr(risk, likelihood_field)
            impact = getattr(risk, impact_field)
            if not likelihood or not impact:
                continue
            buckets.setdefault((likelihood, impact), []).append(risk)

        grid = []
        for impact in reversed(SCALE):
            row = []
            for likelihood in SCALE:
                cell_risks = buckets.get((likelihood, impact), [])
                score = likelihood * impact
                row.append(
                    {
                        "likelihood": likelihood,
                        "impact": impact,
                        "score": score,
                        "level": level_for_score(score),
                        "count": len(cell_risks),
                        "risks": cell_risks[:6],
                    }
                )
            grid.append(row)
        return grid

    def by_category(self) -> list[dict]:
        labels = dict(RiskCategory.choices)
        rows = (
            self.open_risks.values("risk_category")
            .annotate(total=Count("id"))
            .order_by("-total")
        )
        return [
            {"label": labels.get(row["risk_category"], row["risk_category"]), "count": row["total"]}
            for row in rows
        ]

    def level_distribution(self) -> dict:
        summary = self.summary()["by_level"]
        labels = dict(RiskLevel.choices)
        return {
            "labels": [labels[level] for level, _ in RiskLevel.choices],
            "values": [summary.get(level, 0) for level, _ in RiskLevel.choices],
        }
