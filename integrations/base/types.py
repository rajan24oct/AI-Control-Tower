"""Normalized data contracts exchanged with adapters.

These dataclasses are the *only* shapes the Control Tower understands. An
adapter's job is to translate its vendor's payloads into these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from integrations.base.events import ExecutionStatus


@dataclass(frozen=True)
class ModelInfo:
    """A model as described by the upstream provider."""

    model_id: str
    name: str
    family: str = ""
    model_type: str = "chat"
    context_window: int | None = None
    max_output_tokens: int | None = None
    capabilities: frozenset[str] = frozenset()
    input_cost_per_1k: Decimal | None = None
    output_cost_per_1k: Decimal | None = None
    currency: str = "USD"
    deployment: str = ""
    region: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionRequest:
    """A normalized request to run something on a provider or agent engine."""

    model_id: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    prompt: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    max_tokens: int | None = None
    temperature: float | None = None
    stream: bool = False
    trace_id: str | None = None
    execution_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class ExecutionResult:
    """The normalized outcome of a single provider call."""

    execution_id: str
    status: ExecutionStatus = ExecutionStatus.COMPLETED
    output_text: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost: Decimal = Decimal("0")
    currency: str = "USD"
    latency_ms: int = 0
    model_id: str = ""
    tool_calls: list["ToolCall"] = field(default_factory=list)
    error_message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamChunk:
    text: str = ""
    is_final: bool = False
    usage: TokenUsage | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCall:
    tool_name: str
    call_id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    status: str = "completed"
    duration_ms: int | None = None
    error_message: str = ""


@dataclass
class AgentStep:
    """One reasoning/acting step inside an agent run."""

    step_index: int
    name: str
    status: str = "completed"
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    tool_calls: list[ToolCall] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentExecutionHandle:
    """Reference to a run happening inside an external agent engine."""

    execution_id: str
    remote_id: str = ""
    status: ExecutionStatus = ExecutionStatus.QUEUED
    trace_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthCheckResult:
    healthy: bool
    status: str = "unknown"
    latency_ms: int | None = None
    message: str = ""
    checked_at: datetime | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class CostEstimate:
    amount: Decimal = Decimal("0")
    currency: str = "USD"
    input_cost: Decimal = Decimal("0")
    output_cost: Decimal = Decimal("0")
    pricing_source: str = ""


@dataclass
class UsageRecord:
    """A usage/cost datapoint pulled back from a provider's own billing API."""

    model_id: str
    period_start: datetime
    period_end: datetime
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    cost: Decimal = Decimal("0")
    currency: str = "USD"


@dataclass
class ApprovalRequestPayload:
    """An agent engine asking a human for permission to proceed."""

    execution_id: str
    requested_action: str
    reason: str = ""
    risk_level: str = "moderate"
    metadata: dict[str, Any] = field(default_factory=dict)
