"""Outbound observability contract.

Lets the Control Tower mirror normalized events into an external APM or trace
backend without any module importing that vendor's SDK directly.
"""

from __future__ import annotations

import abc

from integrations.base.config import AdapterConfig
from integrations.base.events import NormalizedEvent


class BaseObservabilityAdapter(abc.ABC):
    adapter_key: str = ""
    display_name: str = ""

    def __init__(self, config: AdapterConfig | None = None):
        self.config = config or AdapterConfig(key=self.adapter_key)

    @abc.abstractmethod
    def emit_event(self, event: NormalizedEvent) -> None:
        """Forward one event downstream."""

    def emit_batch(self, events: list[NormalizedEvent]) -> None:
        for event in events:
            self.emit_event(event)

    def flush(self) -> None:
        """Push any buffered events. No-op for synchronous adapters."""
