"""Dashboard aggregation (sections 7, 32, 33).

Every number is derived from real state. Metrics that depend on modules not yet
built are reported as unavailable rather than as zero, so the dashboard never
implies "no problems" when it simply has no data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Avg, Count, Q, Sum
from django.db.models.functions import TruncDate
from django.urls import reverse
from django.utils import timezone

from apps.agents.models import AgentFramework, AIAgent
from apps.applications_registry.models import AIApplication
from apps.approvals.models import ApprovalRequest, ApprovalStatus
from apps.core.constants import Environment, HealthStatus, LifecycleStatus, RiskLevel
from apps.costs.models import BudgetAlert
from apps.dashboard.ranges import DateRange
from apps.executions.models import Execution, ExecutionStatusChoices
from apps.models_registry.models import AIModel
from apps.policies.models import Policy, PolicyEffect, PolicyViolation
from apps.providers.models import AIProvider
from apps.risks.models import OPEN_STATUSES as OPEN_RISK_STATUSES, Risk

#: An agent is "failing" when it fails at least this often, over enough runs to
#: be meaningful rather than a single unlucky call.
FAILURE_RATE_THRESHOLD = 0.25
FAILURE_MIN_RUNS = 5


@dataclass
class Metric:
    label: str
    value: Any
    icon: str = "bi-graph-up"
    tone: str = "primary"
    hint: str = ""
    url: str = ""
    available: bool = True


@dataclass
class AttentionItem:
    severity: str  # critical | high | medium | info
    title: str
    detail: str = ""
    url: str = ""
    count: int = 0


@dataclass
class ModuleStatus:
    """A platform capability and whether it is actually available yet."""

    name: str
    state: str  # live | planned
    description: str = ""
    url: str = ""


@dataclass
class DashboardData:
    metrics: list[Metric] = field(default_factory=list)
    attention: list[AttentionItem] = field(default_factory=list)
    charts: dict[str, Any] = field(default_factory=dict)
    modules: list[ModuleStatus] = field(default_factory=list)


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "info": 3}


class DashboardService:
    """Builds the executive view for one organization over a chosen window."""

    def __init__(self, organization, date_range: DateRange | None = None):
        self.organization = organization
        self.now = timezone.now()
        self.today = self.now.date()
        self.range = date_range or DateRange.preset(today=self.today)

    # -- Base querysets -----------------------------------------------------

    @property
    def applications(self):
        return AIApplication.objects.for_organization(self.organization)

    @property
    def agents(self):
        return AIAgent.objects.for_organization(self.organization)

    @property
    def ai_models(self):
        return AIModel.objects.for_organization(self.organization)

    @property
    def providers(self):
        return AIProvider.objects.for_organization(self.organization)

    @property
    def executions(self):
        return Execution.objects.for_organization(self.organization)

    def executions_today(self):
        return self.executions.filter(started_at__date=self.today)

    @property
    def in_range(self):
        """Executions inside the selected window."""
        return self.executions.filter(
            started_at__date__gte=self.range.start, started_at__date__lte=self.range.end
        )

    @property
    def policies(self):
        return Policy.objects.for_organization(self.organization)

    @property
    def evaluation_suites(self):
        from apps.evaluations.models import EvaluationSuite

        return EvaluationSuite.objects.for_organization(self.organization)

    @property
    def routing_policies(self):
        from apps.routing.models import RoutingPolicy

        return RoutingPolicy.objects.for_organization(self.organization)

    @property
    def policy_denials_in_range(self):
        return PolicyViolation.objects.for_organization(self.organization).filter(
            effect=PolicyEffect.DENY,
            created_at__date__gte=self.range.start,
            created_at__date__lte=self.range.end,
        )

    @property
    def open_risks(self):
        return Risk.objects.for_organization(self.organization).filter(
            status__in=OPEN_RISK_STATUSES
        )

    @property
    def pending_approvals(self):
        return ApprovalRequest.objects.for_organization(self.organization).filter(
            status=ApprovalStatus.PENDING
        )

    # -- Public API ---------------------------------------------------------

    def build(self) -> DashboardData:
        if self.organization is None:
            return DashboardData()

        return DashboardData(
            metrics=self._metrics(),
            attention=self._attention(),
            charts=self._charts(),
            modules=self._modules(),
        )

    def _modules(self) -> list[ModuleStatus]:
        """What the platform can do today, and what is still ahead.

        Stated explicitly rather than as a vague "coming soon" line: an
        executive reading this needs to know which controls are actually
        enforcing something.
        """
        return [
            ModuleStatus(
                "Policy engine", "live",
                f"{self.policies.filter(status=LifecycleStatus.ACTIVE).count()} active policies "
                "gating provider, model, data and tool use.",
                reverse("policies:list"),
            ),
            ModuleStatus(
                "Model routing", "live",
                f"{self.routing_policies.filter(status=LifecycleStatus.ACTIVE).count()} active "
                "routing policies selecting models and ordering fallbacks.",
                reverse("routing:list"),
            ),
            ModuleStatus(
                "AI gateway", "live",
                "Agent traffic served in-path, with policy and budget enforced "
                "before a provider is called.",
                reverse("gateway:overview"),
            ),
            ModuleStatus(
                "Evaluation centre", "live",
                f"{self.evaluation_suites.filter(status=LifecycleStatus.ACTIVE).count()} active "
                "suites scoring models and agents against deterministic checks.",
                reverse("evaluations:home"),
            ),
        ]

    # -- Metrics ------------------------------------------------------------

    def _metrics(self) -> list[Metric]:
        active = LifecycleStatus.ACTIVE
        window = self.in_range
        period = self.range.describe()

        totals = window.aggregate(
            count=Count("id"),
            tokens=Sum("total_tokens"),
            cost=Sum("cost"),
            latency=Avg("latency_ms"),
        )
        succeeded = window.filter(status=ExecutionStatusChoices.COMPLETED).count()
        failed = window.filter(
            status__in=[ExecutionStatusChoices.FAILED, ExecutionStatusChoices.TIMED_OUT]
        ).count()

        unapproved_models = self.ai_models.filter(approved_for_use=False).count()
        degraded_providers = self.providers.filter(
            health_status__in=[HealthStatus.DEGRADED, HealthStatus.UNHEALTHY]
        ).count()
        awaiting_approval = self.executions.filter(
            status=ExecutionStatusChoices.WAITING_FOR_APPROVAL
        ).count()
        pending_approvals = self.pending_approvals.count()

        open_risk_list = list(self.open_risks)
        open_risks = len(open_risk_list)
        critical_risks = len(
            [r for r in open_risk_list if r.effective_level == RiskLevel.CRITICAL]
        )

        active_policies = self.policies.filter(status=active).count()
        policy_denials = self.policy_denials_in_range.count()

        executions_url = reverse("executions:list")

        metrics = [
            Metric("Active Applications", self.applications.filter(status=active).count(),
                   icon="bi-window-stack", url=reverse("inventory:applications")),
            Metric("Active Agents", self.agents.filter(status=active).count(),
                   icon="bi-robot", url=reverse("inventory:agents")),
            Metric("Active Models", self.ai_models.filter(status=active).count(),
                   icon="bi-cpu", url=reverse("inventory:models")),
            Metric("AI Providers", self.providers.count(),
                   icon="bi-hdd-network", url=reverse("inventory:providers")),

            Metric("Executions", f"{totals['count'] or 0:,}", icon="bi-activity",
                   hint=period, url=executions_url),
            Metric("Successful", f"{succeeded:,}", icon="bi-check-circle",
                   tone="success", hint=period,
                   url=f"{executions_url}?status=completed"),
            Metric("Failed", f"{failed:,}", icon="bi-x-octagon",
                   tone="danger" if failed else "success", hint=period,
                   url=f"{executions_url}?status=failed"),
            Metric("Tokens", f"{totals['tokens'] or 0:,}", icon="bi-hash",
                   hint=period, url=executions_url),

            Metric("Cost", f"${totals['cost'] or 0:,.2f}", icon="bi-currency-dollar",
                   hint=period, url=reverse("costs:dashboard")),
            Metric("Avg Latency",
                   f"{int(totals['latency']):,} ms" if totals["latency"] else "—",
                   icon="bi-stopwatch", hint=period, url=executions_url),
            Metric("Policy Denials", policy_denials, icon="bi-shield-slash",
                   tone="danger" if policy_denials else "success",
                   hint=f"blocked, {period}", url=reverse("policies:list")),
            Metric("Active Policies", active_policies, icon="bi-file-earmark-ruled",
                   hint="gating provider, model and tool use",
                   url=reverse("policies:list")),
            Metric("Pending Approvals", pending_approvals, icon="bi-person-check",
                   tone="warning" if pending_approvals else "success",
                   hint="Actions waiting on a human decision",
                   url=reverse("approvals:centre")),
            Metric("Runs Paused", awaiting_approval, icon="bi-pause-circle",
                   tone="warning" if awaiting_approval else "success",
                   hint="Executions held for approval",
                   url=f"{executions_url}?status=waiting_for_approval"),
            Metric("Open Risks", open_risks, icon="bi-clipboard-data",
                   tone="warning" if open_risks else "success",
                   url=reverse("risks:list")),
            Metric("Critical Risks", critical_risks, icon="bi-exclamation-diamond",
                   tone="danger" if critical_risks else "success",
                   url=reverse("risks:list")),
            Metric("Provider Incidents", degraded_providers, icon="bi-exclamation-octagon",
                   tone="danger" if degraded_providers else "success",
                   url=reverse("inventory:providers")),

            Metric("Unapproved Models", unapproved_models, icon="bi-shield-exclamation",
                   tone="warning" if unapproved_models else "success",
                   hint="In use without governance approval",
                   url=reverse("inventory:models")),
            Metric("High-Risk Agents",
                   self.agents.filter(risk_level__in=[RiskLevel.HIGH, RiskLevel.CRITICAL]).count(),
                   icon="bi-exclamation-triangle", tone="warning",
                   url=reverse("inventory:agents")),
            Metric("Production Applications",
                   self.applications.filter(environment=Environment.PRODUCTION).count(),
                   icon="bi-broadcast", url=reverse("inventory:applications")),
        ]

        return metrics

    # -- Needs Attention ----------------------------------------------------

    def _irgf_attention(self) -> list[AttentionItem]:
        """IRGF items: stopped agents, and systems whose S2 classification needs someone."""
        from django.db.models import Q

        from apps.classification.models import (
            CURRENT_STATUSES,
            ClassificationStatus,
            RiskClassification,
        )
        from apps.core.constants import Environment

        items: list[AttentionItem] = []
        register = reverse("classification:register")

        suspended = self.agents.filter(suspended_at__isnull=False)
        if suspended.exists():
            items.append(AttentionItem(
                "high", f"{suspended.count()} agent(s) suspended by kill-switch",
                ", ".join(suspended.values_list("name", flat=True)[:4]),
                reverse("inventory:agents"), suspended.count(),
            ))

        unclassified = self.agents.filter(environment=Environment.PRODUCTION, risk_tier__isnull=True)
        if unclassified.exists():
            items.append(AttentionItem(
                "high", f"{unclassified.count()} production agent(s) without a risk classification",
                "IRGF stage S2 was skipped: Gate G1 needs provisional D1–D5 scores.",
                register, unclassified.count(),
            ))

        current = RiskClassification.objects.for_organization(self.organization).filter(
            status__in=CURRENT_STATUSES
        )
        provisional = current.filter(status=ClassificationStatus.PROVISIONAL).count()
        if provisional:
            items.append(AttentionItem(
                "medium", f"{provisional} risk classification(s) awaiting countersignature",
                "Provisional tiers count for Gate G1; Gate G2 needs an independent "
                "countersignature (IRGF M6).",
                f"{register}?status=provisional", provisional,
            ))

        due = current.filter(Q(rescore_required=True) | Q(review_due__lte=self.today)).count()
        if due:
            items.append(AttentionItem(
                "medium", f"{due} system(s) due for re-scoring",
                "A re-score trigger fired, or the annual Tier 3–4 review has come round.",
                f"{register}?due=1", due,
            ))
        return items + self._gate_attention() + self._drift_attention()

    def _drift_attention(self) -> list[AttentionItem]:
        """IRGF M12-M13: boundary incidents, and drift past its response window."""
        from django.utils import timezone

        from apps.controlplane.models import UNRESOLVED_STATUSES, DriftRecord, DriftStatus

        items: list[AttentionItem] = []
        records = DriftRecord.objects.for_organization(self.organization)
        drift = reverse("controlplane:drift")

        incidents = records.filter(status__in=UNRESOLVED_STATUSES, is_incident=True).count()
        if incidents:
            items.append(AttentionItem(
                "high", f"{incidents} agent boundary incident(s)",
                "The Control Plane saw agents act beyond their authorized boundary or limits (IRGF M13).",
                f"{drift}?incident=1", incidents,
            ))
        overdue = records.filter(status=DriftStatus.OPEN, response_due__lt=timezone.now()).count()
        if overdue:
            items.append(AttentionItem(
                "high", f"{overdue} drift record(s) past their response window",
                "Escalated to the AI Governance Body: revert, ratify, except or escalate them.",
                f"{drift}?overdue=1", overdue,
            ))
        waiting = records.filter(status=DriftStatus.OPEN, is_incident=False).count()
        if waiting:
            items.append(AttentionItem(
                "medium", f"{waiting} drift record(s) awaiting a decision",
                "Running state that differs from the authorized configuration (IRGF M12).",
                f"{drift}?status=open", waiting,
            ))
        return items

    def _gate_attention(self) -> list[AttentionItem]:
        """IRGF gates: G3 decisions and suspensions, G4 changes, M17 exceptions."""
        from datetime import timedelta

        from apps.core.constants import Environment
        from apps.lifecycle import irgf
        from apps.lifecycle.models import (
            BASELINE_STATUSES,
            AuthorizationCondition,
            AuthorizationStatus,
            ChangeRecord,
            ChangeStatus,
            DeploymentAuthorization,
            ExceptionStatus,
            GovernanceException,
        )

        items: list[AttentionItem] = []
        authorizations = DeploymentAuthorization.objects.for_organization(self.organization)
        g3 = reverse("lifecycle:authorizations")

        suspended = authorizations.filter(status=AuthorizationStatus.SUSPENDED).select_related("agent", "application")
        if suspended.exists():
            items.append(AttentionItem(
                "high", f"{suspended.count()} deployment authorization(s) suspended",
                ", ".join(a.subject.name for a in suspended[:4]), f"{g3}?status=suspended", suspended.count(),
            ))

        changes = ChangeRecord.objects.for_organization(self.organization).filter(
            status=ChangeStatus.AWAITING_G4
        )
        if changes.exists():
            major = changes.filter(classification=irgf.MAJOR).count()
            items.append(AttentionItem(
                "high" if major else "medium", f"{changes.count()} change(s) awaiting Gate G4",
                f"{major} major" if major else "Material changes to authorized agents.",
                reverse("lifecycle:changes"), changes.count(),
            ))

        pending = authorizations.filter(status=AuthorizationStatus.PENDING).count()
        if pending:
            items.append(AttentionItem(
                "medium", f"{pending} deployment authorization(s) awaiting Gate G3",
                "Tier 4 needs the governance body and the enterprise sponsor.",
                f"{g3}?status=pending", pending,
            ))

        covered = authorizations.filter(status__in=BASELINE_STATUSES).values_list("agent_id", flat=True)
        unauthorized = self.agents.filter(environment=Environment.PRODUCTION).exclude(pk__in=covered)
        if unauthorized.exists():
            items.append(AttentionItem(
                "medium", f"{unauthorized.count()} production agent(s) without a deployment authorization",
                "IRGF S7 · Gate G3: " + ", ".join(unauthorized.values_list("name", flat=True)[:3]),
                g3, unauthorized.count(),
            ))

        overdue = AuthorizationCondition.objects.for_organization(self.organization).filter(
            met_at__isnull=True, due_date__lt=self.today, authorization__status__in=BASELINE_STATUSES,
        ).count()
        if overdue:
            items.append(AttentionItem(
                "medium", f"{overdue} G3 condition(s) past their deadline",
                "Escalated to whoever authorized the system.", g3, overdue,
            ))

        expiring = GovernanceException.objects.for_organization(self.organization).filter(
            status=ExceptionStatus.ACTIVE, expires_at__gt=self.now,
            expires_at__lte=self.now + timedelta(days=irgf.EXCEPTION_WARNING_DAYS),
        ).count()
        if expiring:
            items.append(AttentionItem(
                "medium", f"{expiring} exception(s) end within {irgf.EXCEPTION_WARNING_DAYS} days",
                "IRGF M17: renew, or be ready for the policy to apply again.",
                reverse("lifecycle:exceptions"), expiring,
            ))
        return items

    def _attention(self) -> list[AttentionItem]:
        items: list[AttentionItem] = []
        executions_url = reverse("executions:list")

        unhealthy = self.providers.filter(health_status=HealthStatus.UNHEALTHY)
        if unhealthy.exists():
            items.append(AttentionItem(
                "critical", f"{unhealthy.count()} provider(s) unhealthy",
                ", ".join(unhealthy.values_list("name", flat=True)[:4]),
                reverse("inventory:providers"), unhealthy.count(),
            ))

        for agent_name, rate, runs in self._failing_agents():
            items.append(AttentionItem(
                "critical", f"Agent '{agent_name}' is failing",
                f"{rate:.0%} of {runs} runs failed in the last 24 hours.",
                f"{executions_url}?status=failed&days=1", runs,
            ))

        risky_agents = [a for a in self.agents.all() if a.has_excessive_autonomy]
        if risky_agents:
            items.append(AttentionItem(
                "critical", f"{len(risky_agents)} high-risk agent(s) with excessive autonomy",
                ", ".join(a.name for a in risky_agents[:4]),
                reverse("inventory:agents"), len(risky_agents),
            ))

        items.extend(self._irgf_attention())

        critical_risks = [r for r in self.open_risks if r.effective_level == RiskLevel.CRITICAL]
        if critical_risks:
            items.append(AttentionItem(
                "critical", f"{len(critical_risks)} critical risk(s) open",
                ", ".join(r.title for r in critical_risks[:3]),
                reverse("risks:list"), len(critical_risks),
            ))

        denials = PolicyViolation.objects.for_organization(self.organization).filter(
            effect=PolicyEffect.DENY, acknowledged_at__isnull=True
        )
        if denials.exists():
            names = list(denials.values_list("policy__name", flat=True).distinct()[:3])
            items.append(AttentionItem(
                "critical", f"{denials.count()} policy denial(s) unreviewed",
                ", ".join(names), reverse("policies:list"), denials.count(),
            ))

        pending = self.pending_approvals
        pending_count = pending.count()
        if pending_count:
            critical_pending = pending.filter(risk_level=RiskLevel.CRITICAL).count()
            items.append(AttentionItem(
                "critical" if critical_pending else "high",
                f"{pending_count} approval(s) pending",
                f"{critical_pending} critical" if critical_pending
                else "Actions are held until a human decides.",
                reverse("approvals:centre"), pending_count,
            ))

        stale = [a for a in pending if a.is_expired]
        if stale:
            items.append(AttentionItem(
                "high", f"{len(stale)} approval(s) past their deadline",
                "These will expire and the runs will time out.",
                reverse("approvals:centre"), len(stale),
            ))

        alerts = BudgetAlert.objects.for_organization(self.organization).filter(
            acknowledged_at__isnull=True
        )
        if alerts.exists():
            worst = alerts.order_by("-threshold").first()
            items.append(AttentionItem(
                "critical" if worst.threshold >= 100 else "high",
                f"{alerts.count()} unacknowledged budget alert(s)",
                f"{worst.budget.name} reached {worst.threshold}%",
                reverse("costs:budgets"), alerts.count(),
            ))

        awaiting = self.executions.filter(status=ExecutionStatusChoices.WAITING_FOR_APPROVAL)
        if awaiting.exists():
            items.append(AttentionItem(
                "medium", f"{awaiting.count()} execution(s) paused for approval",
                "Runs are held until a decision is recorded.",
                f"{executions_url}?status=waiting_for_approval", awaiting.count(),
            ))

        overdue_risks = self.open_risks.filter(review_date__lte=self.today)
        if overdue_risks.exists():
            items.append(AttentionItem(
                "medium", f"{overdue_risks.count()} risk(s) overdue for review",
                ", ".join(overdue_risks.values_list("title", flat=True)[:3]),
                reverse("risks:list"), overdue_risks.count(),
            ))

        degraded = self.providers.filter(health_status=HealthStatus.DEGRADED)
        if degraded.exists():
            items.append(AttentionItem(
                "high", f"{degraded.count()} provider(s) degraded",
                ", ".join(degraded.values_list("name", flat=True)[:4]),
                reverse("inventory:providers"), degraded.count(),
            ))

        spend = self._spend_change()
        if spend is not None and spend > 20:
            items.append(AttentionItem(
                "high", f"AI spend up {spend:.0f}% week on week",
                "Compared with the previous seven days.", executions_url,
            ))

        unapproved = self.ai_models.filter(approved_for_use=False, status=LifecycleStatus.ACTIVE)
        if unapproved.exists():
            items.append(AttentionItem(
                "high", f"{unapproved.count()} unapproved model(s) in active use",
                ", ".join(unapproved.values_list("model_name", flat=True)[:4]),
                reverse("inventory:models"), unapproved.count(),
            ))

        overdue = self.applications.filter(
            review_date__lte=self.today, status=LifecycleStatus.ACTIVE
        )
        if overdue.exists():
            items.append(AttentionItem(
                "medium", f"{overdue.count()} application(s) overdue for review",
                ", ".join(overdue.values_list("name", flat=True)[:4]),
                reverse("inventory:applications"), overdue.count(),
            ))

        unowned = self.applications.filter(
            Q(business_owner__isnull=True) | Q(technical_owner__isnull=True),
            environment=Environment.PRODUCTION,
        )
        if unowned.exists():
            items.append(AttentionItem(
                "medium", f"{unowned.count()} production application(s) missing an owner",
                "Ownership is required before a production review can be signed off.",
                reverse("inventory:applications"), unowned.count(),
            ))

        unresolvable = [
            p for p in self.providers.filter(status=LifecycleStatus.ACTIVE)
            if p.credential_ref and not p.has_resolvable_credential
        ]
        if unresolvable:
            items.append(AttentionItem(
                "high", f"{len(unresolvable)} provider(s) with unresolvable credentials",
                ", ".join(p.name for p in unresolvable[:4]),
                reverse("inventory:providers"), len(unresolvable),
            ))

        items.sort(key=lambda item: SEVERITY_ORDER.get(item.severity, 9))
        return items

    def _failing_agents(self) -> list[tuple[str, float, int]]:
        """Agents whose recent failure rate crosses the alerting threshold."""
        rows = (
            self.executions.filter(started_at__gte=self.now - timedelta(days=1), agent__isnull=False)
            .values("agent__name")
            .annotate(
                runs=Count("id"),
                failures=Count(
                    "id",
                    filter=Q(status__in=[
                        ExecutionStatusChoices.FAILED, ExecutionStatusChoices.TIMED_OUT
                    ]),
                ),
            )
            .filter(runs__gte=FAILURE_MIN_RUNS)
        )
        failing = []
        for row in rows:
            rate = row["failures"] / row["runs"]
            if rate >= FAILURE_RATE_THRESHOLD:
                failing.append((row["agent__name"], rate, row["runs"]))
        return sorted(failing, key=lambda item: item[1], reverse=True)[:5]

    def _spend_change(self) -> float | None:
        """Week-on-week spend change, as a percentage."""
        this_week = self.executions.filter(
            started_at__gte=self.now - timedelta(days=7)
        ).aggregate(total=Sum("cost"))["total"] or Decimal("0")
        last_week = self.executions.filter(
            started_at__gte=self.now - timedelta(days=14),
            started_at__lt=self.now - timedelta(days=7),
        ).aggregate(total=Sum("cost"))["total"] or Decimal("0")

        if not last_week:
            return None
        return float((this_week - last_week) / last_week * 100)

    # -- Charts -------------------------------------------------------------

    def _charts(self) -> dict[str, Any]:
        return {
            "execution_volume": self._daily_volume(),
            "spend_over_time": self._daily_spend(),
            "latency_and_errors": self._daily_latency_errors(),
            "status_distribution": self._status_distribution(),
            "cost_by_provider": self._grouped_cost("provider__name"),
            "cost_by_application": self._grouped_cost("application__name"),
            "usage_by_model": self._grouped_tokens("model__model_name"),
            "usage_by_agent": self._grouped_tokens("agent__name"),
            "agent_risk_distribution": self._labelled_counts(
                self.agents.values("risk_level").annotate(total=Count("id")).order_by("risk_level"),
                "risk_level", dict(RiskLevel.choices),
            ),
            "agents_by_framework": self._labelled_counts(
                self.agents.values("framework").annotate(total=Count("id")).order_by("-total"),
                "framework", dict(AgentFramework.choices),
            ),
            # One chart per platform module, each answering the question that
            # module exists to answer rather than restating its row counts.
            "policy_verdicts": self._daily_policy_verdicts(),
            "routing_outcomes": self._daily_routing_outcomes(),
            "gateway_coverage": self._daily_gateway_coverage(),
            "evaluation_evidence": self._evaluation_evidence(),
        }

    # -- Platform module charts -----------------------------------------------

    #: Models shown on the evaluation chart. Past this a bar chart stops being
    #: readable; the Evaluation Centre holds the full list.
    EVIDENCE_LIMIT = 10

    def _by_day(self, queryset, date_field: str, buckets: dict) -> dict:
        """Count rows per day into named buckets across the selected window.

        Every day in the window is present, so a quiet day renders as zero
        rather than as a gap the reader has to interpret.
        """
        rows = (
            queryset.filter(**{
                f"{date_field}__date__gte": self.range.start,
                f"{date_field}__date__lte": self.range.end,
            })
            .annotate(day=TruncDate(date_field))
            .values("day")
            .annotate(**{name: Count("id", filter=condition) for name, condition in buckets.items()})
        )
        by_day = {row["day"]: row for row in rows}
        days = self._day_series()

        series = {"labels": [self._day_label(day) for day in days]}
        for name in buckets:
            series[name] = [by_day.get(day, {}).get(name, 0) for day in days]
        series["totals"] = {name: sum(series[name]) for name in buckets}
        return series

    def _daily_policy_verdicts(self) -> dict:
        """What the policy engine stopped, sent to a human, or merely noted."""
        return self._by_day(
            PolicyViolation.objects.for_organization(self.organization),
            "created_at",
            {
                "denied": Q(effect=PolicyEffect.DENY),
                "approval": Q(effect=PolicyEffect.REQUIRE_APPROVAL),
                "warned": Q(effect=PolicyEffect.WARN),
            },
        )

    def _daily_routing_outcomes(self) -> dict:
        """How often the first choice worked, and how often nothing did."""
        from apps.routing.models import RouteOutcome, RoutingDecision

        served = Q(outcome=RouteOutcome.SERVED)
        series = self._by_day(
            RoutingDecision.objects.for_organization(self.organization),
            "created_at",
            {
                # An engine reporting an outcome without an attempt number gave
                # no sign of falling back, so it is not counted as one.
                "first_choice": served & (Q(attempt__lte=1) | Q(attempt__isnull=True)),
                "fallback": served & Q(attempt__gt=1),
                "failed": Q(outcome=RouteOutcome.EXHAUSTED),
            },
        )
        totals = series["totals"]
        served_total = totals["first_choice"] + totals["fallback"]
        series["fallback_rate"] = totals["fallback"] / served_total if served_total else 0.0
        return series

    def _daily_gateway_coverage(self) -> dict:
        """How much of the estate's traffic is actually under enforcement.

        Reported traffic is observed; gateway traffic is governed. The share
        between the two is the honest measure of what the controls reach.
        """
        series = self._by_day(
            self.executions,
            "started_at",
            {"gateway": Q(source_adapter="gateway"), "reported": ~Q(source_adapter="gateway")},
        )
        totals = series["totals"]
        everything = totals["gateway"] + totals["reported"]
        series["gateway_share"] = totals["gateway"] / everything if everything else 0.0
        return series

    def _evaluation_evidence(self) -> dict:
        """The latest completed run per model, worst first.

        Deliberately not date-filtered: the question is what the current
        evidence says, and a model evaluated last month still has that as its
        evidence. The latest run is used, never the best one -- the same rule
        the model-approval gate applies.
        """
        from django.db.models import OuterRef, Subquery

        from apps.evaluations.models import EvaluationRun, RunStatus
        from apps.evaluations.services import EvaluationStats

        completed = EvaluationRun.objects.for_organization(self.organization).filter(
            status=RunStatus.COMPLETED, model__isnull=False
        )
        latest_for_model = (
            completed.filter(model=OuterRef("model"))
            .order_by("-finished_at")
            .values("pk")[:1]
        )
        runs = list(
            completed.filter(pk=Subquery(latest_for_model)).select_related("model", "suite")
        )

        # Worst first: what needs attention sits where the eye lands.
        verdict_rank = {"blocked": 0, "failed": 1, "passed": 2}
        runs.sort(key=lambda run: (verdict_rank.get(run.verdict, 3), run.score))
        runs = runs[: self.EVIDENCE_LIMIT]

        return {
            "labels": [run.model.model_name for run in runs],
            "scores": [float(run.score) for run in runs],
            "thresholds": [run.suite.pass_threshold for run in runs],
            "verdicts": [run.verdict for run in runs],
            "blocked_by": [run.blocked_by_category for run in runs],
            "suites": [run.suite.name for run in runs],
            "unevaluated_approved": EvaluationStats(self.organization).unevaluated_approved_count(),
        }

    def _day_series(self):
        """Every day in the window, so gaps render as zero not as absent."""
        return self.range.each_day()

    def _day_label(self, day) -> str:
        # A 90-day axis has no room for a label per day; the year only matters
        # once a custom range can span one.
        return day.strftime("%d %b") if self.range.days <= 92 else day.strftime("%d %b %y")

    def _daily_volume(self) -> dict[str, list]:
        rows = (
            self.in_range
            .annotate(day=TruncDate("started_at"))
            .values("day")
            .annotate(
                total=Count("id"),
                failed=Count("id", filter=Q(status__in=[
                    ExecutionStatusChoices.FAILED, ExecutionStatusChoices.TIMED_OUT
                ])),
            )
        )
        by_day = {row["day"]: row for row in rows}
        days = self._day_series()

        return {
            "labels": [self._day_label(day) for day in days],
            "total": [by_day.get(day, {}).get("total", 0) for day in days],
            "failed": [by_day.get(day, {}).get("failed", 0) for day in days],
        }

    def _daily_spend(self) -> dict[str, list]:
        rows = (
            self.in_range
            .annotate(day=TruncDate("started_at"))
            .values("day")
            .annotate(cost=Sum("cost"), tokens=Sum("total_tokens"))
        )
        by_day = {row["day"]: row for row in rows}
        days = self._day_series()

        return {
            "labels": [self._day_label(day) for day in days],
            "cost": [float(by_day.get(day, {}).get("cost") or 0) for day in days],
            "tokens": [int(by_day.get(day, {}).get("tokens") or 0) for day in days],
        }

    def _daily_latency_errors(self) -> dict[str, list]:
        rows = (
            self.in_range
            .annotate(day=TruncDate("started_at"))
            .values("day")
            .annotate(
                latency=Avg("latency_ms"),
                errors=Count("id", filter=Q(status__in=[
                    ExecutionStatusChoices.FAILED, ExecutionStatusChoices.TIMED_OUT
                ])),
            )
        )
        by_day = {row["day"]: row for row in rows}
        days = self._day_series()

        return {
            "labels": [self._day_label(day) for day in days],
            "latency": [int(by_day.get(day, {}).get("latency") or 0) for day in days],
            "errors": [by_day.get(day, {}).get("errors", 0) for day in days],
        }

    def _status_distribution(self) -> dict[str, list]:
        labels = dict(ExecutionStatusChoices.choices)
        rows = (
            self.in_range
            .values("status")
            .annotate(total=Count("id"))
            .order_by("-total")
        )
        return {
            "labels": [labels.get(row["status"], row["status"]) for row in rows],
            "values": [row["total"] for row in rows],
        }

    def _grouped_cost(self, field_name: str, limit: int = 8) -> dict[str, list]:
        rows = (
            self.in_range
            .filter(**{f"{field_name}__isnull": False})
            .values(field_name)
            .annotate(total=Sum("cost"))
            .order_by("-total")[:limit]
        )
        return {
            "labels": [row[field_name] for row in rows],
            "values": [round(float(row["total"] or 0), 4) for row in rows],
        }

    def _grouped_tokens(self, field_name: str, limit: int = 8) -> dict[str, list]:
        rows = (
            self.in_range
            .filter(**{f"{field_name}__isnull": False})
            .values(field_name)
            .annotate(total=Sum("total_tokens"))
            .order_by("-total")[:limit]
        )
        return {
            "labels": [row[field_name] for row in rows],
            "values": [int(row["total"] or 0) for row in rows],
        }

    @staticmethod
    def _labelled_counts(queryset, field_name: str, label_map: dict) -> dict[str, list]:
        rows = list(queryset)
        return {
            "labels": [label_map.get(row[field_name], row[field_name]) for row in rows],
            "values": [row["total"] for row in rows],
        }
