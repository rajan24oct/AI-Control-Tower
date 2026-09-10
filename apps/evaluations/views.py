"""Evaluation Centre views."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView

from apps.accounts.roles import Perm
from apps.core.responses import action_response
from apps.core.views import OrganizationScopedMixin, ScopedDetailMixin
from apps.evaluations.models import EvaluationRun, EvaluationSuite, RunStatus, TargetKind
from apps.evaluations.services import EvaluationError, EvaluationService, EvaluationStats
from apps.executions.prompts import PromptService


class EvaluationHomeView(OrganizationScopedMixin, ListView):
    model = EvaluationSuite
    template_name = "evaluations/home.html"
    context_object_name = "suites"
    paginate_by = 25
    extra_context = {
        "page_title": "Evaluation Centre",
        "page_subtitle": "Scored evaluation runs and red-team testing for models and agents",
    }
    search_fields = ["name", "description"]
    filter_fields = {"target_kind": "target_kind", "status": "status"}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["stats"] = EvaluationStats(self.request.organization).build()
        context["recent_runs"] = (
            EvaluationRun.objects.for_organization(self.request.organization)
            .select_related("suite", "model", "agent")
            .order_by("-created_at")[:10]
        )
        return context


class SuiteDetailView(ScopedDetailMixin, DetailView):
    model = EvaluationSuite
    template_name = "evaluations/suite_detail.html"
    context_object_name = "suite"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        suite = self.object
        organization = self.request.organization

        context["page_title"] = suite.name
        context["page_subtitle"] = f"{suite.case_count} cases, {suite.pass_threshold}% to pass"
        context["cases"] = suite.cases.all()
        context["runs"] = (
            suite.runs.select_related("model", "agent").order_by("-created_at")[:10]
        )
        context["targets"] = self._targets(suite, organization)
        context["can_run"] = self.request.user.has_org_permission(
            organization, Perm.MODEL_APPROVE
        )
        return context

    @staticmethod
    def _targets(suite, organization):
        from apps.agents.models import AIAgent
        from apps.models_registry.models import AIModel

        if suite.target_kind == TargetKind.MODEL:
            return AIModel.objects.for_organization(organization).select_related("provider")
        return AIAgent.objects.for_organization(organization)


class StartRunView(LoginRequiredMixin, View):
    """Queue a run against a chosen target.

    Gated on `model.approve` rather than a read permission: a run spends real
    money against real providers, and its output is evidence in an approval
    decision. Both of those want the same people behind them.
    """

    def post(self, request, pk):
        organization = request.organization
        suite = get_object_or_404(
            EvaluationSuite.objects.for_user(request.user), pk=pk
        )

        if not request.user.has_org_permission(organization, Perm.MODEL_APPROVE):
            return action_response(
                request,
                message="Your role does not allow starting evaluation runs.",
                tone="danger",
                redirect_to=reverse("evaluations:suite", args=[suite.pk]),
                status=403,
            )

        model, agent = self._resolve_target(request, organization, suite)
        try:
            run = EvaluationService.create_run(
                suite, model=model, agent=agent, triggered_by=request.user
            )
        except EvaluationError as exc:
            return action_response(
                request,
                message=str(exc),
                tone="danger",
                redirect_to=reverse("evaluations:suite", args=[suite.pk]),
                status=400,
            )

        self._dispatch(run)
        return action_response(
            request,
            message=f"Evaluating {run.target_label} against {suite.name}.",
            redirect_to=reverse("evaluations:run", args=[run.pk]),
            payload={"run_id": str(run.pk), "run_url": reverse("evaluations:run", args=[run.pk])},
        )

    @staticmethod
    def _resolve_target(request, organization, suite):
        from apps.agents.models import AIAgent
        from apps.models_registry.models import AIModel

        target_id = request.POST.get("target")
        if suite.target_kind == TargetKind.MODEL:
            return AIModel.objects.for_organization(organization).filter(pk=target_id).first(), None
        return None, AIAgent.objects.for_organization(organization).filter(pk=target_id).first()

    @staticmethod
    def _dispatch(run) -> None:
        """Hand the run to a worker, falling back to running it inline.

        A deployment without Celery should still be able to evaluate; it will
        simply block until the suite finishes rather than silently never
        running it.
        """
        from apps.evaluations.tasks import execute_run

        try:
            execute_run.delay(str(run.pk))
        except Exception:
            EvaluationService.execute(run)


class RunListView(OrganizationScopedMixin, ListView):
    model = EvaluationRun
    template_name = "evaluations/run_list.html"
    context_object_name = "runs"
    paginate_by = 50
    ordering = ["-created_at"]
    extra_context = {
        "page_title": "Evaluation Runs",
        "page_subtitle": "Every run, and what it showed",
    }
    search_fields = ["suite__name", "model__model_name", "agent__name"]
    filter_fields = {"status": "status", "suite": "suite_id"}

    def get_queryset(self):
        return super().get_queryset().select_related("suite", "model", "agent", "triggered_by")


class RunDetailView(ScopedDetailMixin, DetailView):
    model = EvaluationRun
    template_name = "evaluations/run_detail.html"
    context_object_name = "run"

    def get_queryset(self):
        return super().get_queryset().select_related("suite", "model", "agent", "triggered_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        run = self.object

        context["page_title"] = f"{run.suite.name} vs {run.target_label}"
        context["page_subtitle"] = run.get_status_display()
        context["results"] = run.results.select_related("case", "execution").all()
        context["breakdown"] = EvaluationService.category_breakdown(run)
        # A case's output is model output like any other, so it is subject to
        # the same permission that governs prompts on the monitor.
        context["may_see_output"] = PromptService.may_view(
            self.request.user, self.request.organization
        )
        return context


class RunProgressView(ScopedDetailMixin, DetailView):
    """Small JSON endpoint the run page polls while a suite is in flight."""

    model = EvaluationRun

    def render_to_response(self, context, **kwargs):
        run = self.object
        return JsonResponse(
            {
                "status": run.status,
                "finished": run.is_finished,
                "progress": run.progress,
                "completed": run.completed_cases,
                "total": run.total_cases,
                "passed": run.passed_cases,
                "score": str(run.score),
                "verdict": run.verdict,
            }
        )
