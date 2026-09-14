"""Discovery and review: the registry half of the MCP Gateway.

What a server offers is learned by asking it. What agents may use is decided
here -- by a person, recorded in the audit trail -- and the gateway serves only
what was decided. Discovery can add tools to the registry, but it can never
make one callable on its own.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditAction
from apps.audit.services import AuditService, snapshot
from apps.core.constants import HealthStatus, RiskLevel
from apps.mcp_gateway.models import MCPServer, MCPTool, ToolStatus
from apps.mcp_gateway.services import setting
from integrations.base.mcp import MCPToolDefinition
from integrations.base.types import HealthCheckResult

logger = logging.getLogger(__name__)


def derive_risk(definition: MCPToolDefinition) -> str:
    """A starting risk level from the tool's own hints.

    Hints are the server's claims about itself, so they only ever set where a
    reviewer starts -- they never make a tool callable.
    """
    hints = definition.annotations
    if hints.get("readOnlyHint") is True:
        return RiskLevel.LOW
    if hints.get("destructiveHint") is True:
        return RiskLevel.HIGH
    # A tool that writes is destructive unless it says otherwise (MCP spec).
    if hints.get("readOnlyHint") is False and hints.get("destructiveHint") is not False:
        return RiskLevel.HIGH
    return RiskLevel.MODERATE


@dataclass
class SyncResult:
    server: MCPServer
    ok: bool = True
    error: str = ""
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    unchanged: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.changed or self.removed or self.restored)

    @property
    def summary(self) -> str:
        if not self.ok:
            return f"Could not sync {self.server.name}: {self.error}"
        parts = [
            f"{len(names)} {label}"
            for names, label in (
                (self.added, "new"),
                (self.changed, "changed"),
                (self.restored, "returned"),
                (self.removed, "removed"),
            )
            if names
        ]
        if not parts:
            return f"{self.server.name}: {self.unchanged} tool(s), nothing changed."
        return f"{self.server.name}: {', '.join(parts)}, {self.unchanged} unchanged."

    def as_metadata(self) -> dict:
        return {
            "added": self.added,
            "changed": self.changed,
            "restored": self.restored,
            "removed": self.removed,
            "unchanged": self.unchanged,
        }


class MCPRegistryService:
    @classmethod
    def sync(cls, server: MCPServer, *, actor=None) -> SyncResult:
        """Ask the server what it offers and reconcile the registry with it.

        Never raises for an unreachable server. The failure is recorded on the
        server and returned: "we could not reach it" is a result an operator
        needs to see, not an exception for them to decode.
        """
        if not server.is_gateway_reachable:
            return SyncResult(
                server, ok=False,
                error=f"{server.get_transport_display()} servers cannot be reached by the gateway.",
            )

        started = time.monotonic()
        try:
            adapter = server.get_adapter(timeout=float(setting("CALL_TIMEOUT_SECONDS", 30)))
            info = adapter.initialize()
            definitions = adapter.list_tools()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            logger.warning("MCP discovery failed for %s: %s", server.name, error)
            server.last_sync_error = error
            server.health_status = HealthStatus.UNHEALTHY
            server.last_health_check = timezone.now()
            server.last_health_message = error
            server.save(update_fields=[
                "last_sync_error", "health_status", "last_health_check",
                "last_health_message", "updated_at",
            ])
            return SyncResult(server, ok=False, error=error)

        latency = int((time.monotonic() - started) * 1000)
        now = timezone.now()
        with transaction.atomic():
            result = cls._reconcile(server, definitions, now)
            server.server_info = info.as_dict()
            server.protocol_version = info.protocol_version[:20]
            server.advertised_capabilities = info.capabilities
            server.last_synced_at = now
            server.last_sync_error = ""
            server.health_status = HealthStatus.HEALTHY
            server.last_health_check = now
            server.last_health_latency_ms = latency
            server.last_health_message = ""
            server.save(update_fields=[
                "server_info", "protocol_version", "advertised_capabilities",
                "last_synced_at", "last_sync_error", "health_status", "last_health_check",
                "last_health_latency_ms", "last_health_message", "updated_at",
            ])

        if result.has_changes:
            # A sync that changed nothing is not worth a trail entry; one that
            # changed what agents could be offered is.
            AuditService.log(
                action=AuditAction.CONFIG_CHANGE,
                resource_type=server._meta.label,
                resource_id=server.pk,
                resource_label=f"{server.name} tool catalog",
                organization=server.organization,
                actor=actor,
                new_value=result.as_metadata(),
                metadata={"operation": "mcp_discovery"},
            )
        return result

    @classmethod
    def _reconcile(cls, server: MCPServer, definitions, now) -> SyncResult:
        review = setting("REVIEW_NEW_TOOLS", True)
        first_status = ToolStatus.PENDING_REVIEW if review else ToolStatus.ENABLED
        result = SyncResult(server)
        existing = {tool.name: tool for tool in server.tools.all()}
        seen: set[str] = set()

        for definition in definitions:
            name = definition.name[:128]
            if name in seen:
                continue  # a server listing one name twice: the first wins
            seen.add(name)
            digest = definition.definition_hash()
            tool = existing.get(name)

            if tool is None:
                MCPTool.objects.create(
                    organization_id=server.organization_id,
                    server=server,
                    name=name,
                    definition_hash=digest,
                    status=first_status,
                    risk_level=derive_risk(definition),
                    first_seen_at=now,
                    last_seen_at=now,
                    **cls._fields(definition),
                )
                result.added.append(name)
                continue

            tool.last_seen_at = now
            if tool.status == ToolStatus.REMOVED:
                # Gone and back again. Whatever it was approved as before, it
                # has been away; it is looked at afresh.
                tool.status = first_status
                result.restored.append(name)

            if tool.definition_hash != digest:
                for attribute, value in cls._fields(definition).items():
                    setattr(tool, attribute, value)
                tool.definition_hash = digest
                if review and tool.status in {ToolStatus.ENABLED, ToolStatus.DISABLED}:
                    # The rug-pull case: a tool changing underneath the
                    # decision made about it. It stops being callable until
                    # someone has looked again.
                    tool.changed_since_review = True
                    if tool.status == ToolStatus.ENABLED:
                        tool.status = ToolStatus.PENDING_REVIEW
                result.changed.append(name)
            elif name not in result.restored:
                result.unchanged += 1
            tool.save()

        for name, tool in existing.items():
            if name not in seen and tool.status != ToolStatus.REMOVED:
                # Kept, not deleted: the call log still points at it, and "what
                # did this server used to offer" is a fair question.
                tool.status = ToolStatus.REMOVED
                tool.save(update_fields=["status", "updated_at"])
                result.removed.append(name)
        return result

    @staticmethod
    def _fields(definition: MCPToolDefinition) -> dict:
        return {
            "title": definition.title[:200],
            "description": definition.description,
            "input_schema": definition.input_schema,
            "output_schema": definition.output_schema,
            "annotations": definition.annotations,
        }

    # -- Governance ---------------------------------------------------------

    @staticmethod
    def review(tool: MCPTool, *, enable: bool, actor, note: str = "") -> None:
        """Enable or disable a tool. The decision, not discovery, is what serves it."""
        if tool.status == ToolStatus.REMOVED:
            raise ValueError(f"'{tool.name}' is no longer offered by {tool.server.name}.")

        before = snapshot(tool, ["status", "changed_since_review", "review_note"])
        tool.status = ToolStatus.ENABLED if enable else ToolStatus.DISABLED
        tool.changed_since_review = False
        tool.reviewed_at = timezone.now()
        tool.reviewed_by = actor if getattr(actor, "pk", None) else None
        tool.review_note = note[:500]
        tool.save(update_fields=[
            "status", "changed_since_review", "reviewed_at", "reviewed_by",
            "review_note", "updated_at",
        ])
        AuditService.log(
            action=AuditAction.APPROVE if enable else AuditAction.REJECT,
            resource_type=tool._meta.label,
            resource_id=tool.pk,
            resource_label=tool.qualified_name,
            organization=tool.organization,
            actor=actor,
            old_value=before,
            new_value=snapshot(tool, ["status", "changed_since_review", "review_note"]),
            # The hash pins *which* definition was approved, so a later
            # "this changed after approval" can be checked against the trail.
            metadata={"server": tool.server.name, "definition_hash": tool.definition_hash},
        )

    @staticmethod
    def set_limit_arguments(tool: MCPTool, *, value_argument: str, counterparty_argument: str, actor) -> None:
        """Agent chain link 06: which arguments carry a call's value and its counterparty.

        The MCP Gateway reads them from every call and holds the calling agent
        to its action limits before the call goes out.
        """
        properties = set(((tool.input_schema or {}).get("properties") or {}).keys())
        for name in (value_argument, counterparty_argument):
            if name and properties and name not in properties:
                raise ValueError(f"'{tool.name}' has no argument named '{name}'.")
        before = snapshot(tool, ["value_argument", "counterparty_argument"])
        tool.value_argument = (value_argument or "")[:100]
        tool.counterparty_argument = (counterparty_argument or "")[:100]
        tool.save(update_fields=["value_argument", "counterparty_argument", "updated_at"])
        AuditService.log(
            action=AuditAction.CONFIG_CHANGE,
            resource_type=tool._meta.label,
            resource_id=tool.pk,
            resource_label=tool.qualified_name,
            organization=tool.organization,
            actor=actor,
            old_value=before,
            new_value=snapshot(tool, ["value_argument", "counterparty_argument"]),
            metadata={"irgf": "agent chain link 06: action-limit arguments"},
        )

    @staticmethod
    def set_approval(server: MCPServer, *, approved: bool, actor) -> None:
        fields = ["is_approved", "approved_at", "approved_by"]
        before = snapshot(server, fields)
        server.is_approved = approved
        server.approved_at = timezone.now() if approved else None
        server.approved_by = actor if approved and getattr(actor, "pk", None) else None
        server.save(update_fields=[*fields, "updated_at"])
        AuditService.log(
            action=AuditAction.APPROVE if approved else AuditAction.REJECT,
            resource_type=server._meta.label,
            resource_id=server.pk,
            resource_label=str(server),
            organization=server.organization,
            actor=actor,
            old_value=before,
            new_value=snapshot(server, fields),
        )

    @staticmethod
    def health_check(server: MCPServer) -> HealthCheckResult | None:
        """Probe the server and record the answer. None if it cannot be reached at all."""
        if not server.is_gateway_reachable:
            return None
        try:
            check = server.get_adapter(
                timeout=float(setting("CALL_TIMEOUT_SECONDS", 30))
            ).health_check()
        except Exception as exc:  # misconfiguration: no endpoint, no credential
            check = HealthCheckResult(
                healthy=False, status="unhealthy",
                message=f"{type(exc).__name__}: {exc}"[:500], checked_at=timezone.now(),
            )

        server.health_status = HealthStatus.HEALTHY if check.healthy else HealthStatus.UNHEALTHY
        server.last_health_check = timezone.now()
        server.last_health_latency_ms = check.latency_ms
        server.last_health_message = (check.message or "")[:500]
        server.save(update_fields=[
            "health_status", "last_health_check", "last_health_latency_ms",
            "last_health_message", "updated_at",
        ])
        return check
