from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.generic import DetailView, ListView, View

from apps.accounts.roles import Perm
from apps.core.constants import LifecycleStatus, RiskLevel
from apps.core.responses import action_response
from apps.core.views import OrganizationScopedMixin, ScopedDetailMixin
from apps.policies.models import Policy, PolicyEffect, PolicyType, PolicyViolation


class PolicyListView(OrganizationScopedMixin, ListView):
    model = Policy
    template_name = "policies/policy_list.html"
    context_object_name = "policies"
    paginate_by = 50
    ordering = ["priority", "name"]
    extra_context = {
        "page_title": "AI Control Policies",
        "page_subtitle": "What the organization permits, gates and forbids",
    }
    search_fields = ["name", "description", "reference"]
    filter_fields = {"type": "policy_type", "effect": "effect", "status": "status"}

    def get_base_queryset(self):
        return super().get_base_queryset().annotate(
            violation_count=Count(
                "violations", filter=Q(violations__acknowledged_at__isnull=True)
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization
        base = Policy.objects.for_organization(organization)
        violations = PolicyViolation.objects.for_organization(organization)

        context.update(
            {
                "policy_types": PolicyType.choices,
                "effects": PolicyEffect.choices,
                "statuses": LifecycleStatus.choices,
                "counts": {
                    "active": base.filter(status=LifecycleStatus.ACTIVE).count(),
                    "blocking": base.filter(
                        status=LifecycleStatus.ACTIVE, effect=PolicyEffect.DENY
                    ).count(),
                    "open_violations": violations.filter(
                        acknowledged_at__isnull=True
                    ).count(),
                    "denials": violations.filter(effect=PolicyEffect.DENY).count(),
                },
                "recent_violations": violations.select_related("policy", "agent")[:10],
                "can_manage": self.request.user.has_org_permission(
                    organization, Perm.POLICY_MANAGE
                ),
            }
        )
        return context


class PolicyDetailView(ScopedDetailMixin, DetailView):
    model = Policy
    template_name = "policies/policy_detail.html"
    context_object_name = "policy"
    extra_context = {"page_title": "Policy"}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["violations"] = (
            self.object.violations.select_related("agent", "execution", "provider")[:25]
        )
        context["violation_total"] = self.object.violations.count()
        context["can_manage"] = self.request.user.has_org_permission(
            self.request.organization, Perm.POLICY_MANAGE
        )
        return context


class AcknowledgeViolationView(LoginRequiredMixin, View):
    def post(self, request, pk, *args, **kwargs):
        organization = request.organization
        redirect_to = request.POST.get("next") or reverse("policies:list")

        if not request.user.has_org_permission(organization, Perm.POLICY_MANAGE):
            return action_response(
                request,
                message="Your role does not allow acknowledging policy violations.",
                tone="danger",
                redirect_to=redirect_to,
                status=403,
            )

        violation = get_object_or_404(
            PolicyViolation.objects.for_organization(organization), pk=pk
        )
        if violation.is_acknowledged:
            return action_response(
                request,
                message="That finding was already acknowledged.",
                tone="warning",
                redirect_to=redirect_to,
            )

        violation.acknowledged_at = timezone.now()
        violation.acknowledged_by = request.user
        violation.save(update_fields=["acknowledged_at", "acknowledged_by", "updated_at"])

        return action_response(
            request,
            message=f"Acknowledged the finding against {violation.policy.name}.",
            redirect_to=redirect_to,
        )
