"""Pushes execution changes to connected monitors.

Broadcasting is best-effort by design: a live view is a convenience, and a
channel layer that is unreachable must never fail the telemetry write that
triggered it.
"""

from __future__ import annotations

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from apps.executions.consumers import group_name

logger = logging.getLogger(__name__)


def _live_statuses():
    from apps.executions.views import LIVE_STATUSES

    return LIVE_STATUSES


def execution_changed(execution) -> bool:
    """Tell watchers about an execution. Returns True when a message was sent.

    A run that is still in flight is upserted into the live table; one that has
    finished is removed from it, since the monitor only shows what is running.
    """
    layer = get_channel_layer()
    if layer is None or execution.organization_id is None:
        return False

    group = group_name(execution.organization_id)

    try:
        if execution.status in _live_statuses():
            # Deliberately no rendered HTML: a group message reaches every
            # watcher of the organization at once, and they do not all have the
            # same right to see the row's prompt text. Each consumer renders it
            # for its own viewer instead.
            message = {"type": "execution.upsert", "id": str(execution.pk)}
        else:
            message = {"type": "execution.remove", "id": str(execution.pk)}

        async_to_sync(layer.group_send)(group, message)
    except Exception:
        logger.exception("Could not broadcast execution %s", execution.pk)
        return False

    return True
