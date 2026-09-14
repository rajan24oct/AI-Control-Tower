"""IRGF S2 views: the classification register, scoring and countersigning."""

from __future__ import annotations

import json

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView, ListView

from apps.accounts.roles import Perm
from apps.agents.models import AIAgent
from apps.applications_registry.models import AIApplication
from apps.classification import irgf
from apps.classification.forms import CountersignForm, ScoreForm
from apps.classification.models import (
    CURRENT_STATUSES,
    TIER_CHOICES,
    ClassificationStatus,
    RiskClassification,
)
from apps.classification.services import ClassificationError, ClassificationService
from apps.core.views import OrganizationScopedMixin, ScopedDetailMixin

SUBJECT_MODELS = {"agents": AIAgent, "applications": AIApplication}


def _can(request, permission: str) -> bool:
    return request.user.has_org_permission(request.organization, permission)


def can_score(request, subject) -> bool:
    """The delivery side scores its own systems; the risk function may too."""
    manage = Perm.AGENT_MANAGE if isinstance(subject, AIAgent) else Perm.APPLICATION_MANAGE
    return _can(request, manage) or _can(request, Perm.RISK_MANAGE)


def can_countersign(request) -> bool:
    """IRGF M6 gives countersignature to the risk function."""
    return _can(request, Perm.RISK_MANAGE)


def subject_kind(subject) -> str:
    return "agents" if isinstance(subject, AIAgent) else "applications"


def subject_url(subject) -> str:
    if isinstance(subject, AIAgent):
        return reverse("inventory:agent_detail", args=[subject.pk])
    return reverse("inventory:application_detail", args=[subject.pk])


def matrix_context() -> dict:
    """The M5 matrix, drawn with impact rising upwards, plus the client-side copy."""
    return {
        "matrix_rows": [
            (impact, [irgf.TIER_MATRIX[impact][deficit] for deficit in irgf.SCORES])
            for impact in reversed(irgf.SCORES)
        ],
        "matrix_json": json.dumps(
            {str(i): {str(c): t for c, t in row.items()} for i, row in irgf.TIER_MATRIX.items()}
        ),
        "tier_summaries": [(tier, irgf.TIER_SUMMARY[tier]) for tier in irgf.TIERS],
        "summaries_json": json.dumps({str(tier): text for tier, text in irgf.TIER_SUMMARY.items()}),
        "floor_tier": irgf.FLOOR_TIER,
    }


class ClassificationRegisterView(OrganizationScopedMixin, ListView):
    model = RiskClassification
    template_name = "classification/register.html"
    context_object_name = "classifications"
    paginate_by = 25
    ordering = ["-scored_at"]
    extra_context = {
        "page_title": "Risk Classification",
        "page_subtitle": "IRGF stage S2: every AI system scored on D1–D5, tiered, and countersigned",
    }
    search_fields = ["agent__name", "application__name", "rationale"]
    sort_fields = {
        "subject": ["agent__name", "application__name"],
        "tier": "tier",
        "impact": "impact",
        "control": "control_deficit",
        "status": "status",
        "scored": "scored_at",
        "review": "review_due",
    }
    filter_fields = {"tier": "tier", "status": "status"}

    def get_base_queryset(self):
        return super().get_base_queryset().select_related(
            "agent", "application", "scored_by", "countersigned_by"
        )

    def get_queryset(self):
        queryset = super().get_queryset()
        # Current records by default; superseded ones are history, shown on
        # request so the register reads as "where we stand".
        if not self.request.GET.get("status"):
            queryset = queryset.filter(status__in=CURRENT_STATUSES)
        kind = self.request.GET.get("kind")
        if kind == "agent":
            queryset = queryset.filter(agent__isnull=False)
        elif kind == "application":
            queryset = queryset.filter(application__isnull=False)
        if self.request.GET.get("due") == "1":
            queryset = queryset.filter(
                Q(rescore_required=True) | Q(review_due__lte=timezone.now().date())
            )
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization
        current = RiskClassification.objects.for_organization(organization).filter(
            status__in=CURRENT_STATUSES
        )
        by_tier = dict(current.values_list("tier").annotate(total=Count("id")).order_by())
        agents = AIAgent.objects.for_organization(organization).filter(risk_tier__isnull=True)
        applications = AIApplication.objects.for_organization(organization).filter(
            risk_tier__isnull=True
        )
        context.update(
            {
                "tier_counts": [
                    (tier, by_tier.get(tier, 0), irgf.TIER_SUMMARY[tier]) for tier in irgf.TIERS
                ],
                "awaiting_countersignature": current.filter(
                    status=ClassificationStatus.PROVISIONAL
                ).count(),
                "rescore_due": current.filter(
                    Q(rescore_required=True) | Q(review_due__lte=timezone.now().date())
                ).count(),
                "unclassified_agents": agents.order_by("name")[:8],
                "unclassified_agent_count": agents.count(),
                "unclassified_applications": applications.order_by("name")[:8],
                "unclassified_application_count": applications.count(),
                "statuses": ClassificationStatus.choices,
                "tiers": TIER_CHOICES,
                "can_countersign": can_countersign(self.request),
            }
        )
        return context


class ClassificationDetailView(ScopedDetailMixin, DetailView):
    model = RiskClassification
    template_name = "classification/detail.html"
    context_object_name = "record"
    extra_context = {"page_title": "Risk Classification"}

    def get_queryset(self):
        return super().get_queryset().select_related(
            "agent", "application", "scored_by", "countersigned_by", "superseded_by"
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        record = self.object
        subject = record.subject
        context.update(
            {
                "subject": subject,
                "subject_url": subject_url(subject),
                "history": ClassificationService.history(subject),
                "can_countersign": can_countersign(self.request) and record.is_provisional,
                "independence_problem": (
                    ClassificationService.independence_problem(record, self.request.user)
                    if record.is_provisional
                    else ""
                ),
                "can_score": can_score(self.request, subject),
                "score_url": reverse(
                    "classification:score", args=[subject_kind(subject), subject.pk]
                ),
                **matrix_context(),
            }
        )
        return context


class ScoreView(LoginRequiredMixin, View):
    """Stage S2: score a system on D1–D5. The result is provisional."""

    template_name = "classification/score_form.html"

    def _subject(self, request, kind, pk):
        model = SUBJECT_MODELS.get(kind)
        if model is None:
            raise Http404("Only agents and applications are classified.")
        subject = get_object_or_404(model.objects.for_organization(request.organization), pk=pk)
        if not can_score(request, subject):
            raise PermissionDenied("Your role does not allow scoring this system.")
        return subject

    def get(self, request, kind, pk):
        subject = self._subject(request, kind, pk)
        form = ScoreForm(initial=ClassificationService.suggested_scores(subject))
        return self._render(request, subject, form)

    def post(self, request, kind, pk):
        subject = self._subject(request, kind, pk)
        form = ScoreForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            try:
                record = ClassificationService.score(
                    subject,
                    scores={field: data[field] for field in irgf.ALL_FIELDS},
                    rationale=data["rationale"],
                    actor=request.user,
                    tier_override=data.get("tier_override"),
                    override_rationale=data.get("override_rationale", ""),
                )
            except ClassificationError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request,
                    f"{subject} scored Tier {record.tier}. It stays provisional (Gate G1 "
                    "evidence) until someone independent countersigns it for Gate G2.",
                )
                return redirect("classification:detail", pk=record.pk)
        return self._render(request, subject, form, status=400)

    def _render(self, request, subject, form, status=200):
        return render(
            request,
            self.template_name,
            {
                "page_title": "Score Risk Classification",
                "subject": subject,
                "subject_url": subject_url(subject),
                "form": form,
                "current": ClassificationService.current(subject),
                "dimension_groups": form.dimension_groups(),
                **matrix_context(),
            },
            status=status,
        )


class CountersignView(LoginRequiredMixin, View):
    """IRGF M6: someone outside the delivery line confirms the tier."""

    template_name = "classification/countersign_form.html"

    def _record(self, request, pk):
        record = get_object_or_404(
            RiskClassification.objects.for_organization(request.organization).select_related(
                "agent", "application", "scored_by"
            ),
            pk=pk,
        )
        if not can_countersign(request):
            raise PermissionDenied("Countersigning needs a risk role (risk.manage).")
        return record

    def get(self, request, pk):
        record = self._record(request, pk)
        if not record.is_provisional:
            messages.info(request, "This classification is not awaiting countersignature.")
            return redirect("classification:detail", pk=record.pk)
        return self._render(request, record, CountersignForm(initial=record.scores()))

    def post(self, request, pk):
        record = self._record(request, pk)
        form = CountersignForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            try:
                ClassificationService.countersign(
                    record,
                    actor=request.user,
                    adjusted_scores={field: data[field] for field in irgf.ALL_FIELDS},
                    note=data.get("note", ""),
                )
            except ClassificationError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request,
                    f"Countersigned: {record.subject} is confirmed at Tier {record.tier} "
                    "(Gate G2 evidence).",
                )
                return redirect("classification:detail", pk=record.pk)
        return self._render(request, record, form, status=400)

    def _render(self, request, record, form, status=200):
        return render(
            request,
            self.template_name,
            {
                "page_title": "Countersign Risk Classification",
                "record": record,
                "subject": record.subject,
                "subject_url": subject_url(record.subject),
                "form": form,
                "independence_problem": ClassificationService.independence_problem(
                    record, request.user
                ),
                "dimension_groups": form.dimension_groups(),
                **matrix_context(),
            },
            status=status,
        )
