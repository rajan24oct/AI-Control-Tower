"""Approval Centre (section 19)."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.generic import DetailView, ListView, View

from apps.accounts.roles import Perm
from apps.approvals.models import ApprovalRequest, ApprovalRule, ApprovalStatus
from apps.approvals.services import ApprovalService
from apps.core.constants import RiskLevel
from apps.core.responses import action_response
from apps.core.views import OrganizationScopedMixin, ScopedDetailMixin


class ApprovalCentreView(OrganizationScopedMixin, ListView):
    model = ApprovalRequest
    template_name = "approvals/centre.html"
    context_object_name = "requests"
    paginate_by = 50
    ordering = ["-requested_at"]
    extra_context = {
        "page_title": "Approval Centre",
        "page_subtitle": "AI actions waiting on a human decision",
    }
    search_fields = ["request_id", "requested_action", "reason"]
    filter_fields = {"status": "status", "risk": "risk_level", "agent": "agent_id"}

    def get_base_queryset(self):
        return (
            super()
            .get_base_queryset()
            .select_related("agent", "application", "execution", "assigned_approver", "decision_by")
        )

    def get_queryset(self):
        queryset = super().get_queryset()
        # Default to the actionable queue rather than the full history.
        if not self.request.GET.get("status") and self.request.GET.get("all") != "1":
            queryset = queryset.filter(status=ApprovalStatus.PENDING)
        if self.request.GET.get("mine") == "1":
            queryset = queryset.filter(assigned_approver=self.request.user)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization
        base = ApprovalRequest.objects.for_organization(organization)

        counts = base.aggregate(
            pending=Count("id", filter=Q(status=ApprovalStatus.PENDING)),
            approved=Count("id", filter=Q(status=ApprovalStatus.APPROVED)),
            rejected=Count("id", filter=Q(status=ApprovalStatus.REJECTED)),
            expired=Count("id", filter=Q(status=ApprovalStatus.EXPIRED)),
        )
        pending = base.filter(status=ApprovalStatus.PENDING)

        context.update(
            {
                "statuses": ApprovalStatus.choices,
                "risk_levels": RiskLevel.choices,
                "counts": counts,
                "critical_pending": pending.filter(risk_level=RiskLevel.CRITICAL).count(),
                "showing_all": self.request.GET.get("all") == "1"
                or bool(self.request.GET.get("status")),
                "mine_only": self.request.GET.get("mine") == "1",
                "can_decide": self.request.user.has_org_permission(
                    organization, Perm.APPROVAL_DECIDE
                ),
                "rules": ApprovalRule.objects.for_organization(organization).filter(
                    status="active"
                ),
            }
        )
        return context


class ApprovalDetailView(ScopedDetailMixin, DetailView):
    model = ApprovalRequest
    template_name = "approvals/detail.html"
    context_object_name = "approval"
    extra_context = {"page_title": "Approval Request"}

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related(
                "agent", "application", "execution", "assigned_approver",
                "decision_by", "requested_by",
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_decide"] = self.request.user.has_org_permission(
            self.request.organization, Perm.APPROVAL_DECIDE
        )
        if self.object.execution_id:
            context["events"] = self.object.execution.events.order_by("sequence", "timestamp")
        return context


class ApprovalDecisionView(LoginRequiredMixin, View):
    """Record an approve or reject decision.

    Permission is checked here, in the backend, not merely by hiding buttons.
    """

    def post(self, request, pk, decision, *args, **kwargs):
        organization = request.organization
        approval = get_object_or_404(
            ApprovalRequest.objects.for_organization(organization), pk=pk
        )
        redirect_to = request.POST.get("next") or reverse("approvals:detail", args=[approval.pk])

        if not request.user.has_org_permission(organization, Perm.APPROVAL_DECIDE):
            return action_response(
                request,
                message="Your role does not allow deciding approval requests.",
                tone="danger",
                redirect_to=redirect_to,
                status=403,
            )

        notes = request.POST.get("notes", "").strip()
        try:
            if decision == "approve":
                ApprovalService.approve(approval, user=request.user, notes=notes)
                message, tone = f"{approval.request_id} approved.", "success"
            elif decision == "reject":
                ApprovalService.reject(approval, user=request.user, notes=notes)
                message, tone = f"{approval.request_id} rejected.", "warning"
            else:
                return action_response(
                    request, message="Unknown decision.", tone="danger",
                    redirect_to=redirect_to, status=400,
                )
        except ValueError as exc:
            return action_response(
                request, message=str(exc), tone="danger", redirect_to=redirect_to, status=409
            )

        return action_response(
            request,
            message=message,
            tone=tone,
            redirect_to=redirect_to,
            payload={"status": approval.status, "request_id": approval.request_id},
        )
