"""Mock execution generator (section 44).

Produces realistic execution history so monitoring, timelines and the live
dashboard are demonstrable without a real engine. It drives the registered
MockAgentAdapter rather than fabricating rows directly, so the same
normalization path that real telemetry takes is exercised end to end.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.agents.models import AIAgent
from apps.executions.models import ApprovalState, Execution, ExecutionStatusChoices
from apps.executions.services import ExecutionService
from integrations.base.events import EventType, NormalizedEvent
from integrations.base.types import ExecutionRequest
from integrations.registry import registry

TERMINAL_EVENT_TYPES = {EventType.EXECUTION_COMPLETED, EventType.EXECUTION_CANCELLED}

#: Status mix roughly matching a healthy production estate.
STATUS_WEIGHTS = [
    (ExecutionStatusChoices.COMPLETED, 86),
    (ExecutionStatusChoices.FAILED, 6),
    (ExecutionStatusChoices.WAITING_FOR_APPROVAL, 3),
    (ExecutionStatusChoices.RUNNING, 2),
    (ExecutionStatusChoices.CANCELLED, 2),
    (ExecutionStatusChoices.TIMED_OUT, 1),
]

ERRORS = [
    "Upstream provider returned HTTP 429 (rate limited).",
    "Model request exceeded the 60s timeout.",
    "Tool call 'crm_lookup' failed: connection refused.",
    "Context window exceeded for the requested prompt.",
    "Provider returned a malformed response payload.",
]

#: Requests cluster in the working day rather than spreading evenly.
HOUR_WEIGHTS = [1, 1, 1, 1, 1, 2, 4, 8, 14, 18, 20, 19, 16, 18, 20, 19, 15, 10, 6, 4, 3, 2, 2, 1]

LIVE_STATUSES = frozenset(
    {ExecutionStatusChoices.RUNNING, ExecutionStatusChoices.WAITING_FOR_APPROVAL}
)


@dataclass
class GenerationResult:
    executions: int = 0
    events: int = 0


class MockExecutionGenerator:
    def __init__(self, organization, days: int = 14, seed: int | None = 20260909):
        self.organization = organization
        self.days = days
        self.random = random.Random(seed)
        self.agents = list(
            AIAgent.objects.for_organization(organization).select_related(
                "primary_model", "provider"
            ).prefetch_related("applications")
        )

    def _timestamp(self):
        """A start time inside the window, weighted toward business hours."""
        now = timezone.now()
        # Recent days carry more traffic, which makes trend charts legible.
        day_offset = int(abs(self.random.gauss(0, self.days / 2.5))) % max(self.days, 1)
        hour = self.random.choices(range(24), weights=HOUR_WEIGHTS, k=1)[0]

        stamp = (now - timedelta(days=day_offset)).replace(
            hour=hour,
            minute=self.random.randint(0, 59),
            second=self.random.randint(0, 59),
            microsecond=0,
        )
        # Today's busy hours may still be ahead: no run started in the future.
        return stamp if stamp <= now else stamp - timedelta(days=1)

    def _status(self):
        statuses, weights = zip(*STATUS_WEIGHTS)
        return self.random.choices(statuses, weights=weights, k=1)[0]

    def _start_time(self, status):
        """A run still in flight must have started minutes ago, not days ago.

        Dating live runs recently (rather than downgrading them to completed)
        keeps the live monitor populated without misrepresenting anything.
        """
        if status in LIVE_STATUSES:
            return timezone.now() - timedelta(
                minutes=self.random.randint(0, 25), seconds=self.random.randint(0, 59)
            )
        return self._timestamp()

    def _shape_events(self, events, status, error_message: str):
        """Make the timeline agree with the run's outcome.

        The mock adapter always finishes cleanly. A run that failed, was
        cancelled, or is still going must not carry an "Execution Completed"
        event -- and for a live run, a terminal event would close it out on
        ingest (see ExecutionService.recompute_from_events).
        """
        if status == ExecutionStatusChoices.COMPLETED:
            return events

        trimmed = [event for event in events if event.event_type not in TERMINAL_EVENT_TYPES]
        if not trimmed:
            return trimmed

        last = trimmed[-1]

        def append(event_type, **kwargs):
            trimmed.append(
                NormalizedEvent(
                    event_type=event_type,
                    execution_id=last.execution_id,
                    trace_id=last.trace_id,
                    timestamp=last.timestamp,
                    sequence=last.sequence + 1,
                    **kwargs,
                )
            )

        if status in {ExecutionStatusChoices.FAILED, ExecutionStatusChoices.TIMED_OUT}:
            append(EventType.ERROR, status=str(status), error_message=error_message)
        elif status == ExecutionStatusChoices.CANCELLED:
            append(EventType.EXECUTION_CANCELLED, status=str(status))

        return trimmed

    def generate(self, count: int = 500) -> GenerationResult:
        result = GenerationResult()
        if not self.agents:
            return result

        adapter = registry.get_agent("mock")

        for index in range(count):
            agent = self.random.choice(self.agents)
            status = self._status()
            started_at = self._start_time(status)
            external_id = f"exec-{self.organization.slug[:12]}-{index:05d}"

            handle = adapter.create_execution(
                ExecutionRequest(
                    execution_id=external_id,
                    model_id=agent.primary_model.model_id if agent.primary_model else "",
                    trace_id=f"trace-{index // 3:05d}",
                    metadata={
                        "requires_approval": status
                        == ExecutionStatusChoices.WAITING_FOR_APPROVAL,
                        # The mock engine calls the tools the agent declares.
                        "tools": list(agent.allowed_tools or []),
                    },
                )
            )
            events = adapter.get_events(handle.execution_id)

            # The adapter timestamps runs at "now"; shift the whole timeline
            # onto the intended point in history.
            if events:
                shift = started_at - events[0].timestamp
                for event in events:
                    event.timestamp = event.timestamp + shift

            usage = adapter.get_usage(handle.execution_id)
            latency = self.random.randint(400, 22_000)
            is_terminal = status not in LIVE_STATUSES

            application = next(iter(agent.applications.all()), None)
            fields = {
                "agent": str(agent.pk),
                # The run belongs to the application the agent serves, when it serves one.
                **({"application": str(application.pk)} if application else {}),
                "trace_id": f"trace-{index // 3:05d}",
                "status": status,
                "started_at": started_at,
                "environment": agent.environment,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            }

            error_message = ""
            if is_terminal:
                fields["ended_at"] = started_at + timedelta(milliseconds=latency)
                fields["latency_ms"] = latency
            if status == ExecutionStatusChoices.FAILED:
                error_message = self.random.choice(ERRORS)
            if status == ExecutionStatusChoices.TIMED_OUT:
                error_message = "Execution exceeded the configured time limit."
            if error_message:
                fields["error_message"] = error_message
            if status == ExecutionStatusChoices.WAITING_FOR_APPROVAL:
                fields["approval_required"] = True
                fields["approval_status"] = ApprovalState.PENDING

            events = self._shape_events(events, status, error_message)

            # Around a third of engines report their own cost; the rest are
            # priced by the Control Tower from the model registry.
            if self.random.random() < 0.3 and agent.primary_model:
                fields["cost"] = agent.primary_model.estimate_cost(
                    usage.input_tokens, usage.output_tokens
                ) * Decimal("1.02")

            execution = ExecutionService.record(
                organization=self.organization,
                external_id=external_id,
                source_adapter="mock",
                **fields,
            )
            stored = ExecutionService.append_events(execution, events)

            result.executions += 1
            result.events += len(stored)

        adapter._runs.clear()
        return result


def purge(organization) -> int:
    """Remove generated executions for an organization. Returns the count."""
    queryset = Execution.objects.for_organization(organization).filter(source_adapter="mock")
    count = queryset.count()
    queryset.delete()
    return count
