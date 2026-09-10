"""Adapter registry (section 22).

Application code asks the registry for an adapter by key. It never branches on
provider identity -- `if provider == "openai" ... elif ...` is exactly the
pattern this exists to prevent. Adding a vendor means registering a class, not
editing call sites.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from integrations.base.agent import BaseAgentAdapter
from integrations.base.config import AdapterConfig
from integrations.base.cost import BaseCostAdapter
from integrations.base.exceptions import AdapterNotFound
from integrations.base.execution import BaseExecutionAdapter
from integrations.base.mcp import BaseMCPAdapter
from integrations.base.provider import BaseAIProviderAdapter
from integrations.base.telemetry import BaseObservabilityAdapter
from integrations.base.tool import BaseToolAdapter

logger = logging.getLogger(__name__)


class AdapterKind:
    PROVIDER = "provider"
    AGENT = "agent"
    EXECUTION = "execution"
    TOOL = "tool"
    MCP = "mcp"
    OBSERVABILITY = "observability"
    COST = "cost"


_BASE_CLASSES = {
    AdapterKind.PROVIDER: BaseAIProviderAdapter,
    AdapterKind.AGENT: BaseAgentAdapter,
    AdapterKind.EXECUTION: BaseExecutionAdapter,
    AdapterKind.TOOL: BaseToolAdapter,
    AdapterKind.MCP: BaseMCPAdapter,
    AdapterKind.OBSERVABILITY: BaseObservabilityAdapter,
    AdapterKind.COST: BaseCostAdapter,
}


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, dict[str, type]] = {kind: {} for kind in _BASE_CLASSES}

    # -- Registration -------------------------------------------------------

    def register(self, kind: str, key: str, adapter_class: type, *, replace: bool = False) -> type:
        if kind not in _BASE_CLASSES:
            raise ValueError(f"Unknown adapter kind '{kind}'.")

        expected_base = _BASE_CLASSES[kind]
        if not issubclass(adapter_class, expected_base):
            raise TypeError(
                f"{adapter_class.__name__} must subclass {expected_base.__name__} "
                f"to register as a '{kind}' adapter."
            )

        existing = self._adapters[kind].get(key)
        if existing and existing is not adapter_class and not replace:
            raise ValueError(
                f"A different '{kind}' adapter is already registered under '{key}': "
                f"{existing.__name__}. Pass replace=True to override."
            )

        self._adapters[kind][key] = adapter_class
        return adapter_class

    def register_provider(self, key: str, adapter_class: type, **kwargs) -> type:
        return self.register(AdapterKind.PROVIDER, key, adapter_class, **kwargs)

    def register_agent(self, key: str, adapter_class: type, **kwargs) -> type:
        return self.register(AdapterKind.AGENT, key, adapter_class, **kwargs)

    def register_execution(self, key: str, adapter_class: type, **kwargs) -> type:
        return self.register(AdapterKind.EXECUTION, key, adapter_class, **kwargs)

    # -- Lookup -------------------------------------------------------------

    def get_class(self, kind: str, key: str) -> type:
        try:
            return self._adapters[kind][key]
        except KeyError:
            available = ", ".join(sorted(self._adapters.get(kind, {}))) or "none registered"
            raise AdapterNotFound(
                f"No '{kind}' adapter registered under '{key}'. Available: {available}."
            ) from None

    def get(self, kind: str, key: str, config: AdapterConfig | None = None) -> Any:
        adapter_class = self.get_class(kind, key)
        return adapter_class(config or AdapterConfig(key=key))

    def get_provider(self, key: str, config: AdapterConfig | None = None) -> BaseAIProviderAdapter:
        return self.get(AdapterKind.PROVIDER, key, config)

    def get_agent(self, key: str, config: AdapterConfig | None = None) -> BaseAgentAdapter:
        return self.get(AdapterKind.AGENT, key, config)

    def get_execution(self, key: str, config: AdapterConfig | None = None) -> BaseExecutionAdapter:
        return self.get(AdapterKind.EXECUTION, key, config)

    # -- Introspection ------------------------------------------------------

    def has(self, kind: str, key: str) -> bool:
        return key in self._adapters.get(kind, {})

    def keys(self, kind: str) -> list[str]:
        return sorted(self._adapters.get(kind, {}))

    def choices(self, kind: str) -> list[tuple[str, str]]:
        """`(key, label)` pairs for populating form fields and API metadata."""
        return [
            (key, getattr(cls, "display_name", "") or key)
            for key, cls in sorted(self._adapters.get(kind, {}).items())
        ]

    def describe(self, kind: str) -> list[dict[str, Any]]:
        described = []
        for key, cls in sorted(self._adapters.get(kind, {}).items()):
            described.append(
                {
                    "key": key,
                    "kind": kind,
                    "class": f"{cls.__module__}.{cls.__name__}",
                    "display_name": getattr(cls, "display_name", "") or key,
                    "capabilities": sorted(str(c) for c in getattr(cls, "capabilities", ())),
                    "framework": getattr(cls, "framework", ""),
                }
            )
        return described

    def describe_all(self) -> dict[str, list[dict[str, Any]]]:
        return {kind: self.describe(kind) for kind in _BASE_CLASSES}

    def kinds(self) -> Iterable[str]:
        return _BASE_CLASSES.keys()

    def clear(self) -> None:
        """Reset the registry. Intended for tests."""
        for kind in self._adapters:
            self._adapters[kind].clear()


#: Process-wide registry. Import this, do not instantiate your own.
registry = AdapterRegistry()


def register_provider(key: str, **kwargs):
    """Class decorator: `@register_provider("openai")`."""

    def decorator(cls):
        registry.register_provider(key, cls, **kwargs)
        return cls

    return decorator


def register_agent(key: str, **kwargs):
    def decorator(cls):
        registry.register_agent(key, cls, **kwargs)
        return cls

    return decorator


def register_execution(key: str, **kwargs):
    def decorator(cls):
        registry.register_execution(key, cls, **kwargs)
        return cls

    return decorator
