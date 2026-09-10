"""The normalized execution event model (section 4).

Every engine -- an OpenAI call, a LangGraph run, a bespoke service -- reports
into this one vocabulary. Adapters translate; the Control Tower only ever sees
normalized events, which is what makes observability vendor-neutral.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    EXECUTION_STARTED = "EXECUTION_STARTED"
    PROMPT_CREATED = "PROMPT_CREATED"
    MODEL_REQUEST_STARTED = "MODEL_REQUEST_STARTED"
    MODEL_REQUEST_COMPLETED = "MODEL_REQUEST_COMPLETED"
    TOOL_SELECTED = "TOOL_SELECTED"
    TOOL_CALL_STARTED = "TOOL_CALL_STARTED"
    TOOL_CALL_COMPLETED = "TOOL_CALL_COMPLETED"
    AGENT_STARTED = "AGENT_STARTED"
    AGENT_STEP_STARTED = "AGENT_STEP_STARTED"
    AGENT_STEP_COMPLETED = "AGENT_STEP_COMPLETED"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    ERROR = "ERROR"
    EXECUTION_COMPLETED = "EXECUTION_COMPLETED"
    EXECUTION_CANCELLED = "EXECUTION_CANCELLED"


class ExecutionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


TERMINAL_STATUSES = frozenset(
    {
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.TIMED_OUT,
    }
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class NormalizedEvent:
    """One point on an execution timeline, in Control Tower terms."""

    event_type: EventType
    execution_id: str
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    organization_id: str | None = None
    application_id: str | None = None
    agent_id: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    timestamp: datetime = field(default_factory=_now)
    duration_ms: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost: Decimal = Decimal("0")
    status: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    trace_id: str | None = None
    parent_event_id: str | None = None
    sequence: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "execution_id": self.execution_id,
            "organization_id": self.organization_id,
            "application_id": self.application_id,
            "agent_id": self.agent_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "event_type": str(self.event_type),
            "timestamp": self.timestamp.isoformat(),
            "duration_ms": self.duration_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost": str(self.cost),
            "status": self.status,
            "metadata": self.metadata,
            "error_message": self.error_message,
            "trace_id": self.trace_id,
            "parent_event_id": self.parent_event_id,
            "sequence": self.sequence,
        }
