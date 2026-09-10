"""MCP server adapter contract.

Kept minimal on purpose: full MCP management is explicitly out of MVP scope
(section 40). This interface exists so it can be added later without the rest
of the platform changing shape.
"""

from __future__ import annotations

import abc
from typing import Any

from integrations.base.config import AdapterConfig
from integrations.base.types import ToolCall


class BaseMCPAdapter(abc.ABC):
    adapter_key: str = ""
    display_name: str = ""

    def __init__(self, config: AdapterConfig | None = None):
        self.config = config or AdapterConfig(key=self.adapter_key)

    @abc.abstractmethod
    def list_tools(self) -> list[dict[str, Any]]:
        """Tools the MCP server advertises."""

    @abc.abstractmethod
    def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolCall:
        """Invoke a tool on the MCP server."""

    def list_resources(self) -> list[dict[str, Any]]:
        return []
