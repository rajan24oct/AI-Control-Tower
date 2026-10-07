"""Cost optimization: specific, costed recommendations.

Each analyzer looks at the last 30 days and proposes something a person can
act on, with the money it would save per month and how sure the platform is.
The estimates are deliberately conservative and their assumptions are named
constants below, shown to the reader as evidence -- a recommendation that
cannot say how it got its number is not one anybody should act on.

Recommendations are keyed by a fingerprint (what kind, about what), so the
analysis can run every day: figures are refreshed, a dismissal sticks, and one
that no longer applies is marked so rather than deleted.
"""

from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Max, Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from apps.agents.models import AIAgent
from apps.core.constants import LifecycleStatus, RiskLevel
from apps.costs.attribution import with_attribution
from apps.costs.devtools import DevToolCostService, add_months, month_start
from apps.costs.models import (
    KIND_CATEGORIES,
    Budget,
    BudgetScope,
    Confidence,
    CostRecommendation,
    DevToolSeat,
    DevToolSubscription,
    DevToolUsage,
    Effort,
    RecommendationKind,
    RecommendationStatus,
    SeatPlan,
)
from apps.costs.services import PricingService
from apps.executions.models import Execution, ExecutionStatusChoices
from apps.models_registry.models import AIModel

logger = logging.getLogger(__name__)
ZERO = Decimal("0")
CENT = Decimal("0.01")
FAILURE_STATUSES = [ExecutionStatusChoices.FAILED, ExecutionStatusChoices.TIMED_OUT]

#: The window the analysis reads; its spend is treated as one month's.
ANALYSIS_DAYS = 30
#: Recommendations worth less than this a month are noise. The floor scales
#: with the estate: 1% of its runtime spend, between these bounds -- a dollar
#: is noise to a large estate and material to a pilot.
MIN_MONTHLY_SAVINGS = Decimal("1.00")
MIN_SAVINGS_FLOOR = Decimal("0.10")
MIN_SAVINGS_SHARE = Decimal("0.01")

# Model right-sizing.
#: A cheaper model must cost at most this fraction of the current one.
RIGHTSIZE_MAX_COST_RATIO = Decimal("0.6")
#: Share of an agent's traffic assumed movable, by risk: riskier agents are
#: held to their current model for more of their work.
MOVABLE_SHARE = {
    RiskLevel.LOW: Decimal("0.8"),
    RiskLevel.MODERATE: Decimal("0.6"),
    RiskLevel.HIGH: Decimal("0.35"),
    RiskLevel.CRITICAL: Decimal("0.2"),
}

# Prompt caching.
CACHING_MIN_INPUT_SHARE = Decimal("0.75")
CACHING_MIN_INPUT_PER_RUN = 1000
#: Share of input tokens assumed to be a repeated prefix (system prompt, tools, context).
ASSUMED_CACHEABLE_SHARE = Decimal("0.5")
#: Discount on cached input when the price list does not state one.
ASSUMED_CACHE_DISCOUNT = Decimal("0.5")

# Output length.
OUTPUT_OUTLIER_FACTOR = 2
OUTPUT_MIN_COST_SHARE = Decimal("0.5")
ASSUMED_OUTPUT_REDUCTION = Decimal("0.25")

# Failures.
FAILURE_MIN_RATE = Decimal("0.05")
#: Share of failure spend assumed avoidable by fixing the cause (some retries are legitimate).
RECOVERABLE_FAILURE_SHARE = Decimal("0.7")

# Developer tools.
IDLE_SEAT_DAYS = 30
NEW_SEAT_GRACE_DAYS = 14
SEAT_HISTORY_MONTHS = 3

# Governance.
UNBUDGETED_MIN_SHARE = Decimal("0.05")


@dataclass
class Finding:
    kind: str
    fingerprint: str
    title: str
    rationale: str
    action: str
    savings: Decimal
    current_cost: Decimal = ZERO
    confidence: str = Confidence.MEDIUM
    effort: str = Effort.MEDIUM
    evidence: dict = field(default_factory=dict)
    targets: dict = field(default_factory=dict)


def money(value) -> str:
    return f"${Decimal(value):,.2f}"


class OptimizationService:
    def __init__(self, organization, now=None):
        self.organization = organization
        self.now = now or timezone.now()
        self.start = self.now - timedelta(days=ANALYSIS_DAYS)
        self.min_savings = MIN_MONTHLY_SAVINGS

    # -- Shared reads ------------------------------------------------------------

    @property
    def window(self):
        return Execution.objects.for_organization(self.organization).filter(
            started_at__gte=self.start, started_at__lt=self.now
        )

    def _agent_model_usage(self) -> list[dict]:
        return list(
            self.window.filter(agent__isnull=False, model__isnull=False)
            .values("agent", "model")
            .annotate(
                cost=Sum("cost"), runs=Count("id"),
                input_tokens=Sum("input_tokens"), output_tokens=Sum("output_tokens"),
                peak_tokens=Max("total_tokens"),
            )
        )

    def _rates(self, model: AIModel) -> tuple[Decimal, Decimal, Decimal | None]:
        pricing = PricingService.resolve(model, self.now)
        if pricing is not None:
            return pricing.input_cost_per_1k, pricing.output_cost_per_1k, pricing.cached_input_cost_per_1k
        return model.input_cost_per_1k, model.output_cost_per_1k, None

    def _list_cost(self, model, input_tokens: int, output_tokens: int) -> Decimal:
        input_rate, output_rate, _ = self._rates(model)
        return Decimal(input_tokens) / 1000 * input_rate + Decimal(output_tokens) / 1000 * output_rate

    def _split(self, model, row) -> tuple[Decimal, Decimal]:
        """The row's actual cost, divided between input and output by list rates."""
        input_rate, output_rate, _ = self._rates(model)
        input_list = Decimal(row["input_tokens"] or 0) / 1000 * input_rate
        output_list = Decimal(row["output_tokens"] or 0) / 1000 * output_rate
        list_total = input_list + output_list
        cost = row["cost"] or ZERO
        if not list_total:
            return ZERO, ZERO
        return cost * input_list / list_total, cost * output_list / list_total

    # -- Runtime analyzers ---------------------------------------------------------

    def model_rightsizing(self, usage, agents, models) -> list[Finding]:
        findings = []
        candidates = list(
            AIModel.objects.for_organization(self.organization)
            .filter(status=LifecycleStatus.ACTIVE, approved_for_use=True)
            .select_related("provider")
        )
        for row in usage:
            agent, model = agents.get(row["agent"]), models.get(row["model"])
            cost = row["cost"] or ZERO
            if agent is None or model is None or cost < self.min_savings:
                continue
            input_tokens, output_tokens = row["input_tokens"] or 0, row["output_tokens"] or 0
            current = self._list_cost(model, input_tokens, output_tokens)
            if not current:
                continue

            options = []
            for candidate in candidates:
                if candidate.pk == model.pk or candidate.model_type != model.model_type:
                    continue
                if candidate.context_window and row["peak_tokens"] and candidate.context_window < row["peak_tokens"]:
                    continue
                ratio = self._list_cost(candidate, input_tokens, output_tokens) / current
                if ratio > RIGHTSIZE_MAX_COST_RATIO:
                    continue
                same_family = bool(model.model_family) and candidate.model_family == model.model_family
                same_provider = candidate.provider_id == model.provider_id
                # Rank by how safe the move is first, then by how much it saves.
                options.append(((not same_family, not same_provider, ratio), candidate, ratio, same_family, same_provider))
            if not options:
                continue
            _, best, ratio, same_family, same_provider = min(options, key=lambda option: option[0])

            share = MOVABLE_SHARE.get(agent.risk_level, Decimal("0.5"))
            savings = (cost * (1 - ratio) * share).quantize(CENT)
            if savings < self.min_savings:
                continue
            confidence = Confidence.MEDIUM if same_family else Confidence.LOW
            where = "the same model family" if same_family else (
                "the same provider" if same_provider else f"{best.provider.name}"
            )
            findings.append(Finding(
                kind=RecommendationKind.MODEL_RIGHTSIZING,
                fingerprint=f"rightsize:{agent.pk}:{model.pk}",
                title=f"Move part of {agent.name} from {model.model_name} to {best.model_name}",
                rationale=(
                    f"{agent.name} spent {money(cost)} on {model.model_name} over {row['runs']:,} runs in the "
                    f"last {ANALYSIS_DAYS} days. {best.model_name}, from {where}, is approved for use and would "
                    f"cost {ratio * 100:.0f}% as much for the same token mix."
                ),
                action=(
                    f"Run {agent.name}'s evaluation suite against {best.model_name}. Route the work that passes "
                    f"to it through Model Routing, with {model.model_name} kept as the fallback. The estimate "
                    f"assumes {share * 100:.0f}% of traffic moves, given the agent's "
                    f"{agent.get_risk_level_display().lower()} risk."
                ),
                savings=savings,
                current_cost=cost,
                confidence=confidence,
                effort=Effort.MEDIUM,
                evidence={
                    "runs": row["runs"], "input_tokens": input_tokens, "output_tokens": output_tokens,
                    "cost_ratio": float(round(ratio, 3)), "movable_share": float(share),
                    "same_family": same_family, "same_provider": same_provider,
                },
                targets={"agent": agent, "model": model, "suggested_model": best, "team": agent.team},
            ))
        return findings

    def prompt_caching(self, usage, agents, models) -> list[Finding]:
        findings = []
        for row in usage:
            agent, model = agents.get(row["agent"]), models.get(row["model"])
            if agent is None or model is None or not row["runs"]:
                continue
            input_tokens, output_tokens = row["input_tokens"] or 0, row["output_tokens"] or 0
            total = input_tokens + output_tokens
            if not total or Decimal(input_tokens) / total < CACHING_MIN_INPUT_SHARE:
                continue
            if input_tokens / row["runs"] < CACHING_MIN_INPUT_PER_RUN:
                continue

            input_cost, _ = self._split(model, row)
            input_rate, _, cached_rate = self._rates(model)
            if cached_rate is not None and input_rate:
                discount, stated = 1 - cached_rate / input_rate, True
            else:
                discount, stated = ASSUMED_CACHE_DISCOUNT, False
            savings = (input_cost * ASSUMED_CACHEABLE_SHARE * discount).quantize(CENT)
            if savings < self.min_savings:
                continue
            findings.append(Finding(
                kind=RecommendationKind.PROMPT_CACHING,
                fingerprint=f"caching:{agent.pk}:{model.pk}",
                title=f"Cache the repeated prompt prefix of {agent.name}",
                rationale=(
                    f"{Decimal(input_tokens) / total * 100:.0f}% of {agent.name}'s tokens on {model.model_name} are "
                    f"input, averaging {input_tokens // row['runs']:,} per run -- the shape of a long system prompt, "
                    f"tool definitions or retrieved context sent again on every call. Input cost {money(input_cost)} "
                    f"in the last {ANALYSIS_DAYS} days."
                ),
                action=(
                    "Put the stable part of the prompt first and enable the provider's prompt caching. "
                    f"The estimate assumes half the input is a cacheable prefix, at a "
                    f"{discount * 100:.0f}% discount{'' if stated else ' (assumed: the price list states no cached rate)'}."
                ),
                savings=savings,
                current_cost=input_cost,
                confidence=Confidence.MEDIUM if stated else Confidence.LOW,
                effort=Effort.LOW,
                evidence={
                    "runs": row["runs"], "input_tokens": input_tokens, "output_tokens": output_tokens,
                    "cached_discount": float(discount), "discount_from_price_list": stated,
                    "cacheable_share": float(ASSUMED_CACHEABLE_SHARE),
                },
                targets={"agent": agent, "model": model, "team": agent.team},
            ))
        return findings

    def output_length(self, usage, agents, models) -> list[Finding]:
        averages = [
            (row["output_tokens"] or 0) / row["runs"] for row in usage if row["runs"] and row["output_tokens"]
        ]
        if len(averages) < 3:
            return []
        median = statistics.median(averages)
        findings = []
        for row in usage:
            agent, model = agents.get(row["agent"]), models.get(row["model"])
            if agent is None or model is None or not row["runs"]:
                continue
            average = (row["output_tokens"] or 0) / row["runs"]
            if not median or average < median * OUTPUT_OUTLIER_FACTOR:
                continue
            _, output_cost = self._split(model, row)
            cost = row["cost"] or ZERO
            if not cost or output_cost / cost < OUTPUT_MIN_COST_SHARE:
                continue
            savings = (output_cost * ASSUMED_OUTPUT_REDUCTION).quantize(CENT)
            if savings < self.min_savings:
                continue
            findings.append(Finding(
                kind=RecommendationKind.OUTPUT_LENGTH,
                fingerprint=f"output:{agent.pk}:{model.pk}",
                title=f"Cap the output length of {agent.name}",
                rationale=(
                    f"{agent.name} writes {average:,.0f} output tokens per run on {model.model_name}, "
                    f"{average / median:.1f}x the estate's median of {median:,.0f}, and output is "
                    f"{output_cost / cost * 100:.0f}% of its cost."
                ),
                action=(
                    "Set max_tokens to what the task needs, and ask for structured or concise answers in the "
                    f"prompt. The estimate assumes a {ASSUMED_OUTPUT_REDUCTION * 100:.0f}% cut in output."
                ),
                savings=savings,
                current_cost=output_cost,
                confidence=Confidence.LOW,
                effort=Effort.LOW,
                evidence={"avg_output_tokens": round(average), "estate_median": round(median), "runs": row["runs"]},
                targets={"agent": agent, "model": model, "team": agent.team},
            ))
        return findings

    def failure_waste(self, agents) -> list[Finding]:
        rows = (
            self.window.filter(agent__isnull=False)
            .values("agent")
            .annotate(
                runs=Count("id"),
                failed_cost=Sum("cost", filter=Q(status__in=FAILURE_STATUSES)),
                failed_runs=Count("id", filter=Q(status__in=FAILURE_STATUSES)),
            )
        )
        findings = []
        for row in rows:
            agent = agents.get(row["agent"])
            failed_cost = row["failed_cost"] or ZERO
            if agent is None or not row["runs"] or not failed_cost:
                continue
            rate = Decimal(row["failed_runs"]) / row["runs"]
            if rate < FAILURE_MIN_RATE:
                continue
            savings = (failed_cost * RECOVERABLE_FAILURE_SHARE).quantize(CENT)
            if savings < self.min_savings:
                continue
            errors = list(
                self.window.filter(agent=agent, status__in=FAILURE_STATUSES)
                .exclude(error_message="")
                .values("error_message").annotate(count=Count("id")).order_by("-count")[:3]
            )
            findings.append(Finding(
                kind=RecommendationKind.FAILURE_WASTE,
                fingerprint=f"failures:{agent.pk}",
                title=f"Fix the failures {agent.name} is paying for",
                rationale=(
                    f"{row['failed_runs']:,} of {agent.name}'s {row['runs']:,} runs ({rate * 100:.0f}%) failed or "
                    f"timed out in the last {ANALYSIS_DAYS} days, after spending {money(failed_cost)} on tokens."
                ),
                action=(
                    "Look at the most common errors below. Fail fast before the model call where the input is "
                    "invalid, add timeouts and stop conditions, and cap retries. The estimate assumes "
                    f"{RECOVERABLE_FAILURE_SHARE * 100:.0f}% of the failure spend is avoidable."
                ),
                savings=savings,
                current_cost=failed_cost,
                confidence=Confidence.HIGH,
                effort=Effort.MEDIUM,
                evidence={
                    "failed_runs": row["failed_runs"], "runs": row["runs"], "failure_rate": float(round(rate, 3)),
                    "top_errors": [{"error": e["error_message"][:200], "count": e["count"]} for e in errors],
                },
                targets={"agent": agent, "team": agent.team},
            ))
        return findings

    # -- Developer-tool analyzers ---------------------------------------------------

    def _seats(self):
        return DevToolSeat.objects.for_organization(self.organization).filter(
            is_active=True, subscription__status=LifecycleStatus.ACTIVE
        ).select_related("subscription", "team", "user")

    def _grouped(self, seats) -> dict:
        groups = defaultdict(list)
        for seat in seats:
            groups[(seat.subscription, seat.team)].append(seat)
        return groups

    @staticmethod
    def _group_label(team) -> str:
        return team.name if team else "seats with no team"

    def idle_seats(self) -> list[Finding]:
        today = self.now.date()
        idle = [
            seat for seat in self._seats()
            if (today - seat.assigned_on).days >= NEW_SEAT_GRACE_DAYS
            and (seat.idle_days(self.now) is None or seat.idle_days(self.now) >= IDLE_SEAT_DAYS)
        ]
        findings = []
        for (subscription, team), seats in self._grouped(idle).items():
            savings = sum((seat.monthly_price for seat in seats), ZERO).quantize(CENT)
            names = ", ".join(seat.login for seat in seats[:6]) + (" and others" if len(seats) > 6 else "")
            findings.append(Finding(
                kind=RecommendationKind.IDLE_SEATS,
                fingerprint=f"idle_seats:{subscription.pk}:{team.pk if team else 'none'}",
                title=f"Reclaim {len(seats)} idle {subscription.get_platform_display()} seat{'s' if len(seats) != 1 else ''} in {self._group_label(team)}",
                rationale=(
                    f"{len(seats)} seat{'s have' if len(seats) != 1 else ' has'} not been used for {IDLE_SEAT_DAYS} days "
                    f"or more ({names}). Each still costs its monthly licence."
                ),
                action=(
                    "Confirm with the people concerned, then remove the seats in GitHub (organization settings, "
                    "Copilot, Access). They can be re-assigned the same day if someone needs one back."
                ),
                savings=savings,
                current_cost=savings,
                confidence=Confidence.HIGH,
                effort=Effort.LOW,
                evidence={"seats": [
                    {"login": seat.login, "name": seat.person_label,
                     "idle_days": seat.idle_days(self.now), "plan": seat.plan,
                     "monthly_price": float(seat.monthly_price)}
                    for seat in seats
                ]},
                targets={"team": team, "subscription": subscription},
            ))
        return findings

    def _monthly_requests(self, seats) -> dict:
        """Premium requests per seat per month over the recent history."""
        first = add_months(month_start(self.now.date()), -(SEAT_HISTORY_MONTHS - 1))
        totals = defaultdict(dict)
        for row in (
            DevToolUsage.objects.filter(seat__in=seats, date__gte=first)
            .annotate(month=TruncMonth("date"))
            .values("seat_id", "month").annotate(requests=Sum("premium_requests"))
        ):
            totals[row["seat_id"]][row["month"]] = row["requests"] or ZERO
        return totals

    def seat_plans(self) -> list[Finding]:
        seats = [seat for seat in self._seats() if (seat.idle_days(self.now) or 0) < IDLE_SEAT_DAYS and seat.last_activity_at]
        monthly = self._monthly_requests(seats)
        downgrades, upgrades = defaultdict(list), defaultdict(list)

        for seat in seats:
            subscription = seat.subscription
            history = list(monthly.get(seat.pk, {}).values())
            peak = max(history, default=ZERO)
            average = sum(history, ZERO) / len(history) if history else ZERO
            price = subscription.premium_request_price
            business = subscription.business_seat_price + max(ZERO, average - subscription.business_included_requests) * price
            enterprise = subscription.enterprise_seat_price + max(ZERO, average - subscription.enterprise_included_requests) * price

            if seat.plan == SeatPlan.ENTERPRISE and peak <= subscription.business_included_requests:
                downgrades[(subscription, seat.team)].append((seat, enterprise - business, peak))
            elif seat.plan == SeatPlan.BUSINESS and business - enterprise >= self.min_savings:
                upgrades[(subscription, seat.team)].append((seat, business - enterprise, average))

        findings = []
        for (subscription, team), items in downgrades.items():
            savings = sum((saving for _, saving, _ in items), ZERO).quantize(CENT)
            if savings < self.min_savings:
                continue
            findings.append(Finding(
                kind=RecommendationKind.SEAT_DOWNGRADE,
                fingerprint=f"downgrade:{subscription.pk}:{team.pk if team else 'none'}",
                title=f"Move {len(items)} Enterprise seat{'s' if len(items) != 1 else ''} in {self._group_label(team)} to Business",
                rationale=(
                    f"In each of the last {SEAT_HISTORY_MONTHS} months these Enterprise seats used no more premium "
                    f"requests than a Business seat includes ({subscription.business_included_requests})."
                ),
                action=(
                    "Check nobody relies on Enterprise-only features (knowledge bases, custom models), then change "
                    "the seats' plan, or the assigning team's, in GitHub."
                ),
                savings=savings,
                current_cost=(subscription.enterprise_seat_price * len(items)).quantize(CENT),
                confidence=Confidence.MEDIUM,
                effort=Effort.LOW,
                evidence={"seats": [
                    {"login": seat.login, "name": seat.person_label, "peak_monthly_requests": float(peak)}
                    for seat, _, peak in items
                ]},
                targets={"team": team, "subscription": subscription},
            ))
        for (subscription, team), items in upgrades.items():
            savings = sum((saving for _, saving, _ in items), ZERO).quantize(CENT)
            findings.append(Finding(
                kind=RecommendationKind.SEAT_UPGRADE,
                fingerprint=f"upgrade:{subscription.pk}:{team.pk if team else 'none'}",
                title=f"Upgrade {len(items)} heavy Copilot user{'s' if len(items) != 1 else ''} in {self._group_label(team)} to Enterprise",
                rationale=(
                    f"These Business seats average more premium requests a month than the "
                    f"{subscription.business_included_requests} included, and the overage at "
                    f"{money(subscription.premium_request_price)} a request costs more than an Enterprise seat, "
                    f"which includes {subscription.enterprise_included_requests}."
                ),
                action="Change these seats to the Enterprise plan; the overage largely disappears.",
                savings=savings,
                confidence=Confidence.MEDIUM,
                effort=Effort.LOW,
                evidence={"seats": [
                    {"login": seat.login, "name": seat.person_label, "avg_monthly_requests": float(round(average))}
                    for seat, _, average in items
                ]},
                targets={"team": team, "subscription": subscription},
            ))
        return findings

    # -- Governance ----------------------------------------------------------------

    def unbudgeted_spend(self, agents) -> list[Finding]:
        budgets = list(
            Budget.objects.for_organization(self.organization)
            .filter(status=LifecycleStatus.ACTIVE)
            .exclude(scope=BudgetScope.ORGANIZATION)
        )
        runtime_budgets = [b for b in budgets if b.counts_runtime]
        rows = list(
            with_attribution(self.window.filter(agent__isnull=False))
            .values("agent", "attributed_team_id", "attributed_department")
            .annotate(cost=Sum("cost"))
        )
        total = sum((row["cost"] or ZERO for row in rows), ZERO)
        per_agent = defaultdict(lambda: {"cost": ZERO, "teams": set(), "departments": set()})
        for row in rows:
            entry = per_agent[row["agent"]]
            entry["cost"] += row["cost"] or ZERO
            entry["teams"].add(row["attributed_team_id"])
            entry["departments"].add((row["attributed_department"] or "").lower())

        findings = []
        for agent_id, entry in per_agent.items():
            agent = agents.get(agent_id)
            if agent is None or not total or entry["cost"] / total < UNBUDGETED_MIN_SHARE:
                continue
            covered = any(
                (b.scope == BudgetScope.AGENT and b.agent_id == agent.pk)
                or (b.scope == BudgetScope.TEAM and b.team_id in entry["teams"])
                or (b.scope == BudgetScope.DEPARTMENT and b.department.lower() in entry["departments"])
                or (b.scope == BudgetScope.PROVIDER and b.provider_id == agent.provider_id)
                or (b.scope == BudgetScope.MODEL and b.model_id == agent.primary_model_id)
                or (b.scope == BudgetScope.APPLICATION and agent.applications.filter(pk=b.application_id).exists())
                for b in runtime_budgets
            )
            if covered:
                continue
            findings.append(Finding(
                kind=RecommendationKind.UNBUDGETED_SPEND,
                fingerprint=f"unbudgeted:agent:{agent.pk}",
                title=f"Put a budget on {agent.name}",
                rationale=(
                    f"{agent.name} spent {money(entry['cost'])} in the last {ANALYSIS_DAYS} days, "
                    f"{entry['cost'] / total * 100:.0f}% of all AI runtime spend, and no budget narrower than the "
                    "organization's covers it. A surge would only show up in the organization total."
                ),
                action=(
                    "Create an agent, team or department budget covering it, with forecast alerts on. Consider a "
                    "hard stop if its traffic goes through the AI Gateway."
                ),
                savings=ZERO,
                current_cost=entry["cost"],
                confidence=Confidence.HIGH,
                effort=Effort.LOW,
                evidence={"share_of_spend": float(round(entry["cost"] / total * 100, 1))},
                targets={"agent": agent, "team": agent.team},
            ))

        devtool_budgeted = any(b.counts_developer_tools for b in Budget.objects.for_organization(
            self.organization).filter(status=LifecycleStatus.ACTIVE))
        if not devtool_budgeted:
            for subscription in DevToolSubscription.objects.for_organization(self.organization).filter(
                status=LifecycleStatus.ACTIVE
            ):
                start = month_start(self.now.date())
                spend = DevToolCostService.total(
                    subscription.seats.all(), start, self.now.date() + timedelta(days=1)
                )
                if spend <= 0:
                    continue
                findings.append(Finding(
                    kind=RecommendationKind.UNBUDGETED_SPEND,
                    fingerprint=f"unbudgeted:devtools:{subscription.pk}",
                    title=f"Put a budget on {subscription.name}",
                    rationale=(
                        f"{subscription.name} has cost {money(spend)} so far this month in licences and premium "
                        "requests, and no budget counts developer-tool spend."
                    ),
                    action=(
                        "Create an organization or per-team budget with 'Developer tools' as its cost source, "
                        "so premium-request overage is caught as it builds."
                    ),
                    savings=ZERO,
                    current_cost=spend,
                    confidence=Confidence.HIGH,
                    effort=Effort.LOW,
                    targets={"subscription": subscription},
                ))
        return findings

    # -- Running -------------------------------------------------------------------

    def findings(self) -> list[Finding]:
        usage = self._agent_model_usage()
        runtime = sum((row["cost"] or ZERO for row in usage), ZERO)
        self.min_savings = max(MIN_SAVINGS_FLOOR, min(MIN_MONTHLY_SAVINGS, runtime * MIN_SAVINGS_SHARE)).quantize(CENT)
        agents = {
            agent.pk: agent
            for agent in AIAgent.objects.for_organization(self.organization).select_related("team", "provider")
        }
        models = {model.pk: model for model in AIModel.objects.for_organization(self.organization)}
        found: list[Finding] = []
        for analyzer in (
            lambda: self.model_rightsizing(usage, agents, models),
            lambda: self.prompt_caching(usage, agents, models),
            lambda: self.output_length(usage, agents, models),
            lambda: self.failure_waste(agents),
            self.idle_seats,
            self.seat_plans,
            lambda: self.unbudgeted_spend(agents),
        ):
            try:
                found.extend(analyzer())
            except Exception:
                logger.exception("Cost optimization analyzer failed for %s", self.organization)
        return found

    def run(self) -> dict:
        findings = self.findings()
        seen, created, updated, reopened = set(), 0, 0, 0
        with transaction.atomic():
            for finding in findings:
                seen.add(finding.fingerprint)
                values = {
                    "kind": finding.kind,
                    "category": KIND_CATEGORIES[finding.kind],
                    "title": finding.title[:300],
                    "rationale": finding.rationale,
                    "action": finding.action,
                    "estimated_monthly_savings": finding.savings,
                    "current_monthly_cost": Decimal(finding.current_cost).quantize(CENT),
                    "confidence": finding.confidence,
                    "effort": finding.effort,
                    "evidence": finding.evidence,
                    "last_detected_at": self.now,
                    "agent": finding.targets.get("agent"),
                    "model": finding.targets.get("model"),
                    "suggested_model": finding.targets.get("suggested_model"),
                    "team": finding.targets.get("team"),
                    "subscription": finding.targets.get("subscription"),
                }
                recommendation = CostRecommendation.objects.filter(
                    organization=self.organization, fingerprint=finding.fingerprint
                ).first()
                if recommendation is None:
                    CostRecommendation.objects.create(
                        organization=self.organization, fingerprint=finding.fingerprint,
                        first_detected_at=self.now, **values,
                    )
                    created += 1
                    continue
                for name, value in values.items():
                    setattr(recommendation, name, value)
                if recommendation.status == RecommendationStatus.RESOLVED:
                    recommendation.status = RecommendationStatus.OPEN
                    reopened += 1
                recommendation.save()
                updated += 1

            resolved = (
                CostRecommendation.objects.for_organization(self.organization)
                .filter(status__in=[RecommendationStatus.OPEN, RecommendationStatus.ACCEPTED])
                .exclude(fingerprint__in=seen)
                .update(status=RecommendationStatus.RESOLVED, updated_at=self.now)
            )
        return {
            "found": len(findings), "created": created, "updated": updated,
            "reopened": reopened, "resolved": resolved,
        }

    @classmethod
    def run_all(cls) -> int:
        from apps.organizations.models import Organization

        return sum(cls(organization).run()["found"] for organization in Organization.objects.all())


def savings_summary(organization) -> dict:
    queryset = CostRecommendation.objects.for_organization(organization)
    actionable = queryset.filter(status__in=[RecommendationStatus.OPEN, RecommendationStatus.ACCEPTED])
    by_category = {
        row["category"]: row["savings"] or ZERO
        for row in actionable.values("category").annotate(savings=Sum("estimated_monthly_savings"))
    }
    open_savings = actionable.aggregate(total=Sum("estimated_monthly_savings"))["total"] or ZERO
    return {
        "open_savings": open_savings,
        "open_annual": open_savings * 12,
        "implemented_savings": queryset.filter(status=RecommendationStatus.IMPLEMENTED).aggregate(
            total=Sum("estimated_monthly_savings")
        )["total"] or ZERO,
        "open_count": actionable.count(),
        "by_category": by_category,
    }
