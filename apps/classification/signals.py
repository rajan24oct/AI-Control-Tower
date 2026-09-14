"""IRGF re-score triggers observed on the registry.

IRGF names the events that make a classification stale. Two of them are visible
in the registry itself, so they are caught here rather than left to memory: an
autonomy increase, and a rise in the sensitivity of the data a system handles.
An application moving to production is caught too, as the change of affected
population it is.
"""

from __future__ import annotations

from django.db.models.signals import post_save, pre_save
from django.dispatch import Signal, receiver

from apps.agents.models import AIAgent
from apps.applications_registry.models import AIApplication
from apps.core.constants import Environment

#: Sent when an agent's or application's cached IRGF tier changes, with
#: `subject`, `before` and `after`. The lifecycle app listens: a tier change
#: touches the agent's deployment authorization.
tier_changed = Signal()

_SENSITIVITY = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}


def _data_rose(before: str, after: str) -> bool:
    return _SENSITIVITY.get(after, 0) > _SENSITIVITY.get(before, 0)


@receiver(pre_save, sender=AIAgent)
def _agent_triggers(sender, instance, **kwargs):
    instance._rescore_triggers = []
    if instance._state.adding:
        return
    before = sender.objects.filter(pk=instance.pk).values(
        "autonomy_level", "data_classification"
    ).first()
    if not before:
        return
    if instance.autonomy_level > before["autonomy_level"]:
        instance._rescore_triggers.append(
            f"Autonomy increased from level {before['autonomy_level']} to {instance.autonomy_level}."
        )
    if _data_rose(before["data_classification"], instance.data_classification):
        instance._rescore_triggers.append(
            f"Data classification rose from {before['data_classification']} "
            f"to {instance.data_classification}."
        )


@receiver(pre_save, sender=AIApplication)
def _application_triggers(sender, instance, **kwargs):
    instance._rescore_triggers = []
    if instance._state.adding:
        return
    before = sender.objects.filter(pk=instance.pk).values(
        "data_classification", "environment"
    ).first()
    if not before:
        return
    if _data_rose(before["data_classification"], instance.data_classification):
        instance._rescore_triggers.append(
            f"Data classification rose from {before['data_classification']} "
            f"to {instance.data_classification}."
        )
    if instance.environment == Environment.PRODUCTION and before["environment"] != Environment.PRODUCTION:
        instance._rescore_triggers.append(
            "Moved to production, which changes the population it affects."
        )


@receiver(post_save, sender=AIAgent)
@receiver(post_save, sender=AIApplication)
def _flag_rescore(sender, instance, **kwargs):
    triggers = getattr(instance, "_rescore_triggers", None)
    if not triggers:
        return
    from apps.classification.services import ClassificationService

    for reason in triggers:
        ClassificationService.flag_rescore(instance, reason)
    instance._rescore_triggers = []
