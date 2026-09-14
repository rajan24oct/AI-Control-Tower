"""The MCP Gateway request path.

An agent's tool call arrives here and passes through, in order:

    identity -> registry -> access -> policy -> breaker -> upstream -> record

The first four are why the gateway exists. An agent connected straight to an
MCP server can call anything that server offers, with whatever credential it
was handed. Through the gateway it can call only what governance enabled, on
servers it was granted, within policy -- and the credential stays with the
platform, never with the agent.

The rules the AI Gateway follows for being in the request path hold here too:
every upstream call has a timeout, a server that keeps failing is taken out of
the path by its breaker, and recording is best-effort. The Control Tower
failing to write its own log must never turn a tool call that worked into one
that failed.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.constants import Environment, LifecycleStatus
from apps.gateway.models import CircuitState
from apps.mcp_gateway.models import (
    SEPARATOR,
    AccessMode,
    CallChannel,
    CallStatus,
    MCPAccessGrant,
    MCPServer,
    MCPTool,
    MCPToolCall,
    ToolStatus,
)
from integrations.base.exceptions import AdapterConfigurationError
from integrations.base.mcp import MCPProtocolError, MCPToolResult

logger = logging.getLogger(__name__)

#: The action name policies and violations see for a brokered tool call.
POLICY_ACTION = "mcp.tool_call"


def setting(name: str, default):
    return settings.CONTROL_TOWER.get("MCP_GATEWAY", {}).get(name, default)


class UnknownTool(Exception):
    """The name does not resolve to any tool the registry knows."""

    def __init__(self, name: str):
        super().__init__(f"Unknown tool: {name}")
        self.name = name


@dataclass
class ToolCallRequest:
    name: str
    arguments: dict = field(default_factory=dict)
    agent: object = None
    environment: str = ""
    #: The caller's run, if it names one: the call is written onto its timeline.
    execution_id: str = ""
    channel: str = CallChannel.MCP


@dataclass
class ToolCallOutcome:
    status: str
    server: MCPServer
    tool: MCPTool
    result: MCPToolResult | None = None
    reason: str = ""
    policies: list[str] = field(default_factory=list)
    latency_ms: int = 0
    dispatched: bool = False
    call: MCPToolCall | None = None
    #: The agent chain link 06 limit that refused the call, if one did.
    limit: str = ""

    @property
    def reached_server(self) -> bool:
        return self.status in {CallStatus.SUCCEEDED, CallStatus.TOOL_ERROR}

    @property
    def explanation(self) -> str:
        prefix = {
            CallStatus.BLOCKED: "Blocked by the MCP Gateway",
            CallStatus.UNAVAILABLE: "The MCP server is unavailable",
            CallStatus.FAILED: "The MCP server failed",
        }.get(self.status)
        return f"{prefix}: {self.reason}" if prefix else self.reason

    @property
    def http_status(self) -> int:
        """For the REST surface: "you may not" and "nothing answered" differ."""
        return {
            CallStatus.BLOCKED: 403,
            CallStatus.UNAVAILABLE: 503,
            CallStatus.FAILED: 502,
        }.get(self.status, 200)

    def gateway_meta(self) -> dict:
        meta = {
            "status": self.status,
            "server": self.server.slug,
            "tool": self.tool.name,
            "call_id": str(self.call.pk) if self.call else None,
        }
        if self.policies:
            meta["policies"] = self.policies
        return meta

    def as_mcp_result(self) -> dict:
        """The answer in MCP's own shape.

        Refusals are tool results carrying `isError`, not JSON-RPC errors. The
        spec reserves protocol errors for malformed requests, and a model that
        reads an error result can adapt -- try another tool, or tell the user
        why it cannot help -- where a protocol error just breaks its turn.
        """
        if self.reached_server and self.result is not None:
            payload = self.result.to_wire()
        else:
            payload = MCPToolResult.text(self.explanation, is_error=True).to_wire()
        payload["_meta"] = {"io.aicontroltower/gateway": self.gateway_meta()}
        return payload

    def as_payload(self) -> dict:
        reached = self.reached_server and self.result is not None
        return {
            "status": self.status,
            "tool": self.tool.qualified_name,
            "is_error": self.status != CallStatus.SUCCEEDED,
            "content": self.result.content if reached else [],
            "structured_content": self.result.structured_content if reached else None,
            "reason": self.reason,
            "policies": self.policies,
            "latency_ms": self.latency_ms if self.dispatched else None,
            "call_id": str(self.call.pk) if self.call else None,
        }


@dataclass
class CatalogEntry:
    server: MCPServer
    tool: MCPTool

    @property
    def name(self) -> str:
        return self.tool.qualified_name

    def to_wire(self) -> dict:
        """The tool as `tools/list` presents it: renamed into one namespace."""
        tool = self.tool
        entry = {
            "name": self.name,
            "title": f"{tool.title or tool.name} ({self.server.name})",
            "description": tool.description,
            "inputSchema": tool.input_schema or {"type": "object"},
        }
        if tool.output_schema:
            entry["outputSchema"] = tool.output_schema
        if tool.annotations:
            entry["annotations"] = tool.annotations
        return entry

    def as_payload(self) -> dict:
        return {
            **self.to_wire(),
            "server": self.server.slug,
            "server_name": self.server.name,
            "tool": self.tool.name,
            "risk_level": self.tool.risk_level,
        }


class ServerBreaker:
    """Circuit-breaker rules for MCP servers, kept together in one place."""

    @staticmethod
    def threshold() -> int:
        return int(setting("CIRCUIT_FAILURE_THRESHOLD", 5))

    @staticmethod
    def cool_off() -> int:
        return int(setting("CIRCUIT_COOL_OFF_SECONDS", 30))

    @classmethod
    def available(cls, server: MCPServer) -> bool:
        return server.circuit_available(cool_off_seconds=cls.cool_off())

    @staticmethod
    def record_success(server: MCPServer) -> None:
        """Close the breaker. Only writes when there is something to change."""
        if server.circuit_state == CircuitState.CLOSED and server.consecutive_failures == 0:
            return
        server.circuit_state = CircuitState.CLOSED
        server.consecutive_failures = 0
        server.circuit_opened_at = None
        server.last_error = ""
        server.save(update_fields=[
            "circuit_state", "consecutive_failures", "circuit_opened_at", "last_error", "updated_at",
        ])

    @classmethod
    def record_failure(cls, server: MCPServer, error: str) -> None:
        now = timezone.now()
        server.consecutive_failures += 1
        server.last_failure_at = now
        server.last_error = error[:500]

        if server.circuit_state == CircuitState.OPEN:
            # A failed probe. Stay open and start the cool-off again, rather
            # than letting every request after the cool-off become a probe.
            server.circuit_opened_at = now
        elif server.consecutive_failures >= cls.threshold():
            server.circuit_state = CircuitState.OPEN
            server.circuit_opened_at = now
            server.circuit_trips += 1
            logger.warning(
                "MCP circuit opened for %s after %s failures: %s",
                server.name, server.consecutive_failures, error,
            )
        server.save(update_fields=[
            "consecutive_failures", "last_failure_at", "last_error", "circuit_state",
            "circuit_opened_at", "circuit_trips", "updated_at",
        ])


class MCPGateway:
    """Serves tools to agents from the registry, and calls them on their behalf."""

    # -- Catalog ------------------------------------------------------------

    @classmethod
    def catalog(cls, organization, agent=None, *, environment: str = "") -> list[CatalogEntry]:
        """Every tool this caller may call right now, in one namespace.

        Answered from the registry, never live from upstream. Tools a deny
        policy forbids are left out, so a model is not offered something that
        will be refused the moment it tries.
        """
        if organization is None:
            return []
        servers = [
            server
            for server in MCPServer.objects.for_organization(organization).filter(
                status=LifecycleStatus.ACTIVE, is_approved=True
            )
            if server.is_gateway_reachable
        ]
        if not servers:
            return []

        grants = {}
        if agent is not None:
            grants = {
                grant.server_id: grant
                for grant in MCPAccessGrant.objects.filter(server__in=servers, agent=agent)
            }
        denied_by_policy = cls._policy_filter(
            organization, agent, cls._environment(agent, environment)
        )

        entries = []
        tools = (
            MCPTool.objects.filter(server__in=servers, status=ToolStatus.ENABLED)
            .select_related("server")
            .order_by("server__name", "name")
        )
        for tool in tools:
            if cls._access_denial(tool.server, tool, agent, grants.get(tool.server_id)):
                continue
            if denied_by_policy(tool.name):
                continue
            entries.append(CatalogEntry(tool.server, tool))
        return entries

    @staticmethod
    def _policy_filter(organization, agent, environment):
        """A predicate for tools a deny policy forbids this caller outright.

        Runs the policy engine's own tool-access evaluator over the deny
        policies, fetched once -- so the catalog and the call agree, without
        re-reading the policy table for every tool.
        """
        from apps.policies.models import PolicyEffect, PolicyType
        from apps.policies.services import (
            EVALUATORS,
            PolicyContext,
            PolicyEngine,
            excepted_policy_ids,
            in_tier_scope,
        )

        tier = getattr(agent, "risk_tier", None)
        excepted = excepted_policy_ids(organization, agent)
        policies = [
            policy
            for policy in PolicyEngine.policies_for(organization).filter(
                policy_type=PolicyType.TOOL_ACCESS, effect=PolicyEffect.DENY
            )
            # The catalog and the call must agree, so IRGF tier scoping and M17
            # exceptions hold here too.
            if in_tier_scope(policy, tier) and policy.pk not in excepted
        ]
        evaluator = EVALUATORS[PolicyType.TOOL_ACCESS]

        def denied(tool_name: str) -> bool:
            context = PolicyContext(
                agent=agent, action=POLICY_ACTION, tool=tool_name, environment=environment
            )
            for policy in policies:
                try:
                    if evaluator(policy, context):
                        return True
                except Exception:
                    logger.exception("Policy %s raised while filtering the MCP catalog", policy.pk)
            return False

        return denied

    # -- Calling ------------------------------------------------------------

    @staticmethod
    def resolve(organization, name: str) -> MCPTool:
        slug, separator, tool_name = (name or "").partition(SEPARATOR)
        if not (separator and slug and tool_name):
            raise UnknownTool(name)
        tool = (
            MCPTool.objects.for_organization(organization)
            .filter(server__slug=slug, name=tool_name)
            .select_related("server")
            .first()
        )
        if tool is None:
            raise UnknownTool(name)
        return tool

    @classmethod
    def call(cls, organization, request: ToolCallRequest, *, service_account=None) -> ToolCallOutcome:
        """Serve one tool call, or record why it was refused.

        Raises UnknownTool only for a name the registry has never seen. A tool
        it knows but will not serve is answered and recorded as blocked: "an
        agent tried a tool nobody approved" is exactly what governance wants
        to be able to see.
        """
        tool = cls.resolve(organization, request.name)
        environment = cls._environment(request.agent, request.environment)

        outcome = cls._gate(organization, tool.server, tool, request, environment)
        if outcome is None:
            outcome = cls._dispatch(tool.server, tool, request)
        cls._record(organization, request, outcome, service_account)
        return outcome

    @staticmethod
    def _environment(agent, environment: str) -> str:
        return environment or getattr(agent, "environment", "") or Environment.PRODUCTION

    @classmethod
    def _gate(cls, organization, server, tool, request, environment) -> ToolCallOutcome | None:
        """The first refusal that applies, or None if the call may go out."""

        def blocked(reason: str, policies=None) -> ToolCallOutcome:
            return ToolCallOutcome(
                CallStatus.BLOCKED, server, tool, reason=reason, policies=policies or []
            )

        reason = cls._registry_denial(server, tool)
        if reason:
            return blocked(reason)

        grant = None
        if request.agent is not None:
            grant = MCPAccessGrant.objects.filter(server=server, agent=request.agent).first()
        reason = cls._access_denial(server, tool, request.agent, grant)
        if reason:
            return blocked(reason)

        decision = cls._policy(organization, tool, request.agent, environment)
        if not decision.allowed:
            return blocked(decision.reason or "Blocked by policy.", decision.policy_names)
        if decision.requires_approval:
            # Nobody can wait on a human in the middle of a tool call: the
            # agent's turn would hang. Requiring approval means refusing, with
            # the reason, so the run can raise an approval request and return.
            return blocked(
                decision.reason or "This tool call requires human approval.",
                decision.policy_names,
            )

        breach = cls._limits(tool, request)
        if breach is not None:
            outcome = blocked(breach.reason)
            outcome.limit = breach.limit
            return outcome

        if not ServerBreaker.available(server):
            return ToolCallOutcome(
                CallStatus.UNAVAILABLE, server, tool,
                reason=(
                    f"'{server.name}' was taken out of rotation after repeated failures; "
                    "a probe is let through after the cool-off."
                ),
            )
        return None

    @staticmethod
    def _limits(tool: MCPTool, request: ToolCallRequest):
        """Agent chain link 06: the calling agent's value, rate and counterparty limits.

        Preventive: a call over a limit is refused here, before it goes out.
        """
        if request.agent is None:
            return None
        from apps.controlplane import limits

        return limits.check(request.agent, tool, request.arguments)

    @staticmethod
    def _registry_denial(server: MCPServer, tool: MCPTool) -> str:
        if not server.is_active:
            return f"MCP server '{server.name}' is {server.get_status_display().lower()}."
        if not server.is_approved:
            return f"MCP server '{server.name}' has not been approved by governance."
        if not server.is_gateway_reachable:
            return (
                f"MCP server '{server.name}' uses {server.get_transport_display()}, "
                "which the gateway cannot reach."
            )
        if tool.status == ToolStatus.PENDING_REVIEW:
            if tool.changed_since_review:
                return (
                    f"Tool '{tool.name}' changed on '{server.name}' after it was reviewed, "
                    "and is awaiting review again."
                )
            return f"Tool '{tool.name}' is awaiting governance review."
        if tool.status == ToolStatus.DISABLED:
            return f"Tool '{tool.name}' has been disabled by governance."
        if tool.status == ToolStatus.REMOVED:
            return f"Tool '{tool.name}' is no longer offered by '{server.name}'."
        return ""

    @staticmethod
    def _access_denial(server: MCPServer, tool: MCPTool, agent, grant) -> str:
        if agent is not None:
            if agent.is_suspended:
                return (
                    f"Agent '{agent.name}' is suspended by its kill-switch: "
                    f"{agent.suspension_reason or 'no reason recorded'}."
                )
            if not agent.is_active:
                return f"Agent '{agent.name}' is {agent.get_status_display().lower()}."
            # `restricted_tools` is an explicit prohibition, so it is enforced.
            # `allowed_tools` stays descriptive: enforcing it would silently cut
            # agents off from every tool their inventory record does not list.
            if set(agent.restricted_tools or []) & {tool.name, tool.qualified_name}:
                return f"Tool '{tool.name}' is restricted for agent '{agent.name}'."

        if server.access_mode == AccessMode.OPEN:
            return ""
        if agent is None:
            return (
                f"MCP server '{server.name}' is restricted to granted agents, "
                "and this call named no agent."
            )
        if grant is None:
            return f"Agent '{agent.name}' has no access grant for '{server.name}'."
        if grant.is_expired:
            return f"The access grant for agent '{agent.name}' on '{server.name}' has expired."
        if not grant.permits(tool.name):
            return (
                f"The access grant for agent '{agent.name}' on '{server.name}' "
                f"does not include '{tool.name}'."
            )
        return ""

    @staticmethod
    def _policy(organization, tool: MCPTool, agent, environment: str):
        from apps.policies.services import PolicyContext, PolicyEngine

        context = PolicyContext(
            agent=agent,
            action=POLICY_ACTION,
            tool=tool.name,
            environment=environment,
            estimated_cost=Decimal("0"),
        )
        # Agent-level policies only: no model serves a tool call, so gating it
        # on the agent's declared model would check something unrelated.
        return PolicyEngine.check_agent(organization, context, requested_action=POLICY_ACTION)

    @staticmethod
    def _dispatch(server: MCPServer, tool: MCPTool, request: ToolCallRequest) -> ToolCallOutcome:
        started = time.monotonic()

        def outcome(status, **kwargs) -> ToolCallOutcome:
            return ToolCallOutcome(
                status, server, tool, dispatched=True,
                latency_ms=int((time.monotonic() - started) * 1000), **kwargs,
            )

        try:
            adapter = server.get_adapter(timeout=float(setting("CALL_TIMEOUT_SECONDS", 30)))
            result = adapter.call_tool(tool.name, request.arguments or {})
        except MCPProtocolError as exc:
            # The server answered, so it is up -- it refused this request. That
            # is no reason to take it out of rotation for everyone else.
            ServerBreaker.record_success(server)
            return outcome(
                CallStatus.FAILED,
                reason=f"'{server.name}' returned error {exc.code}: {exc.message}"[:1000],
            )
        except AdapterConfigurationError as exc:
            # Our configuration, not their outage: a missing credential must
            # not trip the breaker and hide a healthy server from everyone.
            return outcome(CallStatus.UNAVAILABLE, reason=str(exc)[:1000])
        except Exception as exc:  # unreachable, timed out, broke mid-response
            error = f"{type(exc).__name__}: {exc}"[:500]
            ServerBreaker.record_failure(server, error)
            logger.warning("MCP call %s failed on %s: %s", tool.name, server.name, error)
            return outcome(CallStatus.FAILED, reason=error)

        ServerBreaker.record_success(server)
        if result.is_error:
            return outcome(CallStatus.TOOL_ERROR, result=result, reason=result.text_content[:1000])
        return outcome(CallStatus.SUCCEEDED, result=result)

    # -- Recording ----------------------------------------------------------

    @classmethod
    def _record(cls, organization, request, outcome: ToolCallOutcome, service_account) -> None:
        """Write the call log, best effort: the caller keeps its answer regardless."""
        try:
            with transaction.atomic():
                outcome.call = cls._write(organization, request, outcome, service_account)
        except Exception:
            logger.exception("MCP Gateway could not record a call to %s", request.name)

    @classmethod
    def _write(cls, organization, request, outcome: ToolCallOutcome, service_account) -> MCPToolCall:
        record_content = setting("RECORD_ARGUMENTS", True)
        limit = int(setting("MAX_RECORDED_CHARS", 2000))
        execution = cls._execution(organization, request.execution_id)
        from apps.controlplane.limits import action_facts

        value, counterparty, _ = action_facts(outcome.tool, request.arguments)

        call = MCPToolCall.objects.create(
            organization=organization,
            server=outcome.server,
            tool=outcome.tool,
            tool_name=request.name[:300],
            agent=request.agent,
            service_account=service_account,
            execution=execution,
            channel=request.channel,
            status=outcome.status,
            reason=outcome.reason[:1000],
            policies=outcome.policies,
            latency_ms=outcome.latency_ms if outcome.dispatched else None,
            arguments=_bounded(request.arguments or {}, limit) if record_content else {},
            result_preview=(
                outcome.result.text_content[:limit]
                if record_content and outcome.result is not None
                else ""
            ),
            action_value=value,
            counterparty=counterparty,
            limit_breached=outcome.limit,
        )
        if execution is not None:
            cls._append_to_run(execution, call, outcome)
        return call

    @staticmethod
    def _execution(organization, execution_id: str):
        if not execution_id:
            return None
        from apps.executions.models import Execution

        return Execution.objects.for_organization(organization).filter(
            external_id=execution_id
        ).first()

    @staticmethod
    def _append_to_run(execution, call: MCPToolCall, outcome: ToolCallOutcome) -> None:
        """Put the call on the run's timeline, under the normal event vocabulary.

        Arguments stay in the call log, where their display is gated; the
        timeline carries only what happened.
        """
        from apps.executions.services import ExecutionService

        ExecutionService.append_events(
            execution,
            [
                {
                    "event_type": "TOOL_CALL_COMPLETED" if outcome.reached_server else "ERROR",
                    "duration_ms": call.latency_ms,
                    "status": outcome.status,
                    "error_message": outcome.reason if outcome.status != CallStatus.SUCCEEDED else "",
                    "metadata": {
                        "tool": call.tool_name,
                        "server": outcome.server.name,
                        "mcp_gateway": True,
                        "mcp_call_id": str(call.pk),
                    },
                }
            ],
        )


def _bounded(arguments: dict, limit: int) -> dict:
    """Arguments as recorded: whole if small, a truncated preview if not."""
    encoded = json.dumps(arguments, default=str)
    if len(encoded) <= limit:
        return arguments
    return {"_truncated": True, "preview": encoded[:limit]}
