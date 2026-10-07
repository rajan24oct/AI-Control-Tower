"""Verification on write (link 07), and drift that follows its change or exception."""

from __future__ import annotations

import threading
from contextlib import contextmanager

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.a2a.models import A2ATask
from apps.lifecycle.models import ChangeRecord, GovernanceException
from apps.mcp_gateway.models import MCPToolCall

_state = threading.local()


@contextmanager
def deferred():
    """Leave verification to the sweep while the block runs -- seeding, bulk imports."""
    previous = getattr(_state, "deferred", False)
    _state.deferred = True
    try:
        yield
    finally:
        _state.deferred = previous


@receiver(post_save, sender=MCPToolCall)
def _tool_call_recorded(sender, instance, created, raw=False, **kwargs):
    # After commit, so the gateway's answer never waits on a rollback -- and the
    # sweep verifies anything this misses.
    if created and not raw and not getattr(_state, "deferred", False):
        from apps.controlplane.services import ControlPlane

        pk = instance.pk
        transaction.on_commit(lambda: ControlPlane.verify_tool_call_by_id(pk))


@receiver(post_save, sender=A2ATask)
def _handoff_recorded(sender, instance, created, raw=False, **kwargs):
    # One agent handing work to another is a delegation (IRGF chapter 16).
    if created and not raw and not getattr(_state, "deferred", False):
        from apps.controlplane.services import ControlPlane

        pk = instance.pk
        transaction.on_commit(lambda: ControlPlane.verify_a2a_task_by_id(pk))


def _reconcile(**filters):
    from apps.controlplane.models import DriftRecord
    from apps.controlplane.services import DriftService

    records = DriftRecord.objects.filter(**filters)
    if records.exists():
        DriftService.reconcile(records=records)


@receiver(post_save, sender=ChangeRecord)
def _change_moved(sender, instance, created, raw=False, **kwargs):
    if not created and not raw:
        pk = instance.pk
        transaction.on_commit(lambda: _reconcile(change_record_id=pk))


@receiver(post_save, sender=GovernanceException)
def _exception_moved(sender, instance, created, raw=False, **kwargs):
    if not created and not raw:
        pk = instance.pk
        transaction.on_commit(lambda: _reconcile(exception_id=pk))
