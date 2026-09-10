"""The agent-engine adapter contract (section 3).

LangGraph, CrewAI, AutoGen and bespoke agents all model "an agent" differently.
This interface is the normalization point: the Control Tower governs runs
without knowing anything about a framework's internals.
"""

from __future__ import annotations

import abc
from decimal import Decimal

from integrations.base.capabilities import Capability, normalize
from integrations.base.config import AdapterConfig
from integrations.base.events import ExecutionStatus, NormalizedEvent
from integrations.base.exceptions import CapabilityNotSupported
from integrations.base.types import (
    AgentExecutionHandle,
    AgentStep,
    ApprovalRequestPayload,
    ExecutionRequest,
    ToolCall,
    TokenUsage,
)


class BaseAgentAdapter(abc.ABC):
    adapter_key: str = ""
    display_name: str = ""
    framework: str = ""
    capabilities: frozenset[str] = frozenset()

    def __init__(self, config: AdapterConfig | None = None):
        self.config = config or AdapterConfig(key=self.adapter_key)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} key={self.adapter_key!r}>"

    def supports(self, capability: str | Capability) -> bool:
        return str(capability) in normalize(self.capabilities)

    def ensure_capability(self, capability: str | Capability) -> None:
        if not self.supports(capability):
            raise CapabilityNotSupported(self.adapter_key, str(capability))

    # -- Lifecycle ----------------------------------------------------------

    @abc.abstractmethod
    def create_execution(self, request: ExecutionRequest) -> AgentExecutionHandle:
        """Start a run in the external engine and return a handle to it."""

    @abc.abstractmethod
    def get_execution(self, execution_id: str) -> AgentExecutionHandle:
        """Current handle for a run, including its status."""

    def get_status(self, execution_id: str) -> ExecutionStatus:
        return self.get_execution(execution_id).status

    def cancel_execution(self, execution_id: str, reason: str = "") -> AgentExecutionHandle:
        raise NotImplementedError(f"{type(self).__name__} does not support cancellation.")

    def resume_execution(self, execution_id: str, decision: str = "approved", notes: str = "") -> AgentExecutionHandle:
        """Continue a run that paused for human approval."""
        raise NotImplementedError(f"{type(self).__name__} does not support resuming.")

    # -- Observability ------------------------------------------------------

    @abc.abstractmethod
    def get_events(self, execution_id: str) -> list[NormalizedEvent]:
        """Run history translated into the normalized event vocabulary."""

    def get_steps(self, execution_id: str) -> list[AgentStep]:
        return []

    def get_tool_calls(self, execution_id: str) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for step in self.get_steps(execution_id):
            calls.extend(step.tool_calls)
        return calls

    def get_usage(self, execution_id: str) -> TokenUsage:
        usage = TokenUsage()
        for step in self.get_steps(execution_id):
            usage.input_tokens += step.usage.input_tokens
            usage.output_tokens += step.usage.output_tokens
        return usage

    def get_cost(self, execution_id: str) -> Decimal:
        """Cost as reported by the engine.

        Returning zero means "the engine does not report cost" -- the Control
        Tower then derives cost from token usage and the pricing registry.
        """
        return Decimal("0")

    # -- Human-in-the-loop --------------------------------------------------

    def request_approval(self, execution_id: str, payload: ApprovalRequestPayload) -> str:
        """Signal that a run needs human sign-off. Returns an approval id."""
        raise NotImplementedError(f"{type(self).__name__} does not support approvals.")
