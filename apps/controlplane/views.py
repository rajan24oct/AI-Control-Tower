"""IRGF Control Plane pages: the overview (M12), the drift register (M13), verifications (link 07)."""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count
from django.shortcuts import get_object_or_404
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView

from apps.accounts.roles import Perm
from apps.controlplane import irgf
from apps.controlplane.models import (
    UNRESOLVED_STATUSES,
    DriftRecord,
    DriftStatus,
    Resolution,
    Verification,
    VerificationOutcome,
)
from apps.controlplane.services import ControlPlane, ControlPlaneError, DriftService
from apps.core.responses import action_response
from apps.core.views import OrganizationScopedMixin, ScopedDetailMixin

TIERS = (1, 2, 3, 4)


def can_run(request) -> bool:
    user, organization = request.user, request.organization
    return user.has_org_permission(organization, Perm.RISK_MANAGE) or user.has_org_permission(
        organization, Perm.GATE_DECIDE
    )


def response_table() -> list[dict]:
    """IRGF chapter 19's response windows, as the page shows them."""
    rows = [
        {"label": irgf.GROUP_LABELS[group], "cells": [irgf.RESPONSES[(group, tier)].label for tier in TIERS]}
        for group in (irgf.OPERATIONAL, irgf.PROTECTIVE, irgf.BEHAVIORAL, irgf.POLICY)
    ]
    rows.append({"label": "Agent boundary divergence", "cells": [irgf.INCIDENT.label] * len(TIERS), "incident": True})
    return rows


def _text(value) -> str:
    if value in (None, "", [], {}):
        return "—"
    if isinstance(value, list):
        return ", ".join(_text(item) for item in value)
    if isinstance(value, dict):
        return "; ".join(f"{key.replace('_', ' ')}: {_text(item)}" for key, item in value.items())
    return str(value)


def _url(name, *args):
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return None


EVIDENCE_LINKS = {
    "execution": ("executions:detail", True),
    "tool_call": ("mcp_gateway:calls", False),
    "evaluation": ("evaluations:run_detail", True),
    "policy": ("policies:detail", True),
    "budget_alert": ("costs:budgets", False),
    "tool": ("mcp_gateway:tools", False),
}
EVIDENCE_LABELS = {
    "execution": "Run", "tool_call": "Tool call", "evaluation": "Evaluation", "policy": "Policy",
    "budget_alert": "Budget alert", "tool": "Tool", "message": "Delegation",
}


class OverviewView(LoginRequiredMixin, TemplateView):
    template_name = "controlplane/overview.html"
    extra_context = {
        "page_title": "Control Plane",
        "page_subtitle": "IRGF M12: what runs, compared with what was authorized",
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization
        now = timezone.now()
        records = DriftRecord.objects.for_organization(organization)
        unresolved = records.filter(status__in=UNRESOLVED_STATUSES)

        cells: dict = {}
        for row in unresolved.values("category", "tier").annotate(n=Count("pk")):
            key = (row["category"], row["tier"] if row["tier"] in TIERS else None)
            cells[key] = cells.get(key, 0) + row["n"]
        matrix = [
            {
                "category": category,
                "cells": [(tier, cells.get((category.key, tier), 0)) for tier in (*TIERS, None)],
                "total": sum(cells.get((category.key, tier), 0) for tier in (*TIERS, None)),
            }
            for category in irgf.CATEGORIES.values()
        ]
        stats = ControlPlane.verification_stats(organization)
        context.update({
            "needs_decision": records.filter(status=DriftStatus.OPEN).count(),
            "incidents": unresolved.filter(is_incident=True).count(),
            "overdue": records.filter(status=DriftStatus.OPEN, response_due__lt=now).count(),
            "auto_suspended": records.filter(auto_suspended=True, first_seen_at__gte=now - timedelta(days=30)).count(),
            "verified": sum(row["count"] for row in stats),
            "late": sum(row["late"] for row in stats),
            "stats": stats,
            "matrix": matrix,
            "responses": response_table(),
            "tiers": TIERS,
            "incident_list": unresolved.filter(is_incident=True).select_related("agent", "application")[:6],
            "recent": records.filter(status=DriftStatus.OPEN).select_related("agent", "application")
            .order_by("-last_seen_at")[:8],
            "can_run": can_run(self.request),
        })
        return context


class RunView(LoginRequiredMixin, View):
    """Run one pass of the Control Plane now, rather than waiting for the minute."""

    def post(self, request, *args, **kwargs):
        redirect_to = reverse("controlplane:overview")
        if not can_run(request):
            return action_response(
                request, message="Running the Control Plane needs risk.manage or gate.decide.",
                tone="danger", redirect_to=redirect_to, status=403,
            )
        result = ControlPlane.sweep(request.organization, settle=timedelta(0))
        verified = result["verified"]
        observed = sum(result["observed"].values())
        return action_response(
            request,
            message=(
                f"Verified {verified['tool_calls']} tool call(s) and {verified['executions']} run(s); "
                f"{observed} new observation(s); {result['escalated']} escalated."
            ),
            redirect_to=redirect_to,
        )


class DriftListView(OrganizationScopedMixin, ListView):
    model = DriftRecord
    template_name = "controlplane/drift_list.html"
    context_object_name = "records"
    paginate_by = 25
    ordering = ["-last_seen_at"]
    extra_context = {
        "page_title": "Drift",
        "page_subtitle": "IRGF M13: every divergence from the authorized state, routed by category and tier",
    }
    search_fields = ["title", "agent__name", "application__name", "signal"]
    sort_fields = {
        "subject": ["agent__name", "application__name"],
        "category": "category",
        "tier": "tier",
        "status": "status",
        "count": "occurrences",
        "seen": "last_seen_at",
        "due": "response_due",
    }
    filter_fields = {"category": "category", "tier": "tier"}

    def get_base_queryset(self):
        return super().get_base_queryset().select_related("agent", "application")

    def get_queryset(self):
        queryset = super().get_queryset()
        params = self.request.GET
        if params.get("incident"):
            queryset = queryset.filter(is_incident=True)
        if params.get("route") == "quiet":
            queryset = queryset.filter(route__in=irgf.QUIET_ROUTES)
        if params.get("overdue"):
            queryset = queryset.filter(status=DriftStatus.OPEN, response_due__lt=timezone.now())
        agent = params.get("agent", "")
        if agent:
            try:
                queryset = queryset.filter(agent_id=uuid.UUID(agent))
            except ValueError:
                queryset = queryset.none()
        status = params.get("status", "")
        if status == "all":
            return queryset
        if status in DriftStatus.values:
            return queryset.filter(status=status)
        return queryset.filter(status__in=UNRESOLVED_STATUSES)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        records = DriftRecord.objects.for_organization(self.request.organization)
        context.update({
            "needs_decision": records.filter(status=DriftStatus.OPEN).count(),
            "incidents": records.filter(status__in=UNRESOLVED_STATUSES, is_incident=True).count(),
            "overdue": records.filter(status=DriftStatus.OPEN, response_due__lt=timezone.now()).count(),
            "excepted": records.filter(status__in=(DriftStatus.EXCEPTING, DriftStatus.EXCEPTED)).count(),
            "statuses": DriftStatus.choices,
            "categories": [(category.key, category.label) for category in irgf.CATEGORIES.values()],
            "tiers": TIERS,
        })
        return context


class DriftDetailView(ScopedDetailMixin, DetailView):
    model = DriftRecord
    template_name = "controlplane/drift_detail.html"
    context_object_name = "record"
    extra_context = {"page_title": "Drift Record"}

    def get_queryset(self):
        return super().get_queryset().select_related(
            "agent", "application", "authorization", "change_record", "exception", "resolved_by", "recurrence_of"
        )

    @staticmethod
    def _evidence(item: dict) -> dict:
        kind = item.get("kind", "")
        name, takes_id = EVIDENCE_LINKS.get(kind, (None, False))
        url = None
        if name:
            url = _url(name, item.get("id")) if takes_id else _url(name)
        return {**item, "kind_label": EVIDENCE_LABELS.get(kind, kind), "url": url}

    def get_context_data(self, **kwargs):
        from apps.lifecycle import irgf as lifecycle_irgf
        from apps.lifecycle.models import ChangeStatus

        context = super().get_context_data(**kwargs)
        record = self.object
        user = self.request.user
        changes = record.subject.change_records.exclude(
            status__in=(ChangeStatus.REJECTED, ChangeStatus.REVERTED, ChangeStatus.SUPERSEDED)
        ).order_by("-detected_at")[:10]
        signal = record.signal_info
        context.update({
            "signal": signal,
            "response": record.response,
            "expected_text": _text(record.expected),
            "observed_text": _text(record.observed),
            "evidence": [self._evidence(item) for item in record.evidence],
            "can_triage": DriftService.can_triage(user, record),
            "can_escalate": DriftService.can_escalate(user, record),
            "ratify_by": signal.ratify if signal else irgf.RATIFY_BY_NOTE,
            "change_choices": changes,
            "max_days": lifecycle_irgf.MAX_EXCEPTION_DAYS,
            "tier_requires_control": record.triage_tier >= lifecycle_irgf.INDEPENDENT_FROM_TIER,
        })
        return context


class DriftResolveView(LoginRequiredMixin, View):
    """Revert, ratify, except or escalate: IRGF's four resolution endpoints."""

    def post(self, request, pk, *args, **kwargs):
        record = get_object_or_404(
            DriftRecord.objects.for_organization(request.organization).select_related("agent", "application"),
            pk=pk,
        )
        redirect_to = reverse("controlplane:drift_detail", args=[record.pk])
        action = request.POST.get("action", "")
        allowed = (
            DriftService.can_triage(request.user, record)
            if action in (Resolution.REVERT, Resolution.RATIFY)
            else DriftService.can_escalate(request.user, record)
        )
        if not allowed:
            return action_response(
                request, message="Your role does not allow this resolution.", tone="danger",
                redirect_to=redirect_to, status=403,
            )
        post = request.POST
        try:
            if action == Resolution.REVERT:
                DriftService.revert(record, actor=request.user, note=post.get("note", ""))
            elif action == Resolution.RATIFY:
                change = None
                if post.get("change"):
                    try:
                        change = record.subject.change_records.filter(pk=uuid.UUID(post["change"])).first()
                    except ValueError:
                        change = None
                DriftService.ratify(record, actor=request.user, note=post.get("note", ""), change=change)
            elif action == Resolution.EXCEPT:
                try:
                    days = int(post.get("duration_days", ""))
                except ValueError:
                    raise ControlPlaneError("Give the exception's duration in days.") from None
                DriftService.except_(
                    record, actor=request.user, justification=post.get("justification", ""),
                    compensating_control=post.get("compensating_control", ""), duration_days=days,
                )
            elif action == Resolution.ESCALATE:
                DriftService.escalate(
                    record, actor=request.user, reason=post.get("reason", ""),
                    suspend_authorization=post.get("suspend_authorization") in ("on", "1", "true"),
                )
            else:
                raise ControlPlaneError("Choose revert, ratify, except or escalate.")
        except ControlPlaneError as exc:
            return action_response(request, message=str(exc), tone="warning", redirect_to=redirect_to, status=409)

        record.refresh_from_db()
        message = {
            DriftStatus.RATIFYING: "Waiting for Gate G4: the record closes when the change is approved.",
            DriftStatus.EXCEPTING: "Exception requested under M17. It takes effect once signed.",
        }.get(record.status, f"Resolved: {record.get_resolution_display().lower()}.")
        return action_response(request, message=message, redirect_to=redirect_to)


class VerificationListView(OrganizationScopedMixin, ListView):
    model = Verification
    template_name = "controlplane/verifications.html"
    context_object_name = "verifications"
    paginate_by = 50
    ordering = ["-verified_at"]
    extra_context = {
        "page_title": "Verifications",
        "page_subtitle": "IRGF agent chain link 07: every action checked after it happens, within a ceiling",
    }
    search_fields = ["agent__name"]
    sort_fields = {"verified": "verified_at", "latency": "latency_ms", "tier": "tier", "outcome": "outcome"}
    filter_fields = {"outcome": "outcome", "tier": "tier"}

    def get_base_queryset(self):
        return super().get_base_queryset().select_related(
            "agent", "execution", "tool_call", "message__sender", "message__recipient"
        )

    def get_queryset(self):
        queryset = super().get_queryset()
        params = self.request.GET
        if params.get("late"):
            queryset = queryset.filter(late=True)
        kind = params.get("kind", "")
        if kind == "tool_call":
            queryset = queryset.filter(tool_call__isnull=False)
        elif kind == "execution":
            queryset = queryset.filter(execution__isnull=False)
        elif kind == "message":
            queryset = queryset.filter(message__isnull=False)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            "stats": ControlPlane.verification_stats(self.request.organization),
            "outcomes": VerificationOutcome.choices,
            "tiers": TIERS,
        })
        return context
