"""IRGF lifecycle views: Gate G3 authorizations, Gate G4 changes, M17 exceptions."""

from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView, ListView

from apps.accounts.roles import Perm
from apps.agents.models import AIAgent
from apps.applications_registry.models import AIApplication
from apps.core.constants import Environment
from apps.core.responses import action_response
from apps.core.views import OrganizationScopedMixin, ScopedDetailMixin
from apps.lifecycle import decisions, irgf
from apps.lifecycle.forms import AuthorizationRequestForm, ExceptionRequestForm
from apps.lifecycle.models import (
    BASELINE_STATUSES,
    OPEN_CHANGE_STATUSES,
    AuthorizationCondition,
    AuthorizationStatus,
    ChangeRecord,
    ChangeStatus,
    DeploymentAuthorization,
    ExceptionStatus,
    GovernanceException,
)
from apps.lifecycle.services import (
    FIELD_LABELS,
    AuthorizationService,
    ChangeService,
    ConditionService,
    ExceptionService,
    LifecycleError,
    configuration_rows,
    differences,
    display,
    is_application,
    readiness,
    snapshot,
)

TIER_CHOICES = [(tier, f"Tier {tier}") for tier in (1, 2, 3, 4)]


def _can(request, permission: str) -> bool:
    return request.user.has_org_permission(request.organization, permission)


def can_request(request, subject) -> bool:
    """The delivery side asks for authorization and exceptions: managers or an owner."""
    if is_application(subject):
        return _can(request, Perm.APPLICATION_MANAGE) or request.user.pk in {
            subject.business_owner_id, subject.technical_owner_id,
        }
    return _can(request, Perm.AGENT_MANAGE) or (subject.owner_id and subject.owner_id == request.user.pk)


def _decision_context(request, record) -> dict:
    return {
        "rights": decisions.describe(record),
        "remaining": decisions.remaining(record),
        "next_seats": decisions.next_seats(record),
        "sign_check": decisions.check(record, request.user),
        "reject_check": decisions.check(record, request.user, approving=False),
    }


def _refuse(request, redirect_to, message, status=409):
    return action_response(request, message=message, tone="warning", redirect_to=redirect_to, status=status)


# -- S7 · Gate G3 ----------------------------------------------------------------


class AuthorizationListView(OrganizationScopedMixin, ListView):
    model = DeploymentAuthorization
    template_name = "lifecycle/authorizations.html"
    context_object_name = "authorizations"
    paginate_by = 25
    ordering = ["-requested_at"]
    extra_context = {
        "page_title": "Deployment Authorizations",
        "page_subtitle": "IRGF S7 · Gate G3: this configuration, this tier, this owner",
    }
    search_fields = ["agent__name", "agent__agent_id", "application__name"]
    sort_fields = {
        "agent": ["agent__name", "application__name"],
        "environment": "environment",
        "tier": "tier",
        "status": "status",
        "requested": "requested_at",
        "decided": "decided_at",
    }
    filter_fields = {"status": "status", "tier": "tier", "environment": "environment"}

    def get_base_queryset(self):
        return super().get_base_queryset().select_related("agent", "application", "requested_by")

    def get_queryset(self):
        queryset = super().get_queryset()
        if not self.request.GET.get("status"):
            queryset = queryset.exclude(status=AuthorizationStatus.SUPERSEDED)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization
        records = DeploymentAuthorization.objects.for_organization(organization)
        counts = dict(records.values_list("status").annotate(total=Count("id")).order_by())
        baseline = records.filter(status__in=BASELINE_STATUSES)
        applications = (
            AIApplication.objects.for_organization(organization)
            .filter(environment=Environment.PRODUCTION)
            .exclude(pk__in=baseline.exclude(application=None).values_list("application_id", flat=True))
            .order_by("name")
        )
        agents = (
            AIAgent.objects.for_organization(organization)
            .filter(environment=Environment.PRODUCTION)
            .exclude(pk__in=baseline.exclude(agent=None).values_list("agent_id", flat=True))
            .order_by("name")
        )
        unauthorized = [*applications, *agents]
        context.update(
            {
                "counts": {
                    "in_force": counts.get(AuthorizationStatus.AUTHORIZED, 0),
                    "pending": counts.get(AuthorizationStatus.PENDING, 0),
                    "suspended": counts.get(AuthorizationStatus.SUSPENDED, 0),
                },
                "unauthorized": unauthorized[:12],
                "unauthorized_count": len(unauthorized),
                "statuses": AuthorizationStatus.choices,
                "environments": Environment.choices,
                "tiers": TIER_CHOICES,
                "rights_table": [(tier, irgf.describe_rights(irgf.AUTHORIZATION, tier)) for tier in (1, 2, 3, 4)],
            }
        )
        return context


class AuthorizationDetailView(ScopedDetailMixin, DetailView):
    model = DeploymentAuthorization
    template_name = "lifecycle/authorization_detail.html"
    context_object_name = "authorization"
    extra_context = {"page_title": "Deployment Authorization"}

    def get_queryset(self):
        return super().get_queryset().select_related(
            "agent", "agent__owner", "application", "classification", "requested_by", "accountable_owner",
            "superseded_by",
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        authorization = self.object
        user = self.request.user
        live = authorization.status in (AuthorizationStatus.PENDING, *BASELINE_STATUSES)
        signers = {entry["user"] for entry in authorization.signatures or []}
        context.update(
            {
                "configuration": configuration_rows(authorization.configuration),
                "drift": (
                    [FIELD_LABELS[field] for field in differences(authorization.subject, authorization.configuration)]
                    if live else []
                ),
                "conditions": authorization.conditions.select_related("owner", "met_by"),
                "changes": authorization.changes.order_by("-detected_at"),
                "can_manage_conditions": _can(self.request, Perm.GATE_DECIDE) or str(user.pk) in signers,
                "can_revoke": live and (
                    _can(self.request, Perm.GATE_DECIDE)
                    or (authorization.tier <= 2 and authorization.subject_owner_id == user.pk)
                ),
                **_decision_context(self.request, authorization),
            }
        )
        return context


class AuthorizationRequestView(LoginRequiredMixin, View):
    """Stage S7: ask for Gate G3 with the entry criteria in view, for an agent or an application."""

    template_name = "lifecycle/authorization_request.html"
    #: Criteria met by what the requester writes in this form.
    WRITTEN = {"oversight", "monitoring"}
    #: The kind of system the URL names.
    model = AIAgent

    def _subject(self, request, pk):
        subject = get_object_or_404(self.model.objects.for_organization(request.organization), pk=pk)
        if not can_request(request, subject):
            raise PermissionDenied("Requesting authorization needs the manage permission for it, or owning it.")
        return subject

    def get(self, request, pk):
        agent = self._subject(request, pk)
        form = AuthorizationRequestForm(organization=request.organization)
        return self._render(request, agent, form, readiness(agent))

    def post(self, request, pk):
        agent = self._subject(request, pk)
        form = AuthorizationRequestForm(request.POST, organization=request.organization)
        checks = readiness(agent)
        if form.is_valid():
            data = form.cleaned_data
            written = {
                "assurance_summary": data["assurance_summary"],
                "oversight_point": data["oversight_point"],
                "monitoring_commitments": data["monitoring_commitments"],
            }
            checks = readiness(agent, **written)
            try:
                authorization = AuthorizationService.request(
                    agent, actor=request.user, conditions=form.conditions(), **written
                )
            except LifecycleError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request,
                    f"Gate G3 request recorded. Decision needed from {decisions.describe(authorization)}.",
                )
                return redirect("lifecycle:authorization_detail", pk=authorization.pk)
        return self._render(request, agent, form, checks, status=400)

    def _render(self, request, agent, form, checks, status=200):
        from apps.classification.services import ClassificationService

        classification = ClassificationService.current(agent)
        tier = classification.tier if classification else None
        written = self.WRITTEN | ({"assurance"} if tier == 2 else set())
        return render(
            request,
            self.template_name,
            {
                "page_title": "Request Deployment Authorization",
                "subject": agent,
                "kind": "application" if is_application(agent) else "agent",
                "form": form,
                "checks": checks,
                "written_keys": written,
                "hard_blocking": [c for c in checks if c.blocking and c.key not in written],
                "tier": tier,
                "rights": irgf.describe_rights(irgf.AUTHORIZATION, tier or 1),
                "pending": AuthorizationService.pending(agent),
                "configuration": configuration_rows(snapshot(agent)),
            },
            status=status,
        )


class ApplicationAuthorizationRequestView(AuthorizationRequestView):
    """Gate G3 for an application: its configuration, its agents and their composite authority."""

    model = AIApplication


class AuthorizationDecisionView(LoginRequiredMixin, View):
    def post(self, request, pk):
        authorization = get_object_or_404(
            DeploymentAuthorization.objects.for_organization(request.organization).select_related("agent", "application"),
            pk=pk,
        )
        redirect_to = request.POST.get("next") or reverse("lifecycle:authorization_detail", args=[pk])
        decision = request.POST.get("decision")
        note = request.POST.get("note", "")
        try:
            if decision == "sign":
                AuthorizationService.sign(
                    authorization, actor=request.user, note=note, dissent=request.POST.get("dissent", "")
                )
            elif decision == "reject":
                AuthorizationService.reject(authorization, actor=request.user, note=note)
            else:
                return _refuse(request, redirect_to, "Choose to sign or to reject.", status=400)
        except LifecycleError as exc:
            return _refuse(request, redirect_to, str(exc))

        authorization.refresh_from_db()
        if authorization.is_in_force:
            message = f"{authorization.subject.name} is authorized at Gate G3."
        elif authorization.is_pending:
            message = f"Signed. Still needed: {' or '.join(s.label for s in decisions.next_seats(authorization))}."
        else:
            message = "The request is rejected."
        return action_response(request, message=message, redirect_to=redirect_to)


class AuthorizationRevokeView(LoginRequiredMixin, View):
    def post(self, request, pk):
        authorization = get_object_or_404(
            DeploymentAuthorization.objects.for_organization(request.organization), pk=pk
        )
        redirect_to = request.POST.get("next") or reverse("lifecycle:authorization_detail", args=[pk])
        try:
            AuthorizationService.revoke(authorization, actor=request.user, reason=request.POST.get("reason", ""))
        except LifecycleError as exc:
            return _refuse(request, redirect_to, str(exc))
        return action_response(request, message="Authorization revoked.", tone="warning", redirect_to=redirect_to)


class ConditionMetView(LoginRequiredMixin, View):
    def post(self, request, pk):
        condition = get_object_or_404(
            AuthorizationCondition.objects.for_organization(request.organization).select_related("authorization"),
            pk=pk,
        )
        redirect_to = request.POST.get("next") or reverse(
            "lifecycle:authorization_detail", args=[condition.authorization_id]
        )
        try:
            ConditionService.mark_met(condition, actor=request.user, note=request.POST.get("note", ""))
        except LifecycleError as exc:
            return _refuse(request, redirect_to, str(exc))
        return action_response(request, message="Condition marked as met.", redirect_to=redirect_to)


# -- S9 · Gate G4 ----------------------------------------------------------------


class ChangeListView(OrganizationScopedMixin, ListView):
    model = ChangeRecord
    template_name = "lifecycle/changes.html"
    context_object_name = "changes"
    paginate_by = 25
    ordering = ["-detected_at"]
    extra_context = {
        "page_title": "AI Changes",
        "page_subtitle": "IRGF S9 · Gate G4: every change to an authorized agent, classified",
    }
    search_fields = ["agent__name", "application__name", "description"]
    sort_fields = {
        "agent": ["agent__name", "application__name"],
        "change": "change_type",
        "class": ("classification", list(irgf.CLASSES)),
        "status": "status",
        "detected": "detected_at",
    }
    filter_fields = {"classification": "classification"}

    def get_base_queryset(self):
        return super().get_base_queryset().select_related("agent", "application", "authorization")

    def get_queryset(self):
        queryset = super().get_queryset()
        status = self.request.GET.get("status", "")
        if status == "all":
            return queryset
        if status:
            return queryset.filter(status=status)
        return queryset.filter(status__in=OPEN_CHANGE_STATUSES)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        records = ChangeRecord.objects.for_organization(self.request.organization)
        context.update(
            {
                "awaiting": records.filter(status=ChangeStatus.AWAITING_G4).count(),
                "major": records.filter(status=ChangeStatus.AWAITING_G4, classification=irgf.MAJOR).count(),
                "confirming": records.filter(status=ChangeStatus.AWAITING_CONFIRMATION).count(),
                "rejected": records.filter(status=ChangeStatus.REJECTED).count(),
                "statuses": ChangeStatus.choices,
                "classes": [(value, value.title()) for value in irgf.CLASSES],
            }
        )
        return context


class ChangeDetailView(ScopedDetailMixin, DetailView):
    model = ChangeRecord
    template_name = "lifecycle/change_detail.html"
    context_object_name = "change"
    extra_context = {"page_title": "AI Change Record"}

    def get_queryset(self):
        return super().get_queryset().select_related(
            "agent", "application", "authorization", "changed_by", "reclassified_by", "confirmed_by"
        )

    def get_context_data(self, **kwargs):
        from apps.classification.services import ClassificationService

        context = super().get_context_data(**kwargs)
        change = self.object
        classification = ClassificationService.current(change.subject)
        context.update(
            {
                "before": display(change.field, change.before),
                "after": display(change.field, change.after),
                "field_label": FIELD_LABELS.get(change.field, change.field),
                "classification": classification,
                "rescore_done": bool(
                    classification and classification.is_confirmed
                    and classification.scored_at >= change.detected_at
                ),
                "can_reclassify": change.status == ChangeStatus.AWAITING_G4 and (
                    can_request(self.request, change.subject) or _can(self.request, Perm.RISK_MANAGE)
                ),
                "reclassifiable": change.change_type in irgf.RECLASSIFIABLE,
                "can_confirm": change.status == ChangeStatus.AWAITING_CONFIRMATION and (
                    _can(self.request, Perm.AGENT_MANAGE) or _can(self.request, Perm.RISK_MANAGE)
                ),
                "classes": irgf.CLASSES,
                **_decision_context(self.request, change),
            }
        )
        return context


class _ChangeAction(LoginRequiredMixin, View):
    def post(self, request, pk):
        change = get_object_or_404(
            ChangeRecord.objects.for_organization(request.organization).select_related(
                "agent", "application", "authorization"
            ),
            pk=pk,
        )
        redirect_to = request.POST.get("next") or reverse("lifecycle:change_detail", args=[pk])
        try:
            message = self.act(request, change)
        except PermissionDenied as exc:
            return _refuse(request, redirect_to, str(exc), status=403)
        except LifecycleError as exc:
            return _refuse(request, redirect_to, str(exc))
        return action_response(request, message=message, redirect_to=redirect_to)

    def act(self, request, change):  # pragma: no cover - abstract
        raise NotImplementedError


class ChangeDecisionView(_ChangeAction):
    def act(self, request, change):
        decision = request.POST.get("decision")
        if decision not in {"approve", "reject"}:
            raise LifecycleError("Choose to approve or to reject.")
        ChangeService.decide(
            change, actor=request.user, approve=decision == "approve",
            note=request.POST.get("note", ""),
            delta_assurance_scope=request.POST.get("delta_assurance_scope", ""),
        )
        change.refresh_from_db()
        if change.status == ChangeStatus.APPROVED:
            return "Approved at Gate G4. The authorized configuration now includes this change."
        if change.status == ChangeStatus.REJECTED:
            return "Rejected at Gate G4. The authorization stays suspended until the change is reverted."
        return "Signed. Tier 4 needs a second signature."


class ChangeReclassifyView(_ChangeAction):
    def act(self, request, change):
        if not (can_request(request, change.subject) or _can(request, Perm.RISK_MANAGE)):
            raise PermissionDenied("Reclassifying needs agent.manage, the agent's ownership, or risk.manage.")
        ChangeService.reclassify(
            change, actor=request.user, classification=request.POST.get("classification", ""),
            reason=request.POST.get("reason", ""),
        )
        change.refresh_from_db()
        return f"Reclassified as {change.classification}: {change.get_status_display().lower()}."


class ChangeConfirmView(_ChangeAction):
    def act(self, request, change):
        if not (_can(request, Perm.AGENT_MANAGE) or _can(request, Perm.RISK_MANAGE)):
            raise PermissionDenied("Confirming needs agent.manage or risk.manage.")
        ChangeService.confirm(change, actor=request.user)
        return "Confirmed by a second person. The change is accepted."


# -- M17 · Exceptions ----------------------------------------------------------------


class ExceptionListView(OrganizationScopedMixin, ListView):
    model = GovernanceException
    template_name = "lifecycle/exceptions.html"
    context_object_name = "exceptions"
    paginate_by = 25
    ordering = ["-requested_at"]
    extra_context = {
        "page_title": "Governance Exceptions",
        "page_subtitle": "IRGF M17: a policy set aside for one agent, for at most 90 days",
    }
    search_fields = ["agent__name", "policy__name", "justification"]
    sort_fields = {
        "agent": "agent__name",
        "policy": "policy__name",
        "tier": "tier",
        "status": "status",
        "expires": "expires_at",
        "requested": "requested_at",
    }
    filter_fields = {"status": "status"}

    def get_base_queryset(self):
        return super().get_base_queryset().select_related("agent", "policy", "requested_by")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        records = GovernanceException.objects.for_organization(self.request.organization)
        now = timezone.now()
        context.update(
            {
                "active": records.filter(status=ExceptionStatus.ACTIVE, expires_at__gt=now).count(),
                "pending": records.filter(status=ExceptionStatus.PENDING).count(),
                "expiring": records.filter(
                    status=ExceptionStatus.ACTIVE, expires_at__gt=now,
                    expires_at__lte=now + timedelta(days=irgf.EXCEPTION_WARNING_DAYS),
                ).count(),
                "statuses": ExceptionStatus.choices,
                "max_days": irgf.MAX_EXCEPTION_DAYS,
            }
        )
        return context


class ExceptionRequestView(LoginRequiredMixin, View):
    template_name = "lifecycle/exception_request.html"

    def _initial(self, request):
        initial = {}
        renewal = None
        if request.GET.get("renew"):
            renewal = get_object_or_404(
                GovernanceException.objects.for_organization(request.organization), pk=request.GET["renew"]
            )
            initial = {
                "agent": renewal.agent_id, "policy": renewal.policy_id,
                "justification": renewal.justification,
                "compensating_control": renewal.compensating_control,
                "duration_days": renewal.duration_days,
            }
        elif request.GET.get("agent"):
            initial["agent"] = request.GET["agent"]
        return initial, renewal

    def get(self, request):
        initial, renewal = self._initial(request)
        form = ExceptionRequestForm(initial=initial, organization=request.organization)
        return self._render(request, form, renewal)

    def post(self, request):
        _, renewal = self._initial(request)
        form = ExceptionRequestForm(request.POST, organization=request.organization)
        if form.is_valid():
            data = form.cleaned_data
            if not can_request(request, data["agent"]):
                raise PermissionDenied("Requesting an exception needs agent.manage, or owning the agent.")
            try:
                exception = ExceptionService.request(
                    data["agent"], data["policy"], actor=request.user,
                    justification=data["justification"],
                    compensating_control=data["compensating_control"],
                    duration_days=data["duration_days"], renewal_of=renewal,
                )
            except LifecycleError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request, f"Exception requested. Decision needed from {decisions.describe(exception)}."
                )
                return redirect("lifecycle:exception_detail", pk=exception.pk)
        return self._render(request, form, renewal, status=400)

    def _render(self, request, form, renewal, status=200):
        return render(
            request, self.template_name,
            {
                "page_title": "Request an Exception",
                "form": form,
                "renewal": renewal,
                "max_days": irgf.MAX_EXCEPTION_DAYS,
                "rights_table": [(tier, irgf.describe_rights(irgf.EXCEPTION, tier)) for tier in (1, 2, 3, 4)],
            },
            status=status,
        )


class ExceptionDetailView(ScopedDetailMixin, DetailView):
    model = GovernanceException
    template_name = "lifecycle/exception_detail.html"
    context_object_name = "exception"
    extra_context = {"page_title": "Governance Exception"}

    def get_queryset(self):
        return super().get_queryset().select_related("agent", "policy", "requested_by", "renewal_of")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        exception = self.object
        context.update(
            {
                "can_revoke": exception.status in (ExceptionStatus.ACTIVE, ExceptionStatus.PENDING) and (
                    _can(self.request, Perm.GATE_DECIDE) or _can(self.request, Perm.RISK_MANAGE)
                ),
                "can_renew": exception.can_renew and can_request(self.request, exception.agent),
                "renewals": exception.renewals.order_by("-requested_at"),
                **_decision_context(self.request, exception),
            }
        )
        return context


class ExceptionDecisionView(LoginRequiredMixin, View):
    def post(self, request, pk):
        exception = get_object_or_404(
            GovernanceException.objects.for_organization(request.organization).select_related("agent", "policy"),
            pk=pk,
        )
        redirect_to = request.POST.get("next") or reverse("lifecycle:exception_detail", args=[pk])
        decision = request.POST.get("decision")
        note = request.POST.get("note", "")
        try:
            if decision == "sign":
                ExceptionService.sign(exception, actor=request.user, note=note)
            elif decision == "reject":
                ExceptionService.reject(exception, actor=request.user, note=note)
            else:
                return _refuse(request, redirect_to, "Choose to sign or to reject.", status=400)
        except LifecycleError as exc:
            return _refuse(request, redirect_to, str(exc))
        exception.refresh_from_db()
        if exception.status == ExceptionStatus.ACTIVE:
            message = f"Exception active until {exception.expires_at:%d %b %Y}."
        elif exception.status == ExceptionStatus.PENDING:
            message = "Signed. Tier 4 needs a second signature."
        else:
            message = "Exception refused."
        return action_response(request, message=message, redirect_to=redirect_to)


class ExceptionRevokeView(LoginRequiredMixin, View):
    def post(self, request, pk):
        exception = get_object_or_404(
            GovernanceException.objects.for_organization(request.organization), pk=pk
        )
        redirect_to = request.POST.get("next") or reverse("lifecycle:exception_detail", args=[pk])
        try:
            ExceptionService.revoke(exception, actor=request.user, reason=request.POST.get("reason", ""))
        except LifecycleError as exc:
            return _refuse(request, redirect_to, str(exc))
        return action_response(request, message="Exception revoked.", tone="warning", redirect_to=redirect_to)
