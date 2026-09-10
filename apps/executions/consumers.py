"""WebSocket consumer for the live execution monitor (section 25).

Sends rendered HTML rather than JSON the client has to template. The rest of
the platform renders with Django templates; duplicating the row markup in
JavaScript would mean two places to change and two ways to be wrong.
"""

from __future__ import annotations

import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)

GROUP_PREFIX = "executions"
SNAPSHOT_LIMIT = 50


def group_name(organization_id) -> str:
    return f"{GROUP_PREFIX}.{organization_id}"


class ExecutionMonitorConsumer(AsyncJsonWebsocketConsumer):
    """Streams live execution rows for one organization.

    Membership is verified before the socket joins a group. Origin checking and
    session auth happen in the ASGI stack, but neither says *which* tenant this
    user may watch -- that has to be answered here.
    """

    async def connect(self):
        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            await self.close(code=4401)
            return

        requested = self.scope["url_route"]["kwargs"]["organization_id"]
        self.organization = await self.resolve_organization(user, requested)
        if self.organization is None:
            # Never distinguish "no such organization" from "not yours".
            await self.close(code=4403)
            return

        self.group = group_name(self.organization.pk)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

        await self.send_json(
            {"type": "snapshot", "rows": await self.render_snapshot(self.organization)}
        )

    async def disconnect(self, code):
        group = getattr(self, "group", None)
        if group:
            await self.channel_layer.group_discard(group, self.channel_name)

    async def receive_json(self, content, **kwargs):
        """The monitor is a read-only stream; a refresh nudge is all it takes."""
        if content.get("action") == "refresh":
            await self.send_json(
                {"type": "snapshot", "rows": await self.render_snapshot(self.organization)}
            )

    # -- Group message handlers --------------------------------------------

    async def execution_upsert(self, message):
        """Render the changed row for *this* socket's viewer.

        The broadcast carries only an id, so two people watching the same
        organization can receive different markup for the same execution --
        which is the point when one of them may not read prompt text.
        """
        html = await self.render_row(self.organization, message["id"])
        if html is None:
            return
        await self.send_json({"type": "upsert", "id": message["id"], "html": html})

    async def execution_remove(self, message):
        await self.send_json({"type": "remove", "id": message["id"]})

    # -- Database access ----------------------------------------------------

    @database_sync_to_async
    def resolve_organization(self, user, requested_id):
        return user.organizations.filter(pk=requested_id).first()

    @database_sync_to_async
    def render_row(self, organization, execution_id) -> str | None:
        """One row, re-fetched through the org-scoped queryset.

        Going back to the database rather than trusting the broadcast means the
        tenancy check is applied again on the way out.
        """
        from apps.executions.models import Execution
        from apps.executions.prompts import PromptService

        execution = (
            Execution.objects.for_organization(organization)
            .select_related("agent", "application", "model", "provider")
            .filter(pk=execution_id)
            .first()
        )
        if execution is None:
            return None

        PromptService.attach(
            [execution], viewer=self.scope.get("user"), organization=organization
        )
        return render_to_string("executions/partials/_live_row.html", {"execution": execution})

    @database_sync_to_async
    def render_snapshot(self, organization) -> list[str]:
        from apps.executions.views import LIVE_STATUSES
        from apps.executions.models import Execution
        from apps.executions.prompts import PromptService

        executions = (
            Execution.objects.for_organization(organization)
            .filter(status__in=LIVE_STATUSES)
            .select_related("agent", "application", "model", "provider")
            .order_by("-started_at")[:SNAPSHOT_LIMIT]
        )
        # The socket carries rendered HTML, so the permission decision has to
        # happen here rather than in the browser. Whoever opened this socket is
        # the viewer -- a push must not leak text the poll would have withheld.
        executions = PromptService.attach(
            executions, viewer=self.scope.get("user"), organization=organization
        )
        return [
            render_to_string("executions/partials/_live_row.html", {"execution": execution})
            for execution in executions
        ]
