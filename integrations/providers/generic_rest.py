"""Generic REST provider adapter.

Connects the Control Tower to any internal ML API, self-hosted inference server
or bespoke AI service that speaks JSON over HTTP -- without writing a new
adapter. Behaviour is driven entirely by provider configuration, so onboarding
an in-house model service is a data change, not a code change.

Uses stdlib urllib so the adapter layer adds no HTTP dependency.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from integrations.base.capabilities import Capability
from integrations.base.events import ExecutionStatus
from integrations.base.exceptions import ProviderUnavailable
from integrations.base.provider import BaseAIProviderAdapter
from integrations.base.types import (
    ExecutionRequest,
    ExecutionResult,
    HealthCheckResult,
    ModelInfo,
    TokenUsage,
)
from integrations.registry import register_provider

DEFAULT_TIMEOUT = 30


def dig(payload: Any, path: str, default: Any = None) -> Any:
    """Read a dotted path out of nested JSON, e.g. `choices.0.message.content`."""
    if not path:
        return default
    current = payload
    for part in path.split("."):
        if current is None:
            return default
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return default
        elif isinstance(current, dict):
            if part not in current:
                return default
            current = current[part]
        else:
            return default
    return current if current is not None else default


@register_provider("generic_rest")
class GenericRESTProviderAdapter(BaseAIProviderAdapter):
    adapter_key = "generic_rest"
    display_name = "Generic REST Endpoint"
    capabilities = frozenset(
        {
            Capability.SUPPORTS_CHAT,
            Capability.SUPPORTS_USAGE_METRICS,
            Capability.SUPPORTS_HEALTH_CHECK,
        }
    )

    # -- Configuration ------------------------------------------------------

    @property
    def timeout(self) -> int:
        return int(self.config.option("timeout_seconds", DEFAULT_TIMEOUT))

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update(self.config.option("headers", {}) or {})

        secret = self.config.resolve_credential(required=False)
        if secret:
            header_name = self.config.option("auth_header", "Authorization")
            scheme = self.config.option("auth_scheme", "Bearer")
            headers[header_name] = f"{scheme} {secret}".strip()
        return headers

    def _post(self, url: str, body: dict) -> dict:
        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), headers=self._headers(), method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raise ProviderUnavailable(f"{url} returned HTTP {exc.code}: {exc.reason}") from exc
        except Exception as exc:
            raise ProviderUnavailable(f"Request to {url} failed: {exc}") from exc

    def _get(self, url: str) -> Any:
        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raise ProviderUnavailable(f"{url} returned HTTP {exc.code}: {exc.reason}") from exc
        except Exception as exc:
            raise ProviderUnavailable(f"Request to {url} failed: {exc}") from exc

    # -- Provider surface ---------------------------------------------------

    def list_models(self) -> list[ModelInfo]:
        """Models come from configuration, or from a discovery endpoint."""
        configured = self.config.option("models")
        if configured:
            return [
                ModelInfo(
                    model_id=entry["model_id"],
                    name=entry.get("name", entry["model_id"]),
                    family=entry.get("family", ""),
                    model_type=entry.get("model_type", "chat"),
                    context_window=entry.get("context_window"),
                    input_cost_per_1k=Decimal(str(entry["input_cost_per_1k"]))
                    if entry.get("input_cost_per_1k") is not None
                    else None,
                    output_cost_per_1k=Decimal(str(entry["output_cost_per_1k"]))
                    if entry.get("output_cost_per_1k") is not None
                    else None,
                )
                for entry in configured
            ]

        models_path = self.config.option("models_path")
        if not models_path:
            return []

        payload = self._get(f"{self.config.endpoint.rstrip('/')}/{models_path.lstrip('/')}")
        items = dig(payload, self.config.option("models_list_path", "data"), []) or []
        id_field = self.config.option("model_id_field", "id")
        return [
            ModelInfo(model_id=str(item.get(id_field)), name=str(item.get(id_field)))
            for item in items
            if isinstance(item, dict) and item.get(id_field)
        ]

    def health_check(self) -> HealthCheckResult:
        endpoint = self.config.endpoint
        if not endpoint:
            return HealthCheckResult(
                healthy=False,
                status="unhealthy",
                message="No endpoint configured.",
                checked_at=datetime.now(timezone.utc),
            )

        started = time.monotonic()
        health_path = self.config.option("health_path", "health")
        try:
            self._get(f"{endpoint.rstrip('/')}/{str(health_path).lstrip('/')}")
        except ProviderUnavailable as exc:
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

        completion_path = self.config.option("completion_path", "v1/chat/completions")
        url = f"{self.config.endpoint.rstrip('/')}/{str(completion_path).lstrip('/')}"

        body: dict[str, Any] = {
            "model": request.model_id,
            "messages": request.messages or [{"role": "user", "content": request.prompt}],
        }
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        body.update(self.config.option("extra_body", {}) or {})

        try:
            payload = self._post(url, body)
        except ProviderUnavailable as exc:
            return ExecutionResult(
                execution_id=execution_id,
                status=ExecutionStatus.FAILED,
                model_id=request.model_id,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_message=str(exc)[:1000],
            )

        text = dig(payload, self.config.option("response_text_path", "choices.0.message.content"), "")
        usage = TokenUsage(
            input_tokens=int(dig(payload, self.config.option("input_tokens_path", "usage.prompt_tokens"), 0) or 0),
            output_tokens=int(dig(payload, self.config.option("output_tokens_path", "usage.completion_tokens"), 0) or 0),
        )
        estimate = self.calculate_cost(usage, request.model_id)

        return ExecutionResult(
            execution_id=execution_id,
            status=ExecutionStatus.COMPLETED,
            output_text=str(text or ""),
            usage=usage,
            cost=estimate.amount,
            currency=estimate.currency,
            latency_ms=int((time.monotonic() - started) * 1000),
            model_id=request.model_id,
            raw=payload if self.config.option("store_raw_response", False) else {},
        )
