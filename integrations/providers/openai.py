"""OpenAI provider adapter.

The `openai` SDK is imported lazily inside `_client()`. That is deliberate: the
Control Tower must import, migrate and run with no vendor SDK installed, and a
missing dependency should surface as a clear configuration error on the one
provider that needs it -- not an ImportError at startup.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone

from integrations.base.capabilities import Capability
from integrations.base.events import ExecutionStatus
from integrations.base.exceptions import AdapterDependencyMissing, ProviderUnavailable
from integrations.base.provider import BaseAIProviderAdapter
from integrations.base.types import (
    ExecutionRequest,
    ExecutionResult,
    HealthCheckResult,
    ModelInfo,
    StreamChunk,
    TokenUsage,
)
from integrations.registry import register_provider


@register_provider("openai")
class OpenAIProviderAdapter(BaseAIProviderAdapter):
    adapter_key = "openai"
    display_name = "OpenAI"
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
        }
    )

    def _client(self):
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise AdapterDependencyMissing(
                "The 'openai' package is required for the OpenAI adapter. "
                "Install it with: pip install openai"
            ) from exc

        kwargs = {"api_key": self.config.resolve_credential()}
        if self.config.endpoint:
            kwargs["base_url"] = self.config.endpoint
        return OpenAI(**kwargs)

    def _messages(self, request: ExecutionRequest) -> list[dict]:
        if request.messages:
            return request.messages
        return [{"role": "user", "content": request.prompt}]

    def list_models(self) -> list[ModelInfo]:
        try:
            response = self._client().models.list()
        except AdapterDependencyMissing:
            raise
        except Exception as exc:
            raise ProviderUnavailable(f"Could not list OpenAI models: {exc}") from exc

        # Pricing intentionally omitted: rates come from the Control Tower's
        # model registry, which is the single source of truth for cost.
        return [
            ModelInfo(
                model_id=model.id,
                name=model.id,
                family=model.id.split("-")[0],
                model_type="chat",
                metadata={"owned_by": getattr(model, "owned_by", "")},
            )
            for model in response.data
        ]

    def health_check(self) -> HealthCheckResult:
        started = time.monotonic()
        try:
            self._client().models.list()
        except Exception as exc:
            return HealthCheckResult(
                healthy=False,
                status="unhealthy",
                message=str(exc)[:500],
                checked_at=datetime.now(timezone.utc),
            )
        return HealthCheckResult(
            healthy=True,
            status="healthy",
            latency_ms=int((time.monotonic() - started) * 1000),
            checked_at=datetime.now(timezone.utc),
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        started = time.monotonic()
        execution_id = request.execution_id or str(uuid.uuid4())

        payload: dict = {"model": request.model_id, "messages": self._messages(request)}
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tools:
            payload["tools"] = request.tools

        try:
            response = self._client().chat.completions.create(**payload)
        except AdapterDependencyMissing:
            raise
        except Exception as exc:
            return ExecutionResult(
                execution_id=execution_id,
                status=ExecutionStatus.FAILED,
                model_id=request.model_id,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_message=str(exc)[:1000],
            )

        usage = TokenUsage(
            input_tokens=getattr(response.usage, "prompt_tokens", 0) if response.usage else 0,
            output_tokens=getattr(response.usage, "completion_tokens", 0) if response.usage else 0,
        )
        return ExecutionResult(
            execution_id=execution_id,
            status=ExecutionStatus.COMPLETED,
            output_text=response.choices[0].message.content or "",
            usage=usage,
            latency_ms=int((time.monotonic() - started) * 1000),
            model_id=response.model,
        )

    def stream(self, request: ExecutionRequest) -> Iterator[StreamChunk]:
        self.ensure_capability(Capability.SUPPORTS_STREAMING)

        payload: dict = {
            "model": request.model_id,
            "messages": self._messages(request),
            "stream": True,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        for chunk in self._client().chat.completions.create(**payload):
            if not chunk.choices:
                continue
            text = chunk.choices[0].delta.content
            if text:
                yield StreamChunk(text=text)
        yield StreamChunk(text="", is_final=True)
