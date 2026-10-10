"""Use-case intake pages: the register, a use case with its G1 checklist and decision."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View
from django.views.generic import DetailView, ListView

from apps.classification import irgf as s2
from apps.core.forms import members, person_label
from apps.core.manage import ManagedFormView
from apps.core.responses import action_response
from apps.core.views import OrganizationScopedMixin, ScopedDetailMixin
from apps.intake.forms import SCORE_FIELDS, UseCaseForm
from apps.intake.models import OPEN_STATUSES, G1Outcome, UseCase, UseCaseStatus
from apps.intake.services import (
    IntakeError,
    IntakeService,
    can_decide,
    can_propose,
    criteria,
    overdue_conditions,
    who_decides,
)


class UseCaseListView(OrganizationScopedMixin, ListView):
    model = UseCase
    template_name = "intake/list.html"
    context_object_name = "use_cases"
    paginate_by = 25
    extra_context = {
        "page_title": "Use-Case Intake",
        "page_subtitle": "IRGF S0–S1 and Gate G1: AI ideas qualified before anything is built",
    }
    search_fields = ["title", "reference", "need", "summary"]
    filter_fields = {"status": "status", "tier": "provisional_tier", "team": "team"}
    sort_fields = {
        "reference": "reference", "title": "title", "tier": "provisional_tier", "status": "status",
        "value": "expected_annual_value", "submitted": "submitted_at",
    }

    def get_base_queryset(self):
        return super().get_base_queryset().select_related("sponsor", "team", "application", "decided_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        base = UseCase.objects.for_organization(self.request.organization)
        counts = dict(base.values_list("status").annotate(n=Count("id")))
        context.update({
            "statuses": UseCaseStatus.choices,
            "counts": counts,
            "awaiting": counts.get(UseCaseStatus.SUBMITTED, 0) + counts.get(UseCaseStatus.ESCALATED, 0),
            "approved": counts.get(UseCaseStatus.APPROVED, 0) + counts.get(UseCaseStatus.CONDITIONAL, 0),
            "pipeline_value": base.exclude(status__in=[UseCaseStatus.REJECTED, UseCaseStatus.WITHDRAWN])
            .aggregate(total=Sum("expected_annual_value"))["total"] or 0,
            "tiers": {t: base.filter(provisional_tier=t).exclude(status=UseCaseStatus.WITHDRAWN).count() for t in s2.TIERS},
            "can_propose": can_propose(self.request.user, self.request.organization),
        })
        return context


class UseCaseDetailView(ScopedDetailMixin, DetailView):
    model = UseCase
    template_name = "intake/detail.html"
    context_object_name = "use_case"

    def get_queryset(self):
        return super().get_queryset().select_related("sponsor", "team", "application", "decided_by", "created_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        use_case = self.object
        user = self.request.user
        allowed, reason = can_decide(use_case, user)
        checklist = criteria(use_case)
        result = use_case.tier_result
        context.update({
            "page_title": use_case.title,
            "page_subtitle": f"{use_case.reference} · {use_case.get_stage_display()} · {use_case.get_status_display()}",
            "criteria": checklist,
            "ready": all(c.met for c in checklist),
            "result": result,
            "tier_summary": s2.TIER_SUMMARY.get(use_case.provisional_tier, ""),
            "scores": [
                {"code": d.code, "name": d.name, "score": getattr(use_case, field), "anchor": d.anchor(getattr(use_case, field) or 0)}
                for field, d in SCORE_FIELDS.items()
            ],
            "who_decides": who_decides(use_case.provisional_tier),
            "can_decide": allowed,
            "decide_reason": reason,
            "outcomes": G1Outcome.choices,
            "can_edit": use_case.is_editable and (use_case.created_by_id == user.pk or use_case.sponsor_id == user.pk
                                                   or can_propose(user, use_case.organization)),
            "can_propose": can_propose(user, use_case.organization),
            "is_open": use_case.status in OPEN_STATUSES,
            "people": [(member.pk, person_label(member)) for member in members(use_case.organization)],
            "overdue": overdue_conditions(use_case),
            "indexed_conditions": list(enumerate(use_case.conditions or [])),
        })
        return context


class UseCaseFormView(ManagedFormView):
    model = UseCase
    form_class = UseCaseForm
    noun = "use case"
    create_heading = "Propose an AI use case"
    create_lead = ("Describe the need and the decision AI would support. Gate G1 decides whether it goes ahead, "
                   "before anyone commits to a build or a vendor.")
    create_submit_label = "Save draft"
    list_url_name = "intake:list"
    list_label = "Use-Case Intake"
    detail_url_name = "intake:detail"
    guide_template = "manage/guides/_use_case.html"

    def check_permission(self, obj) -> None:
        if not can_propose(self.request.user, self.request.organization):
            raise PermissionDenied("Your role does not allow proposing use cases.")
        if obj is not None and not obj.is_editable:
            raise PermissionDenied("Only a draft, or a use case returned to remediate, can be edited.")

    def success_message(self, obj, creating: bool, changed: bool) -> str:
        if creating:
            return f"Saved {obj.reference} as a draft. Submit it for Gate G1 when the checklist is complete."
        return super().success_message(obj, creating, changed)


class _ActionView(LoginRequiredMixin, View):
    def get_use_case(self, pk):
        return get_object_or_404(UseCase.objects.for_organization(self.request.organization), pk=pk)

    def respond(self, use_case, message, tone="success", status=200):
        return action_response(self.request, message=message, tone=tone, status=status,
                               redirect_to=reverse("intake:detail", args=[use_case.pk]))

    def post(self, request, pk, *args, **kwargs):
        use_case = self.get_use_case(pk)
        try:
            message = self.act(use_case, request)
        except IntakeError as exc:
            return self.respond(use_case, str(exc), tone="danger", status=400)
        except PermissionDenied as exc:
            return self.respond(use_case, str(exc), tone="danger", status=403)
        return self.respond(use_case, message)


class SubmitView(_ActionView):
    def act(self, use_case, request):
        if not can_propose(request.user, use_case.organization):
            raise PermissionDenied("Your role does not allow submitting use cases.")
        IntakeService.submit(use_case, request.user)
        if use_case.status == UseCaseStatus.APPROVED:
            return f"{use_case.reference} is Tier 1 and was approved at G1 by rule."
        return f"Submitted {use_case.reference} for Gate G1."


class DecideView(_ActionView):
    def act(self, use_case, request):
        conditions = []
        for text, owner_id, due in zip(request.POST.getlist("condition_text"), request.POST.getlist("condition_owner"),
                                       request.POST.getlist("condition_due")):
            if not text.strip():
                continue
            owner = next((label for pk, label in ((str(m.pk), person_label(m)) for m in members(use_case.organization))
                          if pk == owner_id), "")
            conditions.append({"text": text.strip(), "owner_id": owner_id or None, "owner": owner, "due": due,
                               "done": False})
        outcome = request.POST.get("outcome", "")
        IntakeService.decide(use_case, request.user, outcome, request.POST.get("note", ""), conditions)
        return f"{use_case.reference}: {G1Outcome(outcome).label.lower()} at Gate G1."


class RegisterView(_ActionView):
    def act(self, use_case, request):
        if not can_propose(request.user, use_case.organization):
            raise PermissionDenied("Your role does not allow registering systems.")
        application = IntakeService.register(use_case, request.user)
        return f"Registered as the draft application “{application.name}”. Classify it at S2 next."


class WithdrawView(_ActionView):
    def act(self, use_case, request):
        if request.user.pk not in (use_case.created_by_id, use_case.sponsor_id):
            raise PermissionDenied("Only the person who proposed it or its sponsor can withdraw a use case.")
        IntakeService.withdraw(use_case, request.user)
        return f"Withdrew {use_case.reference}."


class ConditionMetView(_ActionView):
    def post(self, request, pk, index, *args, **kwargs):
        use_case = self.get_use_case(pk)
        try:
            IntakeService.complete_condition(use_case, index, request.user)
        except IntakeError as exc:
            return self.respond(use_case, str(exc), tone="danger", status=403)
        return self.respond(use_case, "Condition marked as met.")
