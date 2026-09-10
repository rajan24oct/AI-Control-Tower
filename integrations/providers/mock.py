"""Mock provider adapter.

Makes the entire Control Tower demonstrable with no vendor credentials
(section 38). Output is deterministic -- seeded from the request -- so demos and
tests are reproducible.
"""

from __future__ import annotations

import hashlib
import random
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from integrations.base.capabilities import Capability
from integrations.base.provider import BaseAIProviderAdapter
from integrations.base.types import (
    ExecutionRequest,
    ExecutionResult,
    HealthCheckResult,
    ModelInfo,
    StreamChunk,
    TokenUsage,
    UsageRecord,
)
from integrations.base.events import ExecutionStatus
from integrations.registry import register_provider

_MODELS = [
    ModelInfo(
        model_id="mock-large",
        name="Mock Large",
        family="mock",
        model_type="chat",
        context_window=200_000,
        max_output_tokens=8192,
        capabilities=frozenset(
            {
                Capability.SUPPORTS_CHAT,
                Capability.SUPPORTS_STREAMING,
                Capability.SUPPORTS_TOOL_CALLING,
                Capability.SUPPORTS_VISION,
                Capability.SUPPORTS_STRUCTURED_OUTPUT,
                Capability.SUPPORTS_REASONING,
            }
        ),
        input_cost_per_1k=Decimal("0.0030"),
        output_cost_per_1k=Decimal("0.0150"),
    ),
    ModelInfo(
        model_id="mock-small",
        name="Mock Small",
        family="mock",
        model_type="chat",
        context_window=128_000,
        max_output_tokens=4096,
        capabilities=frozenset(
            {
                Capability.SUPPORTS_CHAT,
                Capability.SUPPORTS_STREAMING,
                Capability.SUPPORTS_TOOL_CALLING,
            }
        ),
        input_cost_per_1k=Decimal("0.00025"),
        output_cost_per_1k=Decimal("0.00125"),
    ),
    ModelInfo(
        model_id="mock-embed",
        name="Mock Embeddings",
        family="mock",
        model_type="embedding",
        context_window=8192,
        capabilities=frozenset({Capability.SUPPORTS_EMBEDDINGS}),
        input_cost_per_1k=Decimal("0.00002"),
        output_cost_per_1k=Decimal("0"),
    ),
]


@register_provider("mock")
class MockProviderAdapter(BaseAIProviderAdapter):
    adapter_key = "mock"
    display_name = "Mock Provider"
    capabilities = frozenset(
        {
            Capability.SUPPORTS_CHAT,
            Capability.SUPPORTS_STREAMING,
            Capability.SUPPORTS_EMBEDDINGS,
            Capability.SUPPORTS_VISION,
            Capability.SUPPORTS_TOOL_CALLING,
            Capability.SUPPORTS_STRUCTURED_OUTPUT,
            Capability.SUPPORTS_REASONING,
            Capability.SUPPORTS_USAGE_METRICS,
            Capability.SUPPORTS_HEALTH_CHECK,
            Capability.SUPPORTS_COST_REPORTING,
        }
    )

    def _rng(self, seed_text: str) -> random.Random:
        digest = hashlib.sha256(seed_text.encode("utf-8")).hexdigest()
        return random.Random(int(digest[:16], 16))

    def _prompt_text(self, request: ExecutionRequest) -> str:
        if request.prompt:
            return request.prompt
        return " ".join(str(m.get("content", "")) for m in request.messages)

    def list_models(self) -> list[ModelInfo]:
        return list(_MODELS)

    def health_check(self) -> HealthCheckResult:
        return HealthCheckResult(
            healthy=True,
            status="healthy",
            latency_ms=12,
            message="Mock provider is always available.",
            checked_at=datetime.now(timezone.utc),
        )

    def validate_credentials(self) -> HealthCheckResult:
        # The mock provider needs no secret; overriding keeps demos credential-free.
        return self.health_check()

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        started = time.monotonic()
        prompt = self._prompt_text(request)
        rng = self._rng(f"{request.model_id}:{prompt}")

        model_id = request.model_id or _MODELS[0].model_id
        input_tokens = max(1, self.estimate_tokens(prompt))
        output_tokens = rng.randint(40, 900)

        usage = TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)
        estimate = self.calculate_cost(usage, model_id)

        return ExecutionResult(
            execution_id=request.execution_id or str(uuid.uuid4()),
            status=ExecutionStatus.COMPLETED,
            output_text=f"[mock:{model_id}] deterministic response to {input_tokens} input tokens.",
            usage=usage,
            cost=estimate.amount,
            currency=estimate.currency,
            latency_ms=int((time.monotonic() - started) * 1000) + rng.randint(120, 2400),
            model_id=model_id,
            raw={"mock": True, "seed": prompt[:64]},
        )

    def stream(self, request: ExecutionRequest) -> Iterator[StreamChunk]:
        self.ensure_capability(Capability.SUPPORTS_STREAMING)
        result = self.execute(request)
        words = result.output_text.split(" ")
        for word in words:
            yield StreamChunk(text=word + " ")
        yield StreamChunk(text="", is_final=True, usage=result.usage)

    def get_usage(self, start: datetime, end: datetime) -> list[UsageRecord]:
        self.ensure_capability(Capability.SUPPORTS_USAGE_METRICS)
        records: list[UsageRecord] = []
        day = start
        while day < end:
            rng = self._rng(day.isoformat())
            next_day = day + timedelta(days=1)
            for model in _MODELS[:2]:
                input_tokens = rng.randint(20_000, 400_000)
                output_tokens = rng.randint(5_000, 120_000)
                usage = TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)
                records.append(
                    UsageRecord(
                        model_id=model.model_id,
                        period_start=day,
                        period_end=next_day,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        requests=rng.randint(50, 900),
                        cost=self.calculate_cost(usage, model).amount,
                    )
                )
            day = next_day
        return records
