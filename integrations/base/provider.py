"""The provider adapter contract (section 2)."""

from __future__ import annotations

import abc
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal

from integrations.base.capabilities import Capability, normalize
from integrations.base.config import AdapterConfig
from integrations.base.exceptions import CapabilityNotSupported
from integrations.base.types import (
    CostEstimate,
    ExecutionRequest,
    ExecutionResult,
    HealthCheckResult,
    ModelInfo,
    StreamChunk,
    TokenUsage,
    UsageRecord,
)


class BaseAIProviderAdapter(abc.ABC):
    """Uniform interface to any model provider.

    Subclasses declare `adapter_key` and `capabilities`. Optional operations
    have defaults that refuse politely rather than forcing every provider to
    implement everything.
    """

    adapter_key: str = ""
    display_name: str = ""
    capabilities: frozenset[str] = frozenset()

    def __init__(self, config: AdapterConfig | None = None):
        self.config = config or AdapterConfig(key=self.adapter_key)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} key={self.adapter_key!r}>"

    # -- Capability discovery ----------------------------------------------

    def supports(self, capability: str | Capability) -> bool:
        return str(capability) in normalize(self.capabilities)

    def ensure_capability(self, capability: str | Capability) -> None:
        if not self.supports(capability):
            raise CapabilityNotSupported(self.adapter_key, str(capability))

    def capability_list(self) -> list[str]:
        return sorted(normalize(self.capabilities))

    # -- Required surface ---------------------------------------------------

    @abc.abstractmethod
    def list_models(self) -> list[ModelInfo]:
        """Every model this provider currently exposes."""

    @abc.abstractmethod
    def health_check(self) -> HealthCheckResult:
        """Cheap liveness probe used by the health monitor."""

    @abc.abstractmethod
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Run a single non-streaming request."""

    # -- Optional surface ---------------------------------------------------

    def get_model(self, model_id: str) -> ModelInfo | None:
        for model in self.list_models():
            if model.model_id == model_id:
                return model
        return None

    def stream(self, request: ExecutionRequest) -> Iterator[StreamChunk]:
        self.ensure_capability(Capability.SUPPORTS_STREAMING)
        raise NotImplementedError(f"{type(self).__name__} advertises streaming but does not implement it.")

    def get_usage(self, start: datetime, end: datetime) -> list[UsageRecord]:
        self.ensure_capability(Capability.SUPPORTS_USAGE_METRICS)
        return []

    def validate_credentials(self) -> HealthCheckResult:
        """Confirm the referenced secret exists and is accepted upstream."""
        if not self.config.has_credential():
            return HealthCheckResult(
                healthy=False,
                status="unhealthy",
                message=f"Credential reference '{self.config.credential_ref}' is not resolvable.",
                checked_at=datetime.now(timezone.utc),
            )
        return self.health_check()

    # -- Shared, provider-neutral defaults ---------------------------------

    def estimate_tokens(self, text: str) -> int:
        """Heuristic token estimate.

        Roughly four characters per token. Adapters with a real tokenizer
        should override; this exists so cost projection works for providers
        that expose no tokenizer at all.
        """
        if not text:
            return 0
        return max(1, len(text) // 4)

    def calculate_cost(
        self, usage: TokenUsage, model: ModelInfo | str, currency: str = "USD"
    ) -> CostEstimate:
        """Cost from the model's published per-1K rates.

        Pricing lives in the registry, never in application logic (section 15).
        """
        if isinstance(model, str):
            resolved = self.get_model(model)
        else:
            resolved = model

        if resolved is None:
            return CostEstimate(currency=currency, pricing_source="unknown-model")

        input_rate = resolved.input_cost_per_1k or Decimal("0")
        output_rate = resolved.output_cost_per_1k or Decimal("0")

        input_cost = (Decimal(usage.input_tokens) / Decimal(1000)) * input_rate
        output_cost = (Decimal(usage.output_tokens) / Decimal(1000)) * output_rate

        return CostEstimate(
            amount=input_cost + output_cost,
            currency=resolved.currency or currency,
            input_cost=input_cost,
            output_cost=output_cost,
            pricing_source=f"{self.adapter_key}:{resolved.model_id}",
        )
