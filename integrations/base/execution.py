"""Inbound execution normalization (section 14).

An execution adapter turns whatever an external engine sends -- a webhook body,
a queue message, a vendor trace export -- into Control Tower events.
"""

from __future__ import annotations

import abc
from typing import Any

from integrations.base.config import AdapterConfig
from integrations.base.events import ExecutionStatus, NormalizedEvent


class BaseExecutionAdapter(abc.ABC):
    adapter_key: str = ""
    display_name: str = ""

    def __init__(self, config: AdapterConfig | None = None):
        self.config = config or AdapterConfig(key=self.adapter_key)

    @abc.abstractmethod
    def normalize_execution(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Map a raw payload onto Control Tower execution fields.

        Returned keys are a subset of: external_id, status, agent, application,
        provider, model, trace_id, input_tokens, output_tokens, cost,
        latency_ms, error_message, metadata.

        The asset keys carry whatever identifier the engine uses -- a slug, a
        display name, a UUID. Resolving them against the registry is the
        execution service's job, not the adapter's.
        """

    @abc.abstractmethod
    def normalize_events(self, payload: dict[str, Any]) -> list[NormalizedEvent]:
        """Extract normalized timeline events from a raw payload."""

    def verify_signature(self, headers: dict[str, str], raw_body: bytes) -> bool:
        """Authenticate an inbound payload. Default: no signature scheme."""
        return True

    def map_status(self, raw_status: str) -> ExecutionStatus:
        """Translate an engine's status vocabulary into ours."""
        normalized = (raw_status or "").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "success": ExecutionStatus.COMPLETED,
            "succeeded": ExecutionStatus.COMPLETED,
            "ok": ExecutionStatus.COMPLETED,
            "done": ExecutionStatus.COMPLETED,
            "finished": ExecutionStatus.COMPLETED,
            "error": ExecutionStatus.FAILED,
            "failure": ExecutionStatus.FAILED,
            "in_progress": ExecutionStatus.RUNNING,
            "started": ExecutionStatus.RUNNING,
            "pending": ExecutionStatus.QUEUED,
            "waiting": ExecutionStatus.WAITING_FOR_APPROVAL,
            "paused": ExecutionStatus.WAITING_FOR_APPROVAL,
            "aborted": ExecutionStatus.CANCELLED,
            "timeout": ExecutionStatus.TIMED_OUT,
        }
        if normalized in aliases:
            return aliases[normalized]
        try:
            return ExecutionStatus(normalized)
        except ValueError:
            return ExecutionStatus.RUNNING
