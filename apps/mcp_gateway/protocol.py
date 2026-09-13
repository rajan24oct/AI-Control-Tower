"""The MCP Gateway's own MCP endpoint: JSON-RPC 2.0 over Streamable HTTP.

This is the "one URL" half of the module. Any MCP client -- an IDE, a desktop
assistant, an agent framework -- connects here once and sees every tool it is
allowed, from every governed server, in a single namespace.

Stateless on purpose. No `Mcp-Session-Id` is issued: every request carries its
own credential and agent identity, so any worker can answer any request and
nothing is lost when one restarts. The spec allows a server to work this way.

Only tools are served. Resources and prompts are not proxied, and the gateway
does not advertise them, so a conforming client will not ask for them.

Kept free of HTTP: the view turns a request into a `Caller` and a payload, and
everything JSON-RPC happens here, where it can be tested without a request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from apps.mcp_gateway.models import CallChannel
from apps.mcp_gateway.services import MCPGateway, ToolCallRequest, UnknownTool
from integrations.base.mcp import LATEST_PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS

logger = logging.getLogger(__name__)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
#: Implementation-defined range (-32000 to -32099) per JSON-RPC 2.0.
UNKNOWN_AGENT = -32001

SERVER_INFO = {
    "name": "ai-control-tower-mcp-gateway",
    "title": "AI Control Tower MCP Gateway",
    "version": "1.0.0",
}

#: Where a caller may name the run a tool call belongs to, inside `_meta`.
EXECUTION_META_KEYS = ("io.aicontroltower/executionId", "executionId")


class ProtocolError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def error_response(message_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


@dataclass
class Caller:
    organization: Any
    agent: Any = None
    service_account: Any = None
    environment: str = ""
    execution_id: str = ""


class MCPEndpoint:
    def __init__(self, caller: Caller):
        self.caller = caller

    def handle(self, payload) -> dict | list | None:
        """Answer one message or a batch. None means nothing to send back (202)."""
        if isinstance(payload, list):
            if not payload:
                return error_response(None, INVALID_REQUEST, "Empty batch.")
            replies = [reply for reply in map(self._one, payload) if reply is not None]
            return replies or None
        return self._one(payload)

    def _one(self, message) -> dict | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            message_id = message.get("id") if isinstance(message, dict) else None
            return error_response(message_id, INVALID_REQUEST, "Not a JSON-RPC 2.0 message.")
        if "method" not in message:
            # A response to a request the gateway never sends. Nothing to do.
            return None
        if "id" not in message:
            # A notification (initialized, cancelled, ...): accepted, never answered.
            return None

        message_id = message["id"]
        method = message["method"]
        handler = self._handlers().get(method) if isinstance(method, str) else None
        if handler is None:
            return error_response(message_id, METHOD_NOT_FOUND, f"Method not found: {method}")

        params = message.get("params") or {}
        if not isinstance(params, dict):
            return error_response(message_id, INVALID_PARAMS, "params must be an object.")

        try:
            return {"jsonrpc": "2.0", "id": message_id, "result": handler(params)}
        except ProtocolError as exc:
            return error_response(message_id, exc.code, exc.message)
        except Exception:
            logger.exception("MCP endpoint failed while handling %s", method)
            return error_response(message_id, INTERNAL_ERROR, "Internal error.")

    def _handlers(self) -> dict:
        return {
            "initialize": self.initialize,
            "ping": self.ping,
            "tools/list": self.list_tools,
            "tools/call": self.call_tool,
        }

    # -- Methods ------------------------------------------------------------

    def initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        # Echo a version we speak; otherwise offer our newest and let the
        # client decide whether it can follow.
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        who = f"agent '{self.caller.agent.name}'" if self.caller.agent else "this caller"
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": (
                f"Tools from the organization's governed MCP servers, limited to what {who} "
                "may use. Each name is '<server>__<tool>'. A call the gateway refuses returns "
                "an error result saying why; repeating it will not change the answer."
            ),
        }

    def ping(self, params: dict) -> dict:
        return {}

    def list_tools(self, params: dict) -> dict:
        entries = MCPGateway.catalog(
            self.caller.organization, self.caller.agent, environment=self.caller.environment
        )
        return {"tools": [entry.to_wire() for entry in entries]}

    def call_tool(self, params: dict) -> dict:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise ProtocolError(INVALID_PARAMS, "tools/call needs a tool name.")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ProtocolError(INVALID_PARAMS, "arguments must be an object.")

        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        execution_id = next(
            (str(meta[key]) for key in EXECUTION_META_KEYS if meta.get(key)),
            self.caller.execution_id,
        )

        try:
            outcome = MCPGateway.call(
                self.caller.organization,
                ToolCallRequest(
                    name=name,
                    arguments=arguments,
                    agent=self.caller.agent,
                    environment=self.caller.environment,
                    execution_id=execution_id[:200],
                    channel=CallChannel.MCP,
                ),
                service_account=self.caller.service_account,
            )
        except UnknownTool:
            # The spec's own example: an unknown tool is a protocol error.
            raise ProtocolError(INVALID_PARAMS, f"Unknown tool: {name}") from None
        return outcome.as_mcp_result()
