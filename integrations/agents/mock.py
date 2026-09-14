"""Mock agent adapter.

Simulates a multi-step agent run -- reasoning steps, tool calls, an optional
human approval pause -- and reports it as normalized events. This is what lets
execution monitoring, the timeline view and the approval flow be demonstrated
without standing up LangGraph, CrewAI or any real engine.
"""

from __future__ import annotations

import hashlib
import random
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from integrations.base.agent import BaseAgentAdapter
from integrations.base.capabilities import Capability
from integrations.base.events import EventType, ExecutionStatus, NormalizedEvent
from integrations.base.exceptions import ExecutionNotFound
from integrations.base.types import (
    AgentExecutionHandle,
    AgentStep,
    ApprovalRequestPayload,
    ExecutionRequest,
    TokenUsage,
    ToolCall,
)
from integrations.registry import register_agent

_TOOLS = ["web_search", "internal_documents", "crm_lookup", "sql_query", "send_email"]
_STEP_NAMES = ["plan", "retrieve_context", "reason", "act", "summarize"]

#: Synthetic prompts and completions, so the monitor's prompt panel shows
#: something shaped like real traffic. Deliberately business text with no
#: personal data in it -- a demo estate should not teach anyone that putting
#: customer records through a mock engine is normal.
_EXCHANGES = [
    (
        "Summarise the outstanding underwriting exceptions on submission "
        "SUB-40182 and say whether it can be bound without referral.",
        "Three exceptions are outstanding: a missing loss run for 2024, an "
        "unconfirmed sprinkler certificate, and a TIV that exceeds the "
        "delegated limit by 12%. The first two are documentation gaps; the "
        "third requires referral. Recommendation: do not bind — refer to the "
        "property desk with the TIV breakdown attached.",
    ),
    (
        "A customer asks why their renewal premium rose 18%. Draft a reply "
        "using the rating factors on policy POL-88213.",
        "The increase is driven by three factors: a 9% adjustment for claims "
        "experience following the 2025 escape-of-water claim, a 6% portfolio "
        "rate change applied across the property book, and a 3% increase in "
        "declared building value. Draft reply prepared for adviser review — "
        "not sent, as outbound customer mail requires approval.",
    ),
    (
        "Check whether the attached vendor contract contains a clause "
        "permitting sub-processing of personal data outside the EEA.",
        "Clause 14.3 permits sub-processing subject to prior written notice, "
        "but does not restrict the location of sub-processors. There is no "
        "standard contractual clauses annex. This is a gap against the data "
        "protection addendum template — flagging for legal review rather than "
        "auto-approving.",
    ),
    (
        "Reconcile the September ledger against the settlement file and list "
        "any entries that do not match.",
        "Reconciled 4,182 of 4,186 entries. Four mismatches, all in the same "
        "batch: three are a rounding difference of £0.01 arising from a "
        "currency conversion, one is a genuine duplicate settlement of "
        "£2,140.00. Recommend reversing the duplicate and accepting the "
        "rounding variance.",
    ),
    (
        "What is the current exposure to the Northwind Industrial account "
        "across all lines, and is it within appetite?",
        "Total exposure is £14.2m across property (£9.1m), liability (£3.6m) "
        "and motor fleet (£1.5m). Appetite for a single industrial account is "
        "£12m, so the account is £2.2m over. The excess sits entirely in the "
        "property line following the June endorsement.",
    ),
    # Two runs that carry identifiers, so the monitor's personal-data
    # highlighting has something to find in a seeded estate. The values are
    # reserved-for-testing ones -- example.com, Ofcom's drama range, the
    # standard Visa test card -- and match nobody.
    (
        "Customer j.okafor@example.com called on 07700 900461 about a claim "
        "on policy POL-88213. Draft a callback note for the claims handler.",
        "Callback note prepared. Caller verified against the policy record; "
        "claim reference CLM-20114 opened for storm damage reported on "
        "14 August. No settlement figure discussed. Handler to confirm the "
        "excess of £350 before the next call.",
    ),
    (
        "The customer says their premium was taken twice from card "
        "4111 1111 1111 1111. Confirm from the settlement file and advise.",
        "Confirmed: two collections of £412.60 on 2 September against the same "
        "mandate, the second with no matching schedule entry. This is a "
        "duplicate. Recommend refunding the second collection and writing to "
        "the customer at 12 Wharf Road, Bristol BS1 4QX.",
    ),
]


@register_agent("mock")
class MockAgentAdapter(BaseAgentAdapter):
    adapter_key = "mock"
    display_name = "Mock Agent Engine"
    framework = "mock"
    capabilities = frozenset(
        {
            Capability.SUPPORTS_NATIVE_AGENTS,
            Capability.SUPPORTS_TOOL_CALLING,
            Capability.SUPPORTS_USAGE_METRICS,
            Capability.SUPPORTS_COST_REPORTING,
        }
    )

    #: Process-local run state. A real engine keeps this upstream.
    _runs: dict[str, dict] = {}

    # -- Helpers ------------------------------------------------------------

    @staticmethod
    def _rng(seed_text: str) -> random.Random:
        digest = hashlib.sha256(seed_text.encode("utf-8")).hexdigest()
        return random.Random(int(digest[:16], 16))

    def _run(self, execution_id: str) -> dict:
        run = self._runs.get(execution_id)
        if run is None:
            raise ExecutionNotFound(f"No mock execution '{execution_id}'.")
        return run

    # -- Lifecycle ----------------------------------------------------------

    def create_execution(self, request: ExecutionRequest) -> AgentExecutionHandle:
        execution_id = request.execution_id or str(uuid.uuid4())
        rng = self._rng(execution_id)

        needs_approval = bool(request.metadata.get("requires_approval", rng.random() < 0.2))
        step_count = rng.randint(2, len(_STEP_NAMES))

        self._runs[execution_id] = {
            "execution_id": execution_id,
            "remote_id": f"mock-run-{rng.randint(100000, 999999)}",
            "status": ExecutionStatus.WAITING_FOR_APPROVAL if needs_approval else ExecutionStatus.COMPLETED,
            "trace_id": request.trace_id or str(uuid.uuid4()),
            "model_id": request.model_id,
            "started_at": datetime.now(timezone.utc),
            "step_count": step_count,
            "needs_approval": needs_approval,
            "approval_id": None,
            "exchange": _EXCHANGES[rng.randrange(len(_EXCHANGES))],
            "metadata": dict(request.metadata),
        }
        return self.get_execution(execution_id)

    def get_execution(self, execution_id: str) -> AgentExecutionHandle:
        run = self._run(execution_id)
        return AgentExecutionHandle(
            execution_id=execution_id,
            remote_id=run["remote_id"],
            status=run["status"],
            trace_id=run["trace_id"],
            metadata=run["metadata"],
        )

    def cancel_execution(self, execution_id: str, reason: str = "") -> AgentExecutionHandle:
        run = self._run(execution_id)
        run["status"] = ExecutionStatus.CANCELLED
        run["cancel_reason"] = reason
        return self.get_execution(execution_id)

    def resume_execution(
        self, execution_id: str, decision: str = "approved", notes: str = ""
    ) -> AgentExecutionHandle:
        run = self._run(execution_id)
        run["decision"] = decision
        run["decision_notes"] = notes
        run["status"] = (
            ExecutionStatus.COMPLETED if decision == "approved" else ExecutionStatus.CANCELLED
        )
        return self.get_execution(execution_id)

    def request_approval(self, execution_id: str, payload: ApprovalRequestPayload) -> str:
        run = self._run(execution_id)
        approval_id = str(uuid.uuid4())
        run["approval_id"] = approval_id
        run["status"] = ExecutionStatus.WAITING_FOR_APPROVAL
        run["approval_payload"] = payload
        return approval_id

    # -- Observability ------------------------------------------------------

    def get_steps(self, execution_id: str) -> list[AgentStep]:
        run = self._run(execution_id)
        rng = self._rng(f"steps:{execution_id}")
        cursor = run["started_at"]
        steps: list[AgentStep] = []

        for index in range(run["step_count"]):
            duration = rng.randint(180, 3200)
            started_at = cursor
            completed_at = cursor + timedelta(milliseconds=duration)
            cursor = completed_at

            tool_calls: list[ToolCall] = []
            if rng.random() < 0.6:
                # An engine calls the tools its agent is given, when the caller says which.
                tool_name = rng.choice(run["metadata"].get("tools") or _TOOLS)
                tool_calls.append(
                    ToolCall(
                        tool_name=tool_name,
                        call_id=str(uuid.uuid4()),
                        arguments={"query": f"step-{index}"},
                        result={"ok": True},
                        duration_ms=rng.randint(50, 900),
                    )
                )

            steps.append(
                AgentStep(
                    step_index=index,
                    name=_STEP_NAMES[index % len(_STEP_NAMES)],
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_ms=duration,
                    usage=TokenUsage(
                        input_tokens=rng.randint(200, 4000), output_tokens=rng.randint(50, 1200)
                    ),
                    tool_calls=tool_calls,
                )
            )
        return steps

    def get_events(self, execution_id: str) -> list[NormalizedEvent]:
        run = self._run(execution_id)
        steps = self.get_steps(execution_id)
        events: list[NormalizedEvent] = []
        sequence = 0

        def add(event_type: EventType, timestamp: datetime, **kwargs) -> NormalizedEvent:
            nonlocal sequence
            event = NormalizedEvent(
                event_type=event_type,
                execution_id=execution_id,
                model_id=run["model_id"],
                trace_id=run["trace_id"],
                timestamp=timestamp,
                sequence=sequence,
                **kwargs,
            )
            sequence += 1
            events.append(event)
            return event

        prompt, completion = run["exchange"]

        start = run["started_at"]
        add(EventType.EXECUTION_STARTED, start, status=str(ExecutionStatus.RUNNING))
        add(EventType.AGENT_STARTED, start, metadata={"framework": self.framework})
        # The text rides on the events that already describe the interaction --
        # there is no separate prompt channel, and nothing is captured that an
        # engine did not choose to report.
        add(EventType.PROMPT_CREATED, start, metadata={"prompt": prompt})

        for index, step in enumerate(steps):
            add(
                EventType.AGENT_STEP_STARTED,
                step.started_at,
                metadata={"step": step.name, "index": step.step_index},
            )
            add(EventType.MODEL_REQUEST_STARTED, step.started_at)
            add(
                EventType.MODEL_REQUEST_COMPLETED,
                step.completed_at,
                duration_ms=step.duration_ms,
                input_tokens=step.usage.input_tokens,
                output_tokens=step.usage.output_tokens,
                # Only the final model call produces the answer the run returns;
                # intermediate calls are reasoning the operator did not ask for.
                metadata={"completion": completion} if index == len(steps) - 1 else {},
            )
            for call in step.tool_calls:
                add(EventType.TOOL_SELECTED, step.completed_at, metadata={"tool": call.tool_name})
                add(EventType.TOOL_CALL_STARTED, step.completed_at, metadata={"tool": call.tool_name})
                add(
                    EventType.TOOL_CALL_COMPLETED,
                    step.completed_at,
                    duration_ms=call.duration_ms,
                    metadata={"tool": call.tool_name},
                )
            add(
                EventType.AGENT_STEP_COMPLETED,
                step.completed_at,
                duration_ms=step.duration_ms,
                metadata={"step": step.name},
            )

        last_timestamp = steps[-1].completed_at if steps else start

        if run["needs_approval"]:
            add(
                EventType.APPROVAL_REQUESTED,
                last_timestamp,
                status=str(ExecutionStatus.WAITING_FOR_APPROVAL),
                metadata={"approval_id": run.get("approval_id")},
            )
            decision = run.get("decision")
            if decision == "approved":
                add(EventType.APPROVAL_GRANTED, last_timestamp)
            elif decision == "rejected":
                add(EventType.APPROVAL_REJECTED, last_timestamp)

        if run["status"] == ExecutionStatus.COMPLETED:
            add(EventType.EXECUTION_COMPLETED, last_timestamp, status=str(ExecutionStatus.COMPLETED))
        elif run["status"] == ExecutionStatus.CANCELLED:
            add(EventType.EXECUTION_CANCELLED, last_timestamp, status=str(ExecutionStatus.CANCELLED))

        return events

    def get_cost(self, execution_id: str) -> Decimal:
        usage = self.get_usage(execution_id)
        # Representative blended rate; authoritative pricing lives in the registry.
        return (
            Decimal(usage.input_tokens) / Decimal(1000) * Decimal("0.003")
            + Decimal(usage.output_tokens) / Decimal(1000) * Decimal("0.015")
        )
