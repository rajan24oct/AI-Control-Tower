"""Tool adapter contract.

Tools are the actions an agent can take against the outside world -- the point
where AI activity becomes a real side effect, and therefore the point that
policy and approval care about most.
"""

from __future__ import annotations

import abc
from typing import Any

from integrations.base.config import AdapterConfig
from integrations.base.types import ToolCall


class BaseToolAdapter(abc.ABC):
    adapter_key: str = ""
    display_name: str = ""
    #: Tools that change state or reach outside the org warrant approval.
    is_side_effecting: bool = False

    def __init__(self, config: AdapterConfig | None = None):
        self.config = config or AdapterConfig(key=self.adapter_key)

    @abc.abstractmethod
    def describe(self) -> dict[str, Any]:
        """JSON-schema-style description of the tool's name and arguments."""

    @abc.abstractmethod
    def invoke(self, arguments: dict[str, Any]) -> ToolCall:
        """Execute the tool and return a normalized record of the call."""
