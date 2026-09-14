"""Change detection (IRGF S9): every way an authorized system's configuration moves.

A system is an agent, or an application and the agents it composes. An
application's authorized configuration includes the composite authority of its
agents (IRGF chapter 16), so an edit to a member agent is checked against every
application it belongs to, as well as against the agent's own authorization.
"""

from __future__ import annotations

from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from apps.agents.models import AIAgent
from apps.api.models import ServiceAccount
from apps.applications_registry.models import AIApplication
from apps.classification.signals import tier_changed
from apps.mcp_gateway.models import MCPAccessGrant


def _detect(subject) -> None:
    if subject is None or subject.pk is None:
        return
    from apps.lifecycle.services import ChangeService

    ChangeService.detect(subject)


def _detect_with_systems(agent) -> None:
    """The agent itself, then each application whose composite it is part of."""
    if agent is None or agent.pk is None:
        return
    _detect(agent)
    for application in agent.applications.all():
        _detect(application)


@receiver(post_save, sender=AIAgent)
def _agent_saved(sender, instance, created, raw=False, **kwargs):
    if not created and not raw:
        _detect_with_systems(instance)


@receiver(post_save, sender=MCPAccessGrant)
@receiver(post_delete, sender=MCPAccessGrant)
def _grant_changed(sender, instance, raw=False, **kwargs):
    if not raw:
        _detect_with_systems(AIAgent.objects.filter(pk=instance.agent_id).first())


@receiver(m2m_changed, sender=ServiceAccount.agents.through)
def _bindings_changed(sender, instance, action, reverse, pk_set, **kwargs):
    if action not in ("post_add", "post_remove"):
        return
    if reverse:
        _detect(instance)
    else:
        for agent in AIAgent.objects.filter(pk__in=pk_set or []):
            _detect(agent)


@receiver(post_save, sender=AIApplication)
def _application_saved(sender, instance, created, raw=False, **kwargs):
    if not created and not raw:
        _detect(instance)


@receiver(m2m_changed, sender=AIApplication.agents.through)
@receiver(m2m_changed, sender=AIApplication.ai_models.through)
@receiver(m2m_changed, sender=AIApplication.providers.through)
def _composition_changed(sender, instance, action, reverse, pk_set, **kwargs):
    """Agents, models or providers joined or left an application."""
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    if isinstance(instance, AIApplication):
        _detect(instance)
    else:
        for application in AIApplication.objects.filter(pk__in=pk_set or []):
            _detect(application)


@receiver(tier_changed)
def _tier_changed(sender, subject, **kwargs):
    if isinstance(subject, (AIAgent, AIApplication)):
        _detect(subject)
