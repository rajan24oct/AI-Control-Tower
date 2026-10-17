"""A2A Gateway views: the registry in one place, and what the gateway is doing."""

from __future__ import annotations

import json
from collections import Counter
from datetime import timedelta

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Avg, Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.generic import DetailView, ListView, TemplateView, View

from apps.a2a.models import (
    A2AAccessMode,
    A2AAgentCard,
    A2AChannel,
    A2AContract,
    A2ASkill,
    A2ATask,
    A2ATransport,
    CardOrigin,
    DeliveryStatus,
    SkillStatus,
    TaskState,
)
from apps.a2a.registry import A2ARegistryService, published_card
from apps.a2a.skill_docs import skill_markdown, skills_archive
from apps.accounts.roles import Perm
from apps.agents.models import AIAgent
from apps.core.constants import HealthStatus, LifecycleStatus, RiskLevel
from apps.core.responses import action_response
from apps.core.views import OrganizationScopedMixin, ScopedDetailMixin
from apps.gateway.models import CircuitState

#: Reviewers work top-down, so what needs a decision comes first.
_SKILL_ORDER = {
    SkillStatus.PENDING_REVIEW: 0,
    SkillStatus.ENABLED: 1,
    SkillStatus.DISABLED: 2,
    SkillStatus.REMOVED: 3,
}


def _can(request, permission: str) -> bool:
    return request.user.has_org_permission(request.organization, permission)


def address_url(request, card) -> str:
    """The absolute URL other agents reach a card at."""
    return request.build_absolute_uri(reverse("api:a2a-endpoint", args=[card.slug]))


def communication_map(organization, *, days: int = 7, limit: int = 8) -> dict:
    """Who may hand work to whom, and who tried, as a sender-by-recipient grid.

    A cell holds the contract for the pair, if there is one, and the pair's
    traffic over the last `days`. Traffic without a contract is the interesting
    part: it is either an open agent, or something the gateway refused.
    """
    since = timezone.now() - timedelta(days=days)
    cells: dict = {}
    for contract in A2AContract.objects.for_organization(organization).select_related("sender", "recipient"):
        cells[(contract.sender_id, contract.recipient_id)] = {
            "contract": contract, "delivered": 0, "blocked": 0, "total": 0,
        }
    traffic = (
        A2ATask.objects.for_organization(organization)
        .filter(created_at__gte=since, sender__isnull=False, recipient__isnull=False)
        .values("sender_id", "recipient_id")
        .annotate(
            total=Count("id"),
            delivered=Count("id", filter=Q(status=DeliveryStatus.DELIVERED)),
            blocked=Count("id", filter=Q(status=DeliveryStatus.BLOCKED)),
        )
        .order_by()
    )
    for row in traffic:
        cell = cells.setdefault(
            (row["sender_id"], row["recipient_id"]), {"contract": None, "delivered": 0, "blocked": 0, "total": 0}
        )
        cell.update(total=row["total"], delivered=row["delivered"], blocked=row["blocked"])

    senders, recipients = Counter(), Counter()
    for (sender, recipient), cell in cells.items():
        weight = cell["total"] + (1 if cell["contract"] else 0)
        senders[sender] += weight
        recipients[recipient] += weight
    agents = AIAgent.objects.in_bulk([pk for pk, _ in senders.most_common(limit)])
    cards = A2AAgentCard.objects.in_bulk([pk for pk, _ in recipients.most_common(limit)])
    columns = [cards[pk] for pk, _ in recipients.most_common(limit) if pk in cards]
    rows = [
        {"sender": agents[pk], "cells": [cells.get((pk, card.pk)) for card in columns]}
        for pk, _ in senders.most_common(limit)
        if pk in agents
    ]
    return {"columns": columns, "rows": rows, "days": days}


class A2AOverviewView(LoginRequiredMixin, TemplateView):
    """What needs a decision, who may reach whom, and what the gateway is doing."""

    template_name = "a2a/overview.html"
    extra_context = {
        "page_title": "A2A Gateway",
        "page_subtitle": "Agent-to-agent communication, governed: who may hand work to whom, and what crosses",
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization
        if organization is None:
            return context

        now = timezone.now()
        cards = A2AAgentCard.objects.for_organization(organization).select_related("agent")
        skills = A2ASkill.objects.for_organization(organization)
        contracts = A2AContract.objects.for_organization(organization)
        all_tasks = A2ATask.objects.for_organization(organization)
        tasks = all_tasks.filter(created_at__gte=now - timedelta(hours=24))
        by_status = dict(tasks.values_list("status").annotate(total=Count("id")).order_by())

        context["stats"] = {
            "agents": cards.count(),
            "serving": sum(
                1 for card in cards.filter(status=LifecycleStatus.ACTIVE, is_approved=True)
                if card.is_gateway_reachable
            ),
            "external": cards.filter(origin=CardOrigin.EXTERNAL).count(),
            "skills_enabled": skills.filter(status=SkillStatus.ENABLED).count(),
            "skills_pending": skills.filter(status=SkillStatus.PENDING_REVIEW).count(),
            "contracts": contracts.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now)).count(),
            "tasks": sum(by_status.values()),
            "blocked": by_status.get(DeliveryStatus.BLOCKED, 0),
            "failed": by_status.get(DeliveryStatus.FAILED, 0) + by_status.get(DeliveryStatus.UNAVAILABLE, 0),
            "personal_data": tasks.exclude(personal_data=[]).count(),
            "avg_latency": tasks.filter(latency_ms__isnull=False).aggregate(avg=Avg("latency_ms"))["avg"] or 0,
        }

        # What needs someone: these lead the page rather than sit in a table.
        context["open_circuits"] = cards.filter(circuit_state=CircuitState.OPEN)
        context["pending_skills"] = (
            skills.filter(status=SkillStatus.PENDING_REVIEW)
            .select_related("card")
            .order_by("-changed_since_review", "card__name", "skill_id")[:8]
        )
        context["awaiting_approval"] = cards.filter(is_approved=False, status=LifecycleStatus.ACTIVE)
        context["sync_failures"] = cards.exclude(last_sync_error="")
        context["missing_credentials"] = [
            card for card in cards.filter(status=LifecycleStatus.ACTIVE) if not card.has_resolvable_credential
        ]
        context["expiring"] = contracts.filter(
            expires_at__gt=now, expires_at__lte=now + timedelta(days=7)
        ).select_related("sender", "recipient")

        context["refusals"] = (
            tasks.filter(status=DeliveryStatus.BLOCKED)
            .exclude(limit_breached="")
            .values("limit_breached")
            .annotate(total=Count("id"))
            .order_by("-total")
        )
        context["map"] = communication_map(organization)
        context["recent"] = all_tasks.select_related("sender", "recipient")[:12]
        context["directory_url"] = self.request.build_absolute_uri(reverse("api:a2a-directory"))
        return context


class CardListView(OrganizationScopedMixin, ListView):
    model = A2AAgentCard
    template_name = "a2a/card_list.html"
    context_object_name = "cards"
    paginate_by = 25
    ordering = ["name"]
    extra_context = {
        "page_title": "A2A Agent Registry",
        "page_subtitle": "Every agent the organization's agents may hand work to, inside it and out",
    }
    search_fields = ["name", "slug", "description", "publisher", "endpoint"]
    sort_fields = {
        "agent": "name",
        "origin": "origin",
        "transport": "transport",
        "skills": "enabled_skills",
        "contracts": "contract_count",
        "access": "access_mode",
        "health": ("health_status", HealthStatus.values),
        "approved": "is_approved",
        "status": "status",
    }
    filter_fields = {
        "origin": "origin",
        "transport": "transport",
        "approved": "is_approved",
        "access": "access_mode",
        "status": "status",
    }

    def get_base_queryset(self):
        return (
            super()
            .get_base_queryset()
            .select_related("owner", "team", "agent")
            .annotate(
                enabled_skills=Count("skills", filter=Q(skills__status=SkillStatus.ENABLED), distinct=True),
                pending_skills=Count("skills", filter=Q(skills__status=SkillStatus.PENDING_REVIEW), distinct=True),
                contract_count=Count("contracts", distinct=True),
            )
            # Meta.ordering does not survive an aggregate, so pages would shuffle.
            .order_by("name")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "origins": CardOrigin.choices,
                "transports": A2ATransport.choices,
                "access_modes": A2AAccessMode.choices,
                "statuses": LifecycleStatus.choices,
                "can_manage": _can(self.request, Perm.A2A_MANAGE),
            }
        )
        return context


class CardDetailView(ScopedDetailMixin, DetailView):
    model = A2AAgentCard
    template_name = "a2a/card_detail.html"
    context_object_name = "card"
    extra_context = {"page_title": "A2A Agent"}

    def get_queryset(self):
        return super().get_queryset().select_related("owner", "team", "approved_by", "agent")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        card = self.object
        skills = sorted(
            card.skills.select_related("reviewed_by"),
            key=lambda skill: (_SKILL_ORDER.get(skill.status, 9), skill.skill_id),
        )
        url = address_url(self.request, card)
        context.update(
            {
                "skills": skills,
                "skill_counts": Counter(skill.status for skill in skills),
                "contracts": card.contracts.select_related("sender"),
                "outbound": card.agent.a2a_contracts.select_related("recipient") if card.agent_id else [],
                "recent_tasks": card.tasks.select_related("sender", "skill")[:15],
                "address_url": url,
                "published": json.dumps(published_card(card, url=url), indent=2),
                "can_manage": _can(self.request, Perm.A2A_MANAGE),
                "can_approve": _can(self.request, Perm.A2A_APPROVE),
                "can_view_content": _can(self.request, Perm.PROMPT_VIEW),
            }
        )
        return context


class SkillListView(OrganizationScopedMixin, ListView):
    """Every skill every agent offers, and the decision made about each."""

    model = A2ASkill
    template_name = "a2a/skill_list.html"
    context_object_name = "skills"
    paginate_by = 50
    ordering = ["card__name", "skill_id"]
    extra_context = {
        "page_title": "A2A Skill Catalog",
        "page_subtitle": "Every skill every agent offers, and what governance decided about it",
    }
    search_fields = ["skill_id", "name", "description", "card__name"]
    sort_fields = {
        "skill": "skill_id",
        "agent": "card__name",
        "risk": ("risk_level", RiskLevel.values),
        "status": "status",
        "seen": "last_seen_at",
    }
    filter_fields = {"status": "status", "agent": "card_id", "risk": "risk_level"}

    def get_base_queryset(self):
        return super().get_base_queryset().select_related("card", "reviewed_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization
        counts = dict(
            A2ASkill.objects.for_organization(organization).values_list("status").annotate(total=Count("id")).order_by()
        )
        context.update(
            {
                "skill_statuses": [(value, label, counts.get(value, 0)) for value, label in SkillStatus.choices],
                "risk_levels": RiskLevel.choices,
                "cards": A2AAgentCard.objects.for_organization(organization).only("pk", "name"),
                "can_approve": _can(self.request, Perm.A2A_APPROVE),
            }
        )
        return context


class SkillDetailView(ScopedDetailMixin, DetailView):
    """One skill: what it does, what governance decided, who may call it, and its SKILL.md."""

    model = A2ASkill
    template_name = "a2a/skill_detail.html"
    context_object_name = "skill"

    def get_queryset(self):
        return super().get_queryset().select_related("card", "card__team", "card__owner", "reviewed_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        skill = self.object
        since = timezone.now() - timedelta(days=30)
        tasks = A2ATask.objects.for_organization(self.request.organization).filter(skill=skill)
        recent = tasks.filter(created_at__gte=since)
        contracts = [
            contract
            for contract in skill.card.contracts.select_related("sender").order_by("sender__name")
            if contract.permits(skill.skill_id)
        ]
        address = address_url(self.request, skill.card)
        context.update(
            {
                "page_title": skill.label,
                "page_subtitle": f"A2A skill · {skill.qualified_name}",
                "address": address,
                "contracts": contracts,
                "recent_tasks": tasks.select_related("sender").order_by("-created_at")[:10],
                "traffic": {
                    "total": recent.count(),
                    "delivered": recent.filter(status=DeliveryStatus.DELIVERED).count(),
                    "refused": recent.filter(status=DeliveryStatus.BLOCKED).count(),
                },
                "skill_md": skill_markdown(skill, address),
                "can_approve": _can(self.request, Perm.A2A_APPROVE),
            }
        )
        return context


class SkillDownloadView(SkillListView):
    """The catalog as SKILL.md files: one skill, or every skill the filters match as a zip."""

    paginate_by = None

    def get(self, request, *args, **kwargs):
        if kwargs.get("pk"):
            skill = get_object_or_404(self.get_base_queryset(), pk=kwargs["pk"])
            response = HttpResponse(
                skill_markdown(skill, address_url(request, skill.card)),
                content_type="text/markdown; charset=utf-8",
            )
            name = slugify(skill.skill_id) or skill.pk.hex[:8]
            response["Content-Disposition"] = f'attachment; filename="{skill.card.slug}-{name}-SKILL.md"'
            return response

        skills = self.apply_sort(self.get_queryset())
        archive = skills_archive(skills, lambda card: address_url(request, card))
        response = HttpResponse(archive, content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="a2a-skills-{timezone.now():%Y%m%d}.zip"'
        return response


class ContractListView(OrganizationScopedMixin, ListView):
    """The communication graph as governance granted it: every sender, recipient and ceiling."""

    model = A2AContract
    template_name = "a2a/contract_list.html"
    context_object_name = "contracts"
    paginate_by = 50
    ordering = ["recipient__name", "sender__name"]
    extra_context = {
        "page_title": "A2A Contracts",
        "page_subtitle": "Which agent may hand work to which, for what, with how sensitive a payload",
    }
    search_fields = ["sender__name", "sender__agent_id", "recipient__name", "purpose"]
    sort_fields = {
        "sender": "sender__name",
        "recipient": "recipient__name",
        "ceiling": "max_data_classification",
        "rate": "max_messages_per_hour",
        "expires": "expires_at",
    }
    filter_fields = {"recipient": "recipient_id", "sender": "sender_id"}

    def get_base_queryset(self):
        return (
            super()
            .get_base_queryset()
            .select_related("sender", "recipient")
            .annotate(
                sent_week=Count(
                    "tasks", filter=Q(tasks__created_at__gte=timezone.now() - timedelta(days=7),
                                      tasks__status=DeliveryStatus.DELIVERED),
                )
            )
            # Meta.ordering does not survive an aggregate, so pages would shuffle.
            .order_by("recipient__name", "sender__name")
        )

    def get_queryset(self):
        queryset = super().get_queryset()
        state = self.request.GET.get("state", "")
        now = timezone.now()
        if state == "active":
            queryset = queryset.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        elif state == "expired":
            queryset = queryset.filter(expires_at__lte=now)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "cards": A2AAgentCard.objects.for_organization(self.request.organization).only("pk", "name"),
                "can_approve": _can(self.request, Perm.A2A_APPROVE),
            }
        )
        return context


class TaskListView(OrganizationScopedMixin, ListView):
    model = A2ATask
    template_name = "a2a/task_list.html"
    context_object_name = "tasks"
    paginate_by = 50
    ordering = ["-created_at"]
    extra_context = {
        "page_title": "A2A Communications",
        "page_subtitle": "Every hand-off the gateway delivered, and every one it refused",
    }
    # Message text is not searchable here: reading it needs prompt.view.
    search_fields = ["recipient_name", "skill_name", "reason", "context_id", "sender__name", "sender__agent_id"]
    sort_fields = {
        "when": "created_at",
        "from": "sender__name",
        "to": "recipient_name",
        "status": "status",
        "state": "state",
        "hops": "hops",
    }
    filter_fields = {
        "status": "status",
        "state": "state",
        "agent": "recipient_id",
        "sender": "sender_id",
        "channel": "channel",
        "classification": "data_classification",
    }

    def get_base_queryset(self):
        return super().get_base_queryset().select_related(
            "sender", "recipient", "skill", "service_account", "execution"
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "delivery_statuses": DeliveryStatus.choices,
                "task_states": TaskState.choices,
                "channels": A2AChannel.choices,
                "cards": A2AAgentCard.objects.for_organization(self.request.organization).only("pk", "name"),
                "can_view_content": _can(self.request, Perm.PROMPT_VIEW),
            }
        )
        return context


class TaskDetailView(ScopedDetailMixin, DetailView):
    model = A2ATask
    template_name = "a2a/task_detail.html"
    context_object_name = "task"
    extra_context = {"page_title": "A2A Task"}

    def get_queryset(self):
        return super().get_queryset().select_related(
            "sender", "recipient", "recipient_agent", "skill", "contract", "service_account", "execution", "parent",
            "parent__sender",
        )

    def get_context_data(self, **kwargs):
        from apps.controlplane.models import Verification

        context = super().get_context_data(**kwargs)
        task = self.object
        context.update(
            {
                "handoffs": task.handoffs.select_related("sender", "recipient"),
                "verification": Verification.objects.filter(a2a_task=task).first(),
                "can_view_content": _can(self.request, Perm.PROMPT_VIEW),
                "message_json": json.dumps(task.message, indent=2, default=str) if task.message else "",
            }
        )
        return context


# -- Actions ------------------------------------------------------------------


class _CardAction(LoginRequiredMixin, View):
    permission = Perm.A2A_MANAGE
    denied_message = "Your role does not allow managing A2A agents."

    def post(self, request, pk, *args, **kwargs):
        redirect_to = request.POST.get("next") or reverse("a2a:card_detail", args=[pk])
        if not _can(request, self.permission):
            return action_response(
                request, message=self.denied_message, tone="danger", redirect_to=redirect_to, status=403,
            )
        card = get_object_or_404(A2AAgentCard.objects.for_organization(request.organization), pk=pk)
        return self.act(request, card, redirect_to)

    def act(self, request, card, redirect_to):  # pragma: no cover - abstract
        raise NotImplementedError


class SyncCardView(_CardAction):
    """Read what the agent offers now. New or changed skills wait for review."""

    def act(self, request, card, redirect_to):
        result = A2ARegistryService.sync(card, actor=request.user)
        return action_response(
            request, message=result.summary, tone="success" if result.ok else "danger",
            redirect_to=redirect_to, status=200 if result.ok else 502,
        )


class HealthCheckCardView(_CardAction):
    def act(self, request, card, redirect_to):
        check = A2ARegistryService.health_check(card)
        if check is None:
            message, tone = f"{card.get_transport_display()} agents cannot be reached by the gateway.", "warning"
        elif check.healthy:
            latency = f" in {check.latency_ms} ms" if check.latency_ms is not None else ""
            message, tone = f"{card.name} answered{latency}. {check.message or ''}".strip(), "success"
        else:
            message, tone = f"{card.name} did not answer: {check.message}", "danger"
        return action_response(request, message=message, tone=tone, redirect_to=redirect_to)


class CardApprovalView(_CardAction):
    permission = Perm.A2A_APPROVE
    denied_message = "Your role does not allow approving A2A agents."

    def act(self, request, card, redirect_to):
        approve = request.POST.get("decision") == "approve"
        A2ARegistryService.set_approval(card, approved=approve, actor=request.user)
        message = (
            f"Approved {card.name}. Agents holding a contract may now ask it for its enabled skills."
            if approve
            else f"Revoked approval for {card.name}. No agent can reach it through the gateway."
        )
        return action_response(
            request, message=message, tone="success" if approve else "warning", redirect_to=redirect_to
        )


class SkillReviewView(LoginRequiredMixin, View):
    def post(self, request, pk, *args, **kwargs):
        redirect_to = request.POST.get("next") or reverse("a2a:skills")
        if not _can(request, Perm.A2A_APPROVE):
            return action_response(
                request, message="Your role does not allow reviewing A2A skills.",
                tone="danger", redirect_to=redirect_to, status=403,
            )
        skill = get_object_or_404(
            A2ASkill.objects.for_organization(request.organization).select_related("card"), pk=pk
        )
        decision = request.POST.get("decision")
        if decision not in {"enable", "disable"}:
            return action_response(
                request, message="Choose to enable or disable the skill.", tone="danger",
                redirect_to=redirect_to, status=400,
            )
        try:
            A2ARegistryService.review(
                skill, enable=decision == "enable", actor=request.user, note=request.POST.get("note", ""),
            )
        except ValueError as exc:
            return action_response(request, message=str(exc), tone="warning", redirect_to=redirect_to, status=409)
        verb = "Enabled" if decision == "enable" else "Disabled"
        return action_response(request, message=f"{verb} {skill.qualified_name}.", redirect_to=redirect_to)
