"""Generic webhook execution adapter.

Accepts telemetry from any external engine and maps it onto the normalized
execution/event model using a configurable field mapping. An engine running in
Kubernetes, a Lambda, or on a laptop can report into the Control Tower without
either side writing bespoke code.

Optional HMAC-SHA256 signature verification guards the ingestion endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from integrations.base.events import EventType, NormalizedEvent
from integrations.base.execution import BaseExecutionAdapter
from integrations.providers.generic_rest import dig
from integrations.registry import register_execution

#: Maps normalized field name -> path in the incoming payload. Defaults match
#: the Control Tower's own API shape. The asset keys (`agent`, `application`,
#: `provider`, `model`) carry loose identifiers, which the execution service
#: resolves against the registry.
DEFAULT_MAPPING = {
    "external_id": "execution_id",
    "status": "status",
    "model": "model",
    "agent": "agent",
    "application": "application",
    "provider": "provider",
    "trace_id": "trace_id",
    "input_tokens": "usage.input_tokens",
    "output_tokens": "usage.output_tokens",
    "cost": "usage.cost",
    "latency_ms": "latency_ms",
    "error_message": "error",
}


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        return Decimal("0")


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


@register_execution("generic_webhook")
class GenericWebhookExecutionAdapter(BaseExecutionAdapter):
    adapter_key = "generic_webhook"
    display_name = "Generic Webhook"

    @property
    def mapping(self) -> dict[str, str]:
        return {**DEFAULT_MAPPING, **(self.config.option("field_mapping", {}) or {})}

    def verify_signature(self, headers: dict[str, str], raw_body: bytes) -> bool:
        """Constant-time HMAC check. Returns True when no secret is configured."""
        secret = self.config.resolve_credential(required=False)
        if not secret:
            return True

        header_name = self.config.option("signature_header", "X-Signature")
        provided = headers.get(header_name) or headers.get(header_name.lower(), "")
        if not provided:
            return False

        provided = provided.removeprefix("sha256=")
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, provided)

    def normalize_execution(self, payload: dict[str, Any]) -> dict[str, Any]:
        mapping = self.mapping
        return {
            "external_id": str(dig(payload, mapping["external_id"], "") or ""),
            "status": self.map_status(str(dig(payload, mapping["status"], "") or "")),
            "model": dig(payload, mapping["model"]),
            "agent": dig(payload, mapping["agent"]),
            "application": dig(payload, mapping["application"]),
            "provider": dig(payload, mapping["provider"]),
            "trace_id": dig(payload, mapping["trace_id"]),
            "input_tokens": _to_int(dig(payload, mapping["input_tokens"], 0)),
            "output_tokens": _to_int(dig(payload, mapping["output_tokens"], 0)),
            "cost": _to_decimal(dig(payload, mapping["cost"], 0)),
            "latency_ms": _to_int(dig(payload, mapping["latency_ms"], 0)),
            "error_message": str(dig(payload, mapping["error_message"], "") or ""),
            "metadata": dig(payload, self.config.option("metadata_path", "metadata"), {}) or {},
        }

    def normalize_events(self, payload: dict[str, Any]) -> list[NormalizedEvent]:
        execution_id = str(dig(payload, self.mapping["external_id"], "") or "")
        raw_events = dig(payload, self.config.option("events_path", "events"), []) or []

        events: list[NormalizedEvent] = []
        for index, raw in enumerate(raw_events):
            if not isinstance(raw, dict):
                continue

            event_type = self._map_event_type(str(raw.get("type") or raw.get("event_type") or ""))
            if event_type is None:
                continue

            usage = raw.get("usage") or {}
            events.append(
                NormalizedEvent(
                    event_type=event_type,
                    execution_id=execution_id,
                    timestamp=_parse_timestamp(raw.get("timestamp")),
                    duration_ms=raw.get("duration_ms"),
                    input_tokens=_to_int(usage.get("input_tokens", raw.get("input_tokens", 0))),
                    output_tokens=_to_int(usage.get("output_tokens", raw.get("output_tokens", 0))),
                    cost=_to_decimal(raw.get("cost", 0)),
                    status=raw.get("status"),
                    metadata=raw.get("metadata") or {},
                    error_message=str(raw.get("error") or raw.get("error_message") or ""),
                    trace_id=raw.get("trace_id") or dig(payload, self.mapping["trace_id"]),
                    parent_event_id=raw.get("parent_event_id"),
                    sequence=raw.get("sequence", index),
                )
            )
        return events

    def _map_event_type(self, raw_type: str) -> EventType | None:
        """Translate an engine's event name into the normalized vocabulary."""
        if not raw_type:
            return None

        overrides = self.config.option("event_type_map", {}) or {}
        candidate = overrides.get(raw_type, raw_type)

        try:
            return EventType(candidate.strip().upper())
        except ValueError:
            # Unknown event names are dropped rather than guessed at; the raw
            # payload is still retained by the telemetry layer.
            return None
