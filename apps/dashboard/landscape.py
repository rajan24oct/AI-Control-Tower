"""The executive AI landscape (sections 32, 33, 34).

Builds the layered view of an organization's AI ecosystem -- applications,
agents, models, providers -- where every node carries its own operational
indicators: health, risk, cost, execution volume and status.

The rollups are computed with one grouped query per layer and then attached to
the inventory records, so the page stays a fixed number of queries no matter
how many assets an organization has.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Count, Q, Sum
from django.urls import reverse
from django.utils import timezone

from apps.agents.models import AIAgent
from apps.applications_registry.models import AIApplication
from apps.approvals.models import ApprovalRequest, ApprovalStatus
from apps.core.constants import HealthStatus, LifecycleStatus, RiskLevel
from apps.costs.models import BudgetAlert
from apps.executions.models import Execution, ExecutionStatusChoices
from apps.models_registry.models import AIModel
from apps.providers.models import AIProvider
from apps.risks.models import OPEN_STATUSES as OPEN_RISK_STATUSES, Risk

WINDOW_DAYS = 30
ZERO = Decimal("0")

FAILED_STATUSES = [ExecutionStatusChoices.FAILED, ExecutionStatusChoices.TIMED_OUT]
LIVE_STATUSES = [
    ExecutionStatusChoices.QUEUED,
    ExecutionStatusChoices.RUNNING,
    ExecutionStatusChoices.WAITING_FOR_APPROVAL,
]

#: Failure-rate bands used to derive health for assets that have no probe of
#: their own. A provider reports its own health; an agent's health has to be
#: inferred from how its runs actually went.
#:
#: Deliberately wider than a typical HTTP SLO: model calls fail for reasons
#: that are routine here -- rate limits, context overflows, tool timeouts. Bands
#: tight enough to flag a 5% failure rate would mark most of the estate
#: degraded and destroy the signal.
DEGRADED_FAILURE_RATE = 0.10
UNHEALTHY_FAILURE_RATE = 0.25

HEALTH_RANK = {
    HealthStatus.UNHEALTHY: 0,
    HealthStatus.DEGRADED: 1,
    HealthStatus.UNKNOWN: 2,
    HealthStatus.HEALTHY: 3,
}


def health_from_failure_rate(runs: int, failed: int) -> str:
    """Derive a health status from recent execution outcomes."""
    if not runs:
        return HealthStatus.UNKNOWN
    rate = failed / runs
    if rate >= UNHEALTHY_FAILURE_RATE:
        return HealthStatus.UNHEALTHY
    if rate >= DEGRADED_FAILURE_RATE:
        return HealthStatus.DEGRADED
    return HealthStatus.HEALTHY


@dataclass
class Node:
    """One asset on the landscape, with everything an executive needs at a glance."""

    id: str
    name: str
    url: str
    subtitle: str = ""
    status: str = LifecycleStatus.ACTIVE
    status_label: str = ""
    health: str = HealthStatus.UNKNOWN
    risk_level: str = RiskLevel.LOW

    runs: int = 0
    failed: int = 0
    live: int = 0
    cost: Decimal = ZERO
    tokens: int = 0

    open_risks: int = 0
    pending_approvals: int = 0
    #: Short governance warnings, e.g. "Unapproved", "Excessive autonomy".
    flags: list[str] = field(default_factory=list)

    @property
    def failure_rate(self) -> float:
        return (self.failed / self.runs) if self.runs else 0.0

    @property
    def needs_attention(self) -> bool:
        return bool(
            self.flags
            or self.pending_approvals
            or self.health in {HealthStatus.DEGRADED, HealthStatus.UNHEALTHY}
            or self.risk_level == RiskLevel.CRITICAL
        )


@dataclass
class Layer:
    key: str
    title: str
    icon: str
    nodes: list[Node] = field(default_factory=list)
    url: str = ""

    @property
    def count(self) -> int:
        return len(self.nodes)


@dataclass
class Ecosystem:
    layers: list[Layer] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


class ControlTowerService:
    """Assembles the layered ecosystem view for one organization."""

    def __init__(self, organization, days: int = WINDOW_DAYS):
        self.organization = organization
        self.days = days
        self.since = timezone.now() - timedelta(days=days)

    # -- Rollups ------------------------------------------------------------

    @property
    def window(self):
        return Execution.objects.for_organization(self.organization).filter(
            started_at__gte=self.since
        )

    def _execution_rollup(self, field_name: str) -> dict:
        rows = (
            self.window.filter(**{f"{field_name}__isnull": False})
            .values(field_name)
            .annotate(
                runs=Count("id"),
                failed=Count("id", filter=Q(status__in=FAILED_STATUSES)),
                live=Count("id", filter=Q(status__in=LIVE_STATUSES)),
                cost=Sum("cost"),
                tokens=Sum("total_tokens"),
            )
        )
        return {row[field_name]: row for row in rows}

    def _risk_rollup(self, field_name: str) -> dict:
        rows = (
            Risk.objects.for_organization(self.organization)
            .filter(status__in=OPEN_RISK_STATUSES, **{f"{field_name}__isnull": False})
            .values(field_name)
            .annotate(total=Count("id"))
        )
        return {row[field_name]: row["total"] for row in rows}

    def _approval_rollup(self, field_name: str) -> dict:
        rows = (
            ApprovalRequest.objects.for_organization(self.organization)
            .filter(status=ApprovalStatus.PENDING, **{f"{field_name}__isnull": False})
            .values(field_name)
            .annotate(total=Count("id"))
        )
        return {row[field_name]: row["total"] for row in rows}

    @staticmethod
    def _apply(node: Node, rollup: dict) -> Node:
        row = rollup.get(node.id)
        if row:
            node.runs = row["runs"]
            node.failed = row["failed"]
            node.live = row["live"]
            node.cost = row["cost"] or ZERO
            node.tokens = row["tokens"] or 0
        return node

    # -- Layers -------------------------------------------------------------

    def applications(self) -> Layer:
        rollup = self._execution_rollup("application")
        risks = self._risk_rollup("application")
        approvals = self._approval_rollup("application")

        nodes = []
        for application in (
            AIApplication.objects.for_organization(self.organization)
            .select_related("team")
            .annotate(agent_total=Count("agents", distinct=True))
            .order_by("name")
        ):
            node = Node(
                id=application.pk,
                name=application.name,
                url=reverse("inventory:application_detail", args=[application.pk]),
                subtitle=f"{application.team.name if application.team else 'Unassigned'} · "
                f"{application.agent_total} agent{'' if application.agent_total == 1 else 's'}",
                status=application.status,
                status_label=application.get_status_display(),
                risk_level=application.risk_level,
                open_risks=risks.get(application.pk, 0),
                pending_approvals=approvals.get(application.pk, 0),
            )
            self._apply(node, rollup)
            node.health = health_from_failure_rate(node.runs, node.failed)

            if application.needs_review:
                node.flags.append("Review overdue")
            if not application.business_owner_id or not application.technical_owner_id:
                node.flags.append("No owner")
            nodes.append(node)

        return Layer("applications", "AI Applications", "bi-window-stack", nodes,
                     reverse("inventory:applications"))

    def agents(self) -> Layer:
        rollup = self._execution_rollup("agent")
        risks = self._risk_rollup("agent")
        approvals = self._approval_rollup("agent")

        nodes = []
        for agent in (
            AIAgent.objects.for_organization(self.organization)
            .select_related("primary_model", "provider")
            .order_by("name")
        ):
            node = Node(
                id=agent.pk,
                name=agent.name,
                url=reverse("inventory:agent_detail", args=[agent.pk]),
                subtitle=f"{agent.get_framework_display()} · "
                f"{agent.primary_model.model_name if agent.primary_model else 'No model'}",
                status=agent.status,
                status_label=agent.get_status_display(),
                risk_level=agent.risk_level,
                open_risks=risks.get(agent.pk, 0),
                pending_approvals=approvals.get(agent.pk, 0),
            )
            self._apply(node, rollup)
            node.health = health_from_failure_rate(node.runs, node.failed)

            node.flags.append(f"L{agent.autonomy_level} autonomy")
            if agent.has_excessive_autonomy:
                node.flags.append("Excessive autonomy")
            nodes.append(node)

        return Layer("agents", "AI Agents", "bi-robot", nodes, reverse("inventory:agents"))

    def models(self) -> Layer:
        rollup = self._execution_rollup("model")
        risks = self._risk_rollup("model")

        nodes = []
        for model in (
            AIModel.objects.for_organization(self.organization)
            .select_related("provider")
            .order_by("model_name")
        ):
            node = Node(
                id=model.pk,
                name=model.model_name,
                url=reverse("inventory:model_detail", args=[model.pk]),
                subtitle=f"{model.provider.name} · {model.get_model_type_display()}",
                status=model.status,
                status_label=model.get_status_display(),
                risk_level=model.risk_classification,
                open_risks=risks.get(model.pk, 0),
            )
            self._apply(node, rollup)
            node.health = health_from_failure_rate(node.runs, node.failed)

            if not model.approved_for_use:
                node.flags.append("Unapproved")
            nodes.append(node)

        return Layer("models", "AI Models", "bi-cpu", nodes, reverse("inventory:models"))

    def providers(self) -> Layer:
        rollup = self._execution_rollup("provider")
        risks = self._risk_rollup("provider")

        nodes = []
        for provider in AIProvider.objects.for_organization(self.organization).order_by("name"):
            node = Node(
                id=provider.pk,
                name=provider.name,
                url=reverse("inventory:provider_detail", args=[provider.pk]),
                subtitle=f"{provider.get_provider_type_display()} · {provider.region or 'global'}",
                status=provider.status,
                status_label=provider.get_status_display(),
                # Providers report their own health, so use the probe rather
                # than inferring it from execution outcomes.
                health=provider.health_status,
                open_risks=risks.get(provider.pk, 0),
            )
            self._apply(node, rollup)

            if not provider.is_approved:
                node.flags.append("Unapproved")
            if provider.credential_ref and not provider.has_resolvable_credential:
                node.flags.append("Credential missing")
            nodes.append(node)

        return Layer("providers", "AI Providers", "bi-hdd-network", nodes,
                     reverse("inventory:providers"))

    # -- Assembly -----------------------------------------------------------

    def build(self) -> Ecosystem:
        if self.organization is None:
            return Ecosystem()

        layers = [self.applications(), self.agents(), self.models(), self.providers()]
        return Ecosystem(layers=layers, summary=self._summary(layers))

    def _summary(self, layers: list[Layer]) -> dict[str, Any]:
        """The one-screen answer to section 34's eight questions."""
        totals = self.window.aggregate(
            runs=Count("id"),
            failed=Count("id", filter=Q(status__in=FAILED_STATUSES)),
            live=Count("id", filter=Q(status__in=LIVE_STATUSES)),
            cost=Sum("cost"),
            tokens=Sum("total_tokens"),
        )
        runs = totals["runs"] or 0
        failed = totals["failed"] or 0

        open_risks = list(
            Risk.objects.for_organization(self.organization).filter(
                status__in=OPEN_RISK_STATUSES
            )
        )
        pending_approvals = ApprovalRequest.objects.for_organization(self.organization).filter(
            status=ApprovalStatus.PENDING
        )
        provider_layer = next(layer for layer in layers if layer.key == "providers")
        unhealthy = [
            node for node in provider_layer.nodes
            if node.health in {HealthStatus.DEGRADED, HealthStatus.UNHEALTHY}
        ]

        # Overall posture is the worst provider health, since a degraded
        # provider degrades everything sitting on top of it.
        overall_health = min(
            (node.health for node in provider_layer.nodes),
            key=lambda status: HEALTH_RANK.get(status, 2),
            default=HealthStatus.UNKNOWN,
        )

        return {
            "window_days": self.days,
            "executions": runs,
            "failed": failed,
            "live": totals["live"] or 0,
            "failure_rate": (failed / runs * 100) if runs else 0,
            "cost": totals["cost"] or ZERO,
            "tokens": totals["tokens"] or 0,
            "overall_health": overall_health,
            "unhealthy_providers": len(unhealthy),
            "open_risks": len(open_risks),
            "critical_risks": len(
                [r for r in open_risks if r.effective_level == RiskLevel.CRITICAL]
            ),
            "pending_approvals": pending_approvals.count(),
            "critical_approvals": pending_approvals.filter(
                risk_level=RiskLevel.CRITICAL
            ).count(),
            "budget_alerts": BudgetAlert.objects.for_organization(self.organization)
            .filter(acknowledged_at__isnull=True)
            .count(),
            "counts": {layer.key: layer.count for layer in layers},
        }
