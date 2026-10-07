"""Abstract interfaces every external AI system is reached through."""

from integrations.base.a2a import BaseA2AAdapter
from integrations.base.agent import BaseAgentAdapter
from integrations.base.capabilities import Capability
from integrations.base.config import AdapterConfig
from integrations.base.cost import BaseCostAdapter
from integrations.base.events import EventType, ExecutionStatus, NormalizedEvent
from integrations.base.exceptions import (
    AdapterConfigurationError,
    AdapterDependencyMissing,
    AdapterError,
    AdapterNotFound,
    CapabilityNotSupported,
    CredentialsUnavailable,
    ProviderUnavailable,
)
from integrations.base.execution import BaseExecutionAdapter
from integrations.base.mcp import BaseMCPAdapter
from integrations.base.provider import BaseAIProviderAdapter
from integrations.base.telemetry import BaseObservabilityAdapter
from integrations.base.tool import BaseToolAdapter

__all__ = [
    "AdapterConfig",
    "AdapterConfigurationError",
    "AdapterDependencyMissing",
    "AdapterError",
    "AdapterNotFound",
    "BaseA2AAdapter",
    "BaseAIProviderAdapter",
    "BaseAgentAdapter",
    "BaseCostAdapter",
    "BaseExecutionAdapter",
    "BaseMCPAdapter",
    "BaseObservabilityAdapter",
    "BaseToolAdapter",
    "Capability",
    "CapabilityNotSupported",
    "CredentialsUnavailable",
    "EventType",
    "ExecutionStatus",
    "NormalizedEvent",
    "ProviderUnavailable",
]
