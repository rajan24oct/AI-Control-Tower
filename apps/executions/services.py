"""Execution domain services (section 23)."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Iterable

from django.db import transaction
from django.db.models import Max, Q, Sum
from django.utils import timezone

from apps.agents.models import AIAgent
from apps.applications_registry.models import AIApplication
from apps.executions.models import (
    ApprovalState,
    Execution,
    ExecutionEvent,
    ExecutionStatusChoices,
    ExecutionTrace,
)
from apps.models_registry.models import AIModel
from apps.providers.models import AIProvider
from integrations.base.events import EventType, ExecutionStatus, NormalizedEvent

logger = logging.getLogger(__name__)

#: Events that imply a terminal execution state, used to close runs out when an
#: engine reports a final event but never calls the complete/fail endpoints.
TERMINAL_EVENTS = {
    EventType.EXECUTION_COMPLETED: ExecutionStatusChoices.COMPLETED,
    EventType.EXECUTION_CANCELLED: ExecutionStatusChoices.CANCELLED,
}

#: Event names the Control Tower recognizes. Anything else is dropped rather
#: than guessed at -- the raw payload is retained by the telemetry layer.
VALID_EVENT_TYPES = frozenset(str(member) for member in EventType)


class AssetResolver:
    """Maps the loose identifiers engines send onto registry records.

    An engine reports "customer-support-agent", not a Control Tower UUID, so
    every lookup accepts a UUID, a slug/business id, or a display name -- and
    is always confined to one organization.
    """

    def __init__(self, organization):
        self.organization = organization

    def _lookup(self, model_class, value, *fields):
        if not value:
            return None

        queryset = model_class.objects.for_organization(self.organization)

        try:
            return queryset.filter(pk=uuid.UUID(str(value))).first()
        except (ValueError, AttributeError, TypeError):
            pass

        condition = Q()
        for field in fields:
            condition |= Q(**{field: value})
        return queryset.filter(condition).first()

    def agent(self, value):
        return self._lookup(AIAgent, value, "agent_id", "name")

    def application(self, value):
        return self._lookup(AIApplication, value, "slug", "name")

    def provider(self, value):
        return self._lookup(AIProvider, value, "slug", "name", "adapter_key")

    def model(self, value):
        return self._lookup(AIModel, value, "model_id", "model_name")


class ExecutionService:
    """Creates and advances executions from normalized telemetry."""

    @staticmethod
    def _trace(organization, trace_id: str) -> ExecutionTrace | None:
        if not trace_id:
            return None
        trace, _ = ExecutionTrace.objects.get_or_create(
            organization=organization, trace_id=trace_id
        )
        return trace

    @classmethod
    @transaction.atomic
    def record(
        cls,
        *,
        organization,
        external_id: str = "",
        service_account=None,
        source_adapter: str = "",
        **fields: Any,
    ) -> Execution:
        """Create or update an execution.

        Idempotent on `(organization, external_id)` so an engine can safely
        re-send the same run -- retries and at-least-once queues are the norm,
        not the exception.
        """
        external_id = str(external_id or uuid.uuid4())
        resolver = AssetResolver(organization)

        agent = resolver.agent(fields.pop("agent", None))
        application = resolver.application(fields.pop("application", None))
        provider = resolver.provider(fields.pop("provider", None))
        model = resolver.model(fields.pop("model", None))

        # Fill in what the engine did not say from what the registry knows.
        if agent:
            application = application or agent.applications.first()
            provider = provider or agent.provider
            model = model or agent.primary_model

        if model and not provider:
            provider = model.provider

        trace_id = str(fields.pop("trace_id", "") or "")
        defaults: dict[str, Any] = {
            "agent": agent,
            "application": application,
            "provider": provider,
            "model": model,
            "trace": cls._trace(organization, trace_id),
            "service_account": service_account,
            "source_adapter": source_adapter,
        }

        if agent:
            defaults["framework"] = agent.framework
            defaults["environment"] = agent.environment
            defaults["approval_required"] = agent.approval_required

        allowed = {
            "framework",
            "user",
            "started_at",
            "ended_at",
            "latency_ms",
            "status",
            "environment",
            "input_tokens",
            "output_tokens",
            "cost",
            "currency",
            "error_message",
            "approval_required",
            "approval_status",
            "metadata",
        }
        defaults.update({k: v for k, v in fields.items() if k in allowed and v is not None})

        # `approval_required` is policy; `approval_status` is the state of an
        # actual decision. A finished run must never be left showing "Pending"
        # -- that state is reached only by pausing for approval, or by an
        # APPROVAL_REQUESTED event (see recompute_from_events).
        if (
            defaults.get("approval_required")
            and "approval_status" not in defaults
            and defaults.get("status") == ExecutionStatusChoices.WAITING_FOR_APPROVAL
        ):
            defaults["approval_status"] = ApprovalState.PENDING

        previous_status = (
            Execution.objects.filter(organization=organization, external_id=external_id)
            .values_list("status", flat=True)
            .first()
        )

        execution, created = Execution.objects.update_or_create(
            organization=organization, external_id=external_id, defaults=defaults
        )
        cls.apply_pricing(execution)
        cls.broadcast(execution, previous_status)
        return execution

    @staticmethod
    def broadcast(execution, previous_status: str | None = None) -> None:
        """Tell the live monitor, but only when the live board is affected.

        Most telemetry describes runs that were already finished when they
        arrived; rendering a row for those would cost far more than it tells
        anyone.
        """
        from apps.executions.broadcast import execution_changed
        from apps.executions.views import LIVE_STATUSES

        was_live = previous_status in LIVE_STATUSES if previous_status else False
        if execution.status in LIVE_STATUSES or was_live:
            execution_changed(execution)

    @staticmethod
    def apply_pricing(execution: Execution) -> Execution:
        """Price a run when the engine reported no cost.

        Rates come from the pricing registry, resolved against the execution's
        own start time so historic runs keep the rates that applied then
        (section 15). Imported lazily: the costs app depends on this module.
        """
        if execution.cost and execution.cost > 0:
            return execution
        if not execution.model or not execution.total_tokens:
            return execution

        from apps.costs.services import CostCalculationService

        breakdown = CostCalculationService.calculate(
            execution.model,
            execution.input_tokens,
            execution.output_tokens,
            at=execution.started_at,
        )
        if not breakdown.amount:
            return execution

        execution.cost = breakdown.amount
        execution.currency = breakdown.currency
        execution.cost_is_derived = True
        execution.metadata = {**(execution.metadata or {}), "pricing_source": breakdown.source}
        execution.save(
            update_fields=["cost", "currency", "cost_is_derived", "metadata", "updated_at"]
        )
        return execution

    @classmethod
    @transaction.atomic
    def append_events(
        cls, execution: Execution, events: Iterable[NormalizedEvent | dict]
    ) -> list[ExecutionEvent]:
        """Persist normalized events onto an execution's timeline."""
        next_sequence = (
            execution.events.aggregate(top=Max("sequence"))["top"] or -1
        ) + 1

        created: list[ExecutionEvent] = []
        for offset, raw in enumerate(events):
            payload = raw.to_dict() if isinstance(raw, NormalizedEvent) else dict(raw)

            event_type = str(payload.get("event_type") or payload.get("type") or "").upper()
            if event_type not in VALID_EVENT_TYPES:
                logger.debug("Dropping unrecognized event type %r", event_type)
                continue

            sequence = payload.get("sequence")
            sequence = next_sequence + offset if sequence is None else int(sequence)

            event_id = payload.get("event_id")
            try:
                event_pk = uuid.UUID(str(event_id)) if event_id else uuid.uuid4()
            except (ValueError, TypeError):
                event_pk = uuid.uuid4()

            event, _ = ExecutionEvent.objects.update_or_create(
                pk=event_pk,
                defaults={
                    "execution": execution,
                    "organization": execution.organization,
                    "event_type": event_type,
                    "timestamp": payload.get("timestamp") or timezone.now(),
                    "sequence": sequence,
                    "duration_ms": payload.get("duration_ms"),
                    "input_tokens": int(payload.get("input_tokens") or 0),
                    "output_tokens": int(payload.get("output_tokens") or 0),
                    "cost": Decimal(str(payload.get("cost") or "0")),
                    "model_id": str(payload.get("model_id") or "")[:200],
                    "status": str(payload.get("status") or "")[:32],
                    "error_message": str(payload.get("error_message") or ""),
                    "trace_id": str(payload.get("trace_id") or execution.trace_key or "")[:200],
                    "metadata": payload.get("metadata") or {},
                },
            )
            created.append(event)

        if created:
            cls.recompute_from_events(execution)
        return created

    @classmethod
    def recompute_from_events(cls, execution: Execution) -> Execution:
        """Roll event-level usage up onto the execution.

        Only applied when the engine has not already reported totals, so a
        summary payload is never overwritten by partial step data.
        """
        totals = execution.events.aggregate(
            input_tokens=Sum("input_tokens"),
            output_tokens=Sum("output_tokens"),
            cost=Sum("cost"),
        )
        changed = []

        if not execution.input_tokens and totals["input_tokens"]:
            execution.input_tokens = totals["input_tokens"]
            changed.append("input_tokens")
        if not execution.output_tokens and totals["output_tokens"]:
            execution.output_tokens = totals["output_tokens"]
            changed.append("output_tokens")
        if not execution.cost and totals["cost"]:
            execution.cost = totals["cost"]
            changed.append("cost")

        # A terminal event closes the run out even if the engine never called
        # the complete/fail endpoints.
        latest_terminal = (
            execution.events.filter(event_type__in=[str(e) for e in TERMINAL_EVENTS])
            .order_by("-sequence")
            .first()
        )
        if latest_terminal and execution.is_live:
            execution.status = TERMINAL_EVENTS[EventType(latest_terminal.event_type)]
            execution.ended_at = execution.ended_at or latest_terminal.timestamp
            changed += ["status", "ended_at"]

        if execution.events.filter(event_type=EventType.APPROVAL_REQUESTED).exists():
            if execution.approval_status == ApprovalState.NOT_REQUIRED:
                execution.approval_required = True
                execution.approval_status = ApprovalState.PENDING
                changed += ["approval_required", "approval_status"]

        if changed:
            was_live = execution.is_live
            execution.total_tokens = execution.input_tokens + execution.output_tokens
            execution.save(update_fields=[*set(changed), "total_tokens", "updated_at"])
            cls.apply_pricing(execution)
            # Token and cost totals move as events arrive; a watcher wants to
            # see that without waiting for the run to finish.
            cls.broadcast(execution, ExecutionStatusChoices.RUNNING if was_live else None)

        return execution

    # -- Lifecycle transitions ---------------------------------------------

    @classmethod
    def complete(cls, execution: Execution, **fields) -> Execution:
        return cls._finish(execution, ExecutionStatusChoices.COMPLETED, **fields)

    @classmethod
    def fail(cls, execution: Execution, error_message: str = "", **fields) -> Execution:
        return cls._finish(
            execution, ExecutionStatusChoices.FAILED, error_message=error_message, **fields
        )

    @classmethod
    def cancel(cls, execution: Execution, reason: str = "", **fields) -> Execution:
        return cls._finish(
            execution, ExecutionStatusChoices.CANCELLED, error_message=reason, **fields
        )

    @classmethod
    def _finish(cls, execution: Execution, status: str, **fields) -> Execution:
        previous_status = execution.status
        execution.status = status
        execution.ended_at = fields.get("ended_at") or timezone.now()

        # An engine that measured its own latency is more accurate than the wall
        # clock between two HTTP calls, so a reported value always wins.
        if fields.get("latency_ms") is not None:
            execution.latency_ms = fields["latency_ms"]
        elif execution.latency_ms is None and execution.started_at:
            execution.latency_ms = max(
                0, int((execution.ended_at - execution.started_at).total_seconds() * 1000)
            )

        for field in ("input_tokens", "output_tokens", "cost", "currency", "error_message"):
            value = fields.get(field)
            if value is not None:
                setattr(execution, field, value)

        if status != ExecutionStatusChoices.WAITING_FOR_APPROVAL:
            if execution.approval_status == ApprovalState.PENDING:
                execution.approval_status = ApprovalState.CANCELLED

        execution.total_tokens = execution.input_tokens + execution.output_tokens
        execution.save()
        cls.apply_pricing(execution)
        cls.broadcast(execution, previous_status)
        return execution
