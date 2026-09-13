"""MCP server adapter contract.

The Control Tower talks to an MCP server in two roles. As a *registry* it asks
the server what it offers (`initialize`, `tools/list`) so governance can review
it; as a *gateway* it calls tools on an agent's behalf. Both go through this
contract, so the platform never branches on how a server is reached -- a new
transport is a new adapter, not a new code path.

Like the rest of `integrations`, nothing here imports Django.
"""

from __future__ import annotations

import abc
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from integrations.base.config import AdapterConfig
from integrations.base.exceptions import AdapterError
from integrations.base.types import HealthCheckResult

#: Newest MCP revision this platform speaks, offered first in every handshake.
LATEST_PROTOCOL_VERSION = "2025-06-18"

#: Revisions whose tool surface is compatible with what the gateway relies on.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


class MCPError(AdapterError):
    """Base class for MCP adapter failures."""


class MCPTransportError(MCPError):
    """The server could not be reached, timed out, or answered with an HTTP error.

    This is the failure that counts against a server's breaker: it says nothing
    about the tool and everything about whether the server is there.
    """


class MCPProtocolError(MCPError):
    """The server answered, with a JSON-RPC error.

    The server is alive -- it refused this request. Kept apart from transport
    failures so a tool rejecting bad arguments never takes its server out of
    rotation.
    """

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"MCP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


@dataclass(frozen=True)
class MCPServerInfo:
    """What a server said about itself in the `initialize` handshake."""

    name: str = ""
    version: str = ""
    title: str = ""
    protocol_version: str = ""
    capabilities: dict[str, Any] = field(default_factory=dict)
    instructions: str = ""

    @classmethod
    def from_initialize(cls, result: dict[str, Any]) -> "MCPServerInfo":
        info = result.get("serverInfo") or {}
        return cls(
            name=str(info.get("name", "")),
            version=str(info.get("version", "")),
            title=str(info.get("title", "")),
            protocol_version=str(result.get("protocolVersion", "")),
            capabilities=result.get("capabilities") or {},
            instructions=str(result.get("instructions", "") or ""),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "title": self.title,
            "instructions": self.instructions,
        }


@dataclass
class MCPToolDefinition:
    """One tool as a server advertises it."""

    name: str
    description: str = ""
    title: str = ""
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> "MCPToolDefinition":
        annotations = dict(payload.get("annotations") or {})
        return cls(
            name=str(payload["name"]),
            description=str(payload.get("description", "") or ""),
            # 2025-06-18 moved the display name to a top-level `title`; older
            # servers put it in the annotations.
            title=str(payload.get("title") or annotations.get("title") or ""),
            input_schema=payload.get("inputSchema") or {"type": "object"},
            output_schema=payload.get("outputSchema") or None,
            annotations=annotations,
        )

    def definition_hash(self) -> str:
        """Fingerprint of everything a reviewer approved.

        The name is left out -- it is the key. Everything a model reads when
        deciding to call the tool is in, because a changed description is
        exactly how a tool is turned against the agent that trusts it.
        """
        canonical = json.dumps(
            {
                "title": self.title,
                "description": self.description,
                "input_schema": self.input_schema,
                "output_schema": self.output_schema,
                "annotations": self.annotations,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class MCPToolResult:
    """The outcome of `tools/call`, kept in the protocol's own shape."""

    content: list[dict[str, Any]] = field(default_factory=list)
    structured_content: dict[str, Any] | None = None
    #: The tool ran and reported a failure. Not a protocol or transport error.
    is_error: bool = False

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> "MCPToolResult":
        content = payload.get("content")
        return cls(
            content=content if isinstance(content, list) else [],
            structured_content=payload.get("structuredContent"),
            is_error=bool(payload.get("isError", False)),
        )

    @classmethod
    def text(cls, text: str, *, is_error: bool = False) -> "MCPToolResult":
        return cls(content=[{"type": "text", "text": text}], is_error=is_error)

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"content": self.content, "isError": self.is_error}
        if self.structured_content is not None:
            payload["structuredContent"] = self.structured_content
        return payload

    @property
    def text_content(self) -> str:
        return "\n".join(
            str(item.get("text", ""))
            for item in self.content
            if isinstance(item, dict) and item.get("type") == "text"
        )


class BaseMCPAdapter(abc.ABC):
    """Uniform interface to an MCP server, whatever its transport."""

    adapter_key: str = ""
    display_name: str = ""
    capabilities: frozenset[str] = frozenset()

    def __init__(self, config: AdapterConfig | None = None):
        self.config = config or AdapterConfig(key=self.adapter_key)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} key={self.adapter_key!r}>"

    @abc.abstractmethod
    def initialize(self) -> MCPServerInfo:
        """Run the handshake and report what the server said about itself."""

    @abc.abstractmethod
    def list_tools(self) -> list[MCPToolDefinition]:
        """Every tool the server advertises, across all pages."""

    @abc.abstractmethod
    def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        """Invoke one tool. Raises MCPTransportError or MCPProtocolError."""

    def list_resources(self) -> list[dict[str, Any]]:
        return []

    def health_check(self) -> HealthCheckResult:
        """A handshake is the cheapest request every server must answer."""
        started = time.monotonic()
        try:
            info = self.initialize()
        except MCPError as exc:
            return HealthCheckResult(
                healthy=False,
                status="unhealthy",
                message=str(exc)[:500],
                checked_at=datetime.now(timezone.utc),
            )
        return HealthCheckResult(
            healthy=True,
            status="healthy",
            latency_ms=int((time.monotonic() - started) * 1000),
            message=f"{info.name or 'Server'} {info.version}".strip(),
            checked_at=datetime.now(timezone.utc),
        )
