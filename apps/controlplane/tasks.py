from __future__ import annotations

from celery import shared_task


@shared_task(name="apps.controlplane.tasks.sweep_control_plane")
def sweep_control_plane() -> dict:
    """Verify unverified actions, observe the estate, escalate overdue drift, send digests."""
    from apps.controlplane.services import ControlPlane

    return ControlPlane.sweep()
