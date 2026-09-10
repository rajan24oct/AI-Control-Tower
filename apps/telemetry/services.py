"""Telemetry ingestion (section 14).

The pipeline is deliberately adapter-driven: an engine's payload shape is a
configuration concern, not a code concern. Onboarding a new agent platform
means picking an execution adapter, not editing this module.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction

from apps.audit.models import AuditAction
from apps.audit.services import AuditService
from apps.executions.models import Execution
from apps.executions.services import ExecutionService
from apps.telemetry.models import IngestionStatus, RawTelemetryPayload
from integrations.base.config import AdapterConfig
from integrations.base.exceptions import AdapterError
from integrations.registry import registry

logger = logging.getLogger(__name__)

DEFAULT_ADAPTER = "generic_webhook"


class TelemetryIngestionError(Exception):
    """The payload could not be turned into an execution."""


class TelemetryService:
    @staticmethod
    def _adapter(adapter_key: str, options: dict | None = None):
        key = adapter_key or DEFAULT_ADAPTER
        return registry.get_execution(key, AdapterConfig(key=key, options=options or {}))

    @classmethod
    def ingest(
        cls,
        *,
        organization,
        payload: dict[str, Any],
        adapter_key: str = DEFAULT_ADAPTER,
        service_account=None,
        endpoint: str = "",
        adapter_options: dict | None = None,
    ) -> Execution:
        """Normalize an engine payload into an execution plus its timeline.

        The raw payload is stored outside the transaction that writes the
        execution. That is deliberate: a rejected payload must survive, and
        wrapping the whole method in a transaction would roll away the very
        evidence needed to diagnose the rejection.
        """
        raw = RawTelemetryPayload.objects.create(
            organization=organization,
            payload=payload,
            adapter_key=adapter_key,
            endpoint=endpoint,
            service_account=service_account,
        )

        try:
            adapter = cls._adapter(adapter_key, adapter_options)
            normalized = adapter.normalize_execution(payload)
            events = adapter.normalize_events(payload)
        except AdapterError as exc:
            cls._reject(raw, f"Adapter '{adapter_key}' could not normalize the payload: {exc}")
            raise TelemetryIngestionError(str(exc)) from exc
        except Exception as exc:
            logger.exception("Telemetry normalization failed for payload %s", raw.pk)
            cls._reject(raw, f"{type(exc).__name__}: {exc}")
            raise TelemetryIngestionError(str(exc)) from exc

        # The execution and its events land together or not at all.
        with transaction.atomic():
            execution = ExecutionService.record(
                organization=organization,
                external_id=normalized.pop("external_id", ""),
                service_account=service_account,
                source_adapter=adapter_key,
                **normalized,
            )
            stored_events = ExecutionService.append_events(execution, events)

            raw.execution = execution
            raw.status = IngestionStatus.PROCESSED
            raw.event_count = len(stored_events)
            raw.save(update_fields=["execution", "status", "event_count", "updated_at"])

        AuditService.log(
            action=AuditAction.TELEMETRY_INGEST,
            resource_type=Execution._meta.label,
            resource_id=execution.pk,
            resource_label=execution.external_id,
            organization=organization,
            metadata={
                "adapter": adapter_key,
                "events": len(stored_events),
                "service_account": getattr(service_account, "name", ""),
            },
        )
        return execution

    @staticmethod
    def _reject(raw: RawTelemetryPayload, message: str) -> None:
        raw.status = IngestionStatus.REJECTED
        raw.processing_error = message[:2000]
        raw.save(update_fields=["status", "processing_error", "updated_at"])

    @classmethod
    def replay(cls, raw: RawTelemetryPayload) -> Execution:
        """Re-run a stored payload through the current normalization logic."""
        return cls.ingest(
            organization=raw.organization,
            payload=raw.payload,
            adapter_key=raw.adapter_key or DEFAULT_ADAPTER,
            service_account=raw.service_account,
            endpoint=raw.endpoint,
        )
