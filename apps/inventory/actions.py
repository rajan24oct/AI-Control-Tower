"""Kill-switch actions on the agent page (IRGF agent chain link 06)."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View

from apps.accounts.roles import Perm
from apps.agents.models import AIAgent
from apps.agents.services import KillSwitch, KillSwitchError
from apps.core.responses import action_response


class _KillSwitchAction(LoginRequiredMixin, View):
    def post(self, request, pk, *args, **kwargs):
        redirect_to = request.POST.get("next") or reverse("inventory:agent_detail", args=[pk])
        if not request.user.has_org_permission(request.organization, Perm.AGENT_SUSPEND):
            return action_response(
                request, message="Your role does not allow using an agent's kill-switch.",
                tone="danger", redirect_to=redirect_to, status=403,
            )
        agent = get_object_or_404(AIAgent.objects.for_organization(request.organization), pk=pk)
        try:
            message, tone = self.act(request, agent)
        except KillSwitchError as exc:
            return action_response(
                request, message=str(exc), tone="warning", redirect_to=redirect_to, status=400
            )
        return action_response(request, message=message, tone=tone, redirect_to=redirect_to)

    def act(self, request, agent):  # pragma: no cover - abstract
        raise NotImplementedError


class AgentSuspendView(_KillSwitchAction):
    def act(self, request, agent):
        KillSwitch.suspend(agent, actor=request.user, reason=request.POST.get("reason", ""))
        return f"{agent.name} is suspended. Both gateways now refuse it.", "warning"


class AgentResumeView(_KillSwitchAction):
    def act(self, request, agent):
        KillSwitch.resume(agent, actor=request.user, note=request.POST.get("note", ""))
        return f"{agent.name} is resumed; the gateways serve it again.", "success"
