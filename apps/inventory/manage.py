"""Onboarding and editing registry records in the web UI.

Providers, models, agents and applications are created and changed here rather
than in the admin, with the same permissions the REST API applies and the same
audit entries the admin writes. Approving a provider or a model stays a
separate, governance-held action, so whoever registers one cannot also clear it
for production.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View

from apps.accounts.roles import Perm
from apps.agents.models import AIAgent
from apps.applications_registry.models import AIApplication
from apps.audit.models import AuditAction
from apps.audit.services import AuditService, snapshot
from apps.core.manage import ManagedFormView, can
from apps.core.responses import action_response
from apps.inventory.forms import AgentForm, AIModelForm, ApplicationForm, ProviderForm
from apps.models_registry.models import AIModel
from apps.providers.models import AIProvider
from apps.providers.services import ProviderCapabilityService, ProviderHealthService

# -- Forms ------------------------------------------------------------------------


class ProviderFormView(ManagedFormView):
    model = AIProvider
    form_class = ProviderForm
    permission = Perm.PROVIDER_MANAGE
    denied_message = "Registering and editing platform providers needs provider.manage."
    noun = "platform provider"
    create_heading = "Onboard a platform provider"
    create_lead = (
        "Where models run. Register it, check that it answers, and have governance approve it for production."
    )
    create_submit_label = "Register provider"
    list_url_name = "inventory:providers"
    list_label = "Providers"
    detail_url_name = "inventory:provider_detail"
    guide_template = "manage/guides/_provider.html"

    def success_message(self, obj, creating, changed):
        if creating:
            return f"Provider “{obj}” registered. Run a health check next; governance approves it for production."
        return super().success_message(obj, creating, changed)


class ModelFormView(ManagedFormView):
    model = AIModel
    form_class = AIModelForm
    permission = Perm.MODEL_MANAGE
    denied_message = "Registering and editing models needs model.manage."
    noun = "model"
    create_heading = "Onboard a model"
    create_lead = "A model version one of your providers serves, with the prices that drive cost and routing."
    create_submit_label = "Register model"
    list_url_name = "inventory:models"
    list_label = "Models"
    detail_url_name = "inventory:model_detail"
    guide_template = "manage/guides/_model.html"

    def get_form(self, data=None):
        form = super().get_form(data)
        if self.object is None and data is None and self.request.GET.get("provider"):
            form.initial["provider"] = self.request.GET["provider"]
        return form

    def success_message(self, obj, creating, changed):
        if creating:
            return f"Model “{obj.model_name}” registered. It serves once someone with model.approve approves it."
        return super().success_message(obj, creating, changed)


class _SystemFormView(ManagedFormView):
    """Agents and applications: once authorized at Gate G3, edits are change-controlled."""

    def notices(self):
        if self.object is None:
            return []
        from apps.lifecycle.services import AuthorizationService

        authorization = AuthorizationService.baseline(self.object)
        if authorization is None:
            return []
        return [{
            "tone": "warning",
            "icon": "bi-patch-check",
            "text": (
                f"{self.object} holds a Gate G3 deployment authorization for "
                f"{authorization.get_environment_display().lower()}. Saving compares your edit with the "
                "authorized configuration: minor changes are recorded, material ones wait at Gate G4, and "
                "major ones suspend the authorization until G4 decides."
            ),
        }]

    def before_edit(self, form, creating):
        self._known_changes = set() if creating else set(
            self.object.change_records.values_list("pk", flat=True)
        )

    def after_commit(self, obj, form, creating):
        if creating:
            return
        new = obj.change_records.exclude(pk__in=self._known_changes)
        count = new.count()
        if count:
            waiting = new.filter(status="awaiting_g4").count()
            text = f"Recorded {count} change{'s' if count != 1 else ''} against its deployment authorization"
            text += f"; {waiting} wait{'s' if waiting == 1 else ''} at Gate G4." if waiting else "."
            messages.info(self.request, text)


class AgentFormView(_SystemFormView):
    model = AIAgent
    form_class = AgentForm
    permission = Perm.AGENT_MANAGE
    denied_message = "Onboarding and editing agents needs agent.manage."
    noun = "agent"
    create_heading = "Onboard an agent"
    create_lead = (
        "Register the agent your code runs: what it is for, what it may reach, and the Agent Card IRGF "
        "asks for before Gate G3."
    )
    create_submit_label = "Onboard agent"
    list_url_name = "inventory:agents"
    list_label = "Agents"
    detail_url_name = "inventory:agent_detail"
    guide_template = "manage/guides/_agent.html"

    def success_message(self, obj, creating, changed):
        if creating:
            return f"Agent “{obj}” onboarded as {obj.agent_id}. Next: score its risk classification (S2)."
        return super().success_message(obj, creating, changed)


class ApplicationFormView(_SystemFormView):
    model = AIApplication
    form_class = ApplicationForm
    permission = Perm.APPLICATION_MANAGE
    denied_message = "Onboarding and editing applications needs application.manage."
    noun = "application"
    create_heading = "Onboard an application"
    create_lead = "The business-facing system: who owns it, what it is made of, and which of its agents may call which."
    create_submit_label = "Onboard application"
    list_url_name = "inventory:applications"
    list_label = "Applications"
    detail_url_name = "inventory:application_detail"
    guide_template = "manage/guides/_application.html"


# -- Actions ----------------------------------------------------------------------


class _RegistryAction(LoginRequiredMixin, View):
    model = None
    permission = ""
    denied_message = ""
    detail_url_name = ""

    def post(self, request, pk, *args, **kwargs):
        redirect_to = reverse(self.detail_url_name, args=[pk])
        if not can(request, self.permission):
            return action_response(
                request, message=self.denied_message, tone="danger", redirect_to=redirect_to, status=403
            )
        record = get_object_or_404(self.model.objects.for_organization(request.organization), pk=pk)
        return self.act(request, record, redirect_to)

    def act(self, request, record, redirect_to):  # pragma: no cover - abstract
        raise NotImplementedError


class _ProviderAction(_RegistryAction):
    model = AIProvider
    permission = Perm.PROVIDER_MANAGE
    denied_message = "Your role does not allow managing platform providers."
    detail_url_name = "inventory:provider_detail"


class ProviderHealthCheckView(_ProviderAction):
    def act(self, request, provider, redirect_to):
        provider = ProviderHealthService.check(provider)
        state = provider.get_health_status_display().lower()
        message = f"{provider.name} is {state}"
        if provider.last_health_latency_ms:
            message += f" ({provider.last_health_latency_ms} ms)"
        if provider.last_health_message and not provider.is_healthy:
            message += f": {provider.last_health_message}"
        tone = "success" if provider.is_healthy else "warning" if provider.health_status == "degraded" else "danger"
        return action_response(request, message=message + ".", tone=tone, redirect_to=redirect_to)


class ProviderCapabilitiesView(_ProviderAction):
    def act(self, request, provider, redirect_to):
        try:
            capabilities = ProviderCapabilityService.refresh(provider)
        except Exception as exc:  # an adapter that will not load says why
            return action_response(
                request, message=f"Could not read {provider.name}'s capabilities: {exc}", tone="danger",
                redirect_to=redirect_to, status=502,
            )
        count = len(capabilities)
        return action_response(
            request, message=f"{provider.name} advertises {count} capabilit{'y' if count == 1 else 'ies'}.",
            redirect_to=redirect_to,
        )


class ProviderApprovalView(_ProviderAction):
    permission = Perm.MODEL_APPROVE
    denied_message = "Approving a provider for production is a governance decision (model.approve)."

    def act(self, request, provider, redirect_to):
        approve = request.POST.get("decision") == "approve"
        before = snapshot(provider, ["is_approved"])
        provider.is_approved = approve
        provider.save(update_fields=["is_approved", "updated_at"])
        AuditService.log_change(
            action=AuditAction.APPROVE if approve else AuditAction.CONFIG_CHANGE, instance=provider,
            before=before, actor=request.user, metadata={"decision": "approve" if approve else "revoke"},
        )
        message = (
            f"Approved {provider.name} for production use."
            if approve
            else f"Revoked approval for {provider.name}. Policies that require approved providers now refuse it."
        )
        return action_response(
            request, message=message, tone="success" if approve else "warning", redirect_to=redirect_to
        )


class ModelApprovalView(_RegistryAction):
    model = AIModel
    permission = Perm.MODEL_APPROVE
    denied_message = "Approving a model for use is a governance decision (model.approve)."
    detail_url_name = "inventory:model_detail"

    def act(self, request, model, redirect_to):
        approve = request.POST.get("decision") == "approve"
        before = snapshot(model, ["approved_for_use"])
        model.approved_for_use = approve
        model.save(update_fields=["approved_for_use", "updated_at"])
        AuditService.log_change(
            action=AuditAction.MODEL_APPROVAL, instance=model, before=before, actor=request.user,
        )
        message = (
            f"Approved {model.model_name} for use. Routing can now choose it."
            if approve
            else f"Revoked approval for {model.model_name}. Routing will not choose it."
        )
        return action_response(
            request, message=message, tone="success" if approve else "warning", redirect_to=redirect_to
        )
