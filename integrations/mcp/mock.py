"""Mock MCP server adapter.

Makes the MCP Gateway demonstrable with no MCP server deployed, the way the
mock provider does for models. Tools come from the server record's options
(`tools`, written in MCP's own wire shape) or a small default set, and results
are derived from the call itself, so demos and tests reproduce exactly.

`simulate_outage: true` in the options makes every request fail the way an
unreachable server would -- which is how the demo shows a breaker opening.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from integrations.base.mcp import (
    LATEST_PROTOCOL_VERSION,
    BaseMCPAdapter,
    MCPProtocolError,
    MCPServerInfo,
    MCPToolDefinition,
    MCPToolResult,
    MCPTransportError,
)
from integrations.registry import register_mcp

DEFAULT_TOOLS = [
    {
        "name": "search",
        "title": "Search",
        "description": "Full-text search across the server's records. Returns the best matches.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_record",
        "title": "Get record",
        "description": "Fetch one record by its identifier.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "create_record",
        "title": "Create record",
        "description": "Create a new record from the supplied fields.",
        "inputSchema": {
            "type": "object",
            "properties": {"fields": {"type": "object"}},
            "required": ["fields"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
]


@register_mcp("mock")
class MockMCPAdapter(BaseMCPAdapter):
    adapter_key = "mock"
    display_name = "Mock / Demo MCP Server"

    def _available(self) -> None:
        if self.config.option("simulate_outage", False):
            raise MCPTransportError(
                f"{self.config.name or 'Mock server'} is not responding (simulated outage)."
            )

    def _definitions(self) -> list[MCPToolDefinition]:
        return [
            MCPToolDefinition.from_wire(tool)
            for tool in (self.config.option("tools") or DEFAULT_TOOLS)
        ]

    def initialize(self) -> MCPServerInfo:
        self._available()
        return MCPServerInfo(
            name=self.config.option("server_name") or "mock-mcp-server",
            version=str(self.config.option("server_version") or "1.0.0"),
            title=self.config.name,
            protocol_version=LATEST_PROTOCOL_VERSION,
            capabilities={"tools": {"listChanged": False}},
        )

    def list_tools(self) -> list[MCPToolDefinition]:
        self._available()
        return self._definitions()

    def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        self._available()
        definition = next((d for d in self._definitions() if d.name == name), None)
        if definition is None:
            raise MCPProtocolError(-32602, f"Unknown tool: {name}")

        arguments = arguments or {}
        missing = [key for key in definition.input_schema.get("required", []) if key not in arguments]
        if missing:
            # Bad input is the tool's error to report, not a protocol failure:
            # the model reads it and can correct itself.
            return MCPToolResult.text(
                f"Missing required argument(s): {', '.join(missing)}.", is_error=True
            )

        canonical = json.dumps(arguments, sort_keys=True, default=str)
        digest = hashlib.sha256(f"{name}:{canonical}".encode("utf-8")).hexdigest()[:10]
        return MCPToolResult.text(f"[mock:{name}] result {digest} for {canonical}")
