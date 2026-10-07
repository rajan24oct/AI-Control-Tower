"""Publishing agents for A2A and granting contracts, in the web UI.

Publishing a card, or registering an external agent (`a2a.manage`), reaches
nothing on its own. Letting one agent send work to another is governance's
decision (`a2a.approve`), like approving the card and reviewing its skills.
"""

from __future__ import annotations

import uuid

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View

from apps.a2a.forms import A2AAgentCardForm, A2AContractForm
from apps.a2a.models import A2AAgentCard, A2AContract
from apps.a2a.registry import A2ARegistryService
from apps.accounts.roles import Perm
from apps.agents.models import AIAgent
from apps.audit.models import AuditAction
from apps.audit.services import AuditService, snapshot
from apps.core.manage import ManagedFormView, can
from apps.core.responses import action_response


class CardFormView(ManagedFormView):
    model = A2AAgentCard
    form_class = A2AAgentCardForm
    permission = Perm.A2A_MANAGE
    denied_message = "Publishing agents for A2A, or registering external ones, needs a2a.manage."
    noun = "A2A agent"
    create_heading = "Publish an agent for A2A"
    create_lead = (
        "One governed address in front of it: other agents reach it only after approval, skill review and, "
        "on a restricted card, a contract."
    )
    create_submit_label = "Publish"
    list_url_name = "a2a:agents"
    list_label = "Agent Registry"
    detail_url_name = "a2a:card_detail"
    guide_template = "manage/guides/_a2a_card.html"

    def get_form_kwargs(self):
        requested = self.request.GET.get("agent", "")
        if self.object is not None or not requested:
            return {}
        try:
            uuid.UUID(requested)
        except ValueError:
            return {}
        return {
            "initial_agent": AIAgent.objects.for_organization(self.request.organization).filter(pk=requested).first()
        }

    def after_commit(self, obj, form, creating):
        # A mailbox agent's card is the registry's own, so its declared skills
        # are read straight away and wait for review like any other.
        if obj.uses_mailbox:
            result = A2ARegistryService.sync(obj, actor=self.request.user)
            if result.has_changes:
                messages.info(self.request, result.summary)

    def success_message(self, obj, creating, changed):
        if creating:
            first = "" if obj.uses_mailbox else "sync its card, then "
            return (
                f"Published {obj.name} at /api/v1/a2a/{obj.slug}/. Next, on its page: {first}approval and skill "
                "review, then contracts for the agents that may send it work."
            )
        return super().success_message(obj, creating, changed)


class ContractFormView(ManagedFormView):
    model = A2AContract
    form_class = A2AContractForm
    permission = Perm.A2A_APPROVE
    denied_message = "Letting one agent send work to another is a governance decision (a2a.approve)."
    noun = "contract"
    create_submit_label = "Grant contract"
    guide_template = "manage/guides/_a2a_contract.html"

    def get_object(self):
        contract = super().get_object()
        if contract is not None:
            self.card = contract.recipient
        else:
            self.card = get_object_or_404(
                A2AAgentCard.objects.for_organization(self.request.organization), pk=self.kwargs["card_pk"]
            )
        return contract

    def new_instance(self):
        return A2AContract(recipient=self.card, organization=self.card.organization)

    def get_form_kwargs(self):
        return {"card": self.card}

    @property
    def create_heading(self):
        return f"Let an agent send work to {self.card.name}"

    @property
    def list_label(self):
        return self.card.name

    def list_url(self):
        return reverse("a2a:card_detail", args=[self.card.pk])

    def detail_url(self, obj):
        return self.list_url()

    def success_message(self, obj, creating, changed):
        if creating:
            scope = ", ".join(obj.allowed_skills) if obj.allowed_skills else "all of its enabled skills"
            return f"{obj.sender} may now send work to {self.card.name}: {scope}, up to {obj.ceiling} data."
        return super().success_message(obj, creating, changed)


class ContractRevokeView(LoginRequiredMixin, View):
    def post(self, request, pk, *args, **kwargs):
        contract = get_object_or_404(
            A2AContract.objects.for_organization(request.organization).select_related("recipient", "sender"), pk=pk
        )
        redirect_to = request.POST.get("next") or reverse("a2a:card_detail", args=[contract.recipient_id])
        if not can(request, Perm.A2A_APPROVE):
            return action_response(
                request, message="Revoking a contract is a governance decision (a2a.approve).", tone="danger",
                redirect_to=redirect_to, status=403,
            )
        before, label = snapshot(contract), str(contract)
        sender, recipient = contract.sender, contract.recipient
        contract.delete()
        AuditService.log(
            action=AuditAction.DELETE, resource_type=A2AContract._meta.label, resource_id=pk,
            resource_label=label, organization=request.organization, actor=request.user, old_value=before,
        )
        return action_response(
            request, message=f"Revoked {sender}'s contract to send work to {recipient}.", tone="warning",
            redirect_to=redirect_to,
        )
