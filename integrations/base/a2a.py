"""Agent2Agent (A2A) adapter contract.

The Control Tower talks to an A2A agent in two roles, as it does to an MCP
server. As a *registry* it reads the agent's Agent Card so governance can
review the skills it advertises; as a *gateway* it delivers messages to the
agent on another agent's behalf and follows the tasks they start. Both go
through this contract, so the platform never branches on how an agent is
reached -- a new transport is a new adapter, not a new code path.

Kept in A2A's own vocabulary -- messages made of parts, tasks with a state --
because the gateway forwards them rather than translating them. Like the rest
of `integrations`, nothing here imports Django.
"""

from __future__ import annotations

import abc
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from integrations.base.config import AdapterConfig
from integrations.base.exceptions import AdapterError
from integrations.base.types import HealthCheckResult

#: The A2A revision this platform speaks.
PROTOCOL_VERSION = "0.3.0"

#: Where an agent publishes its card, newest first: 0.3 renamed the file.
WELL_KNOWN_CARD_PATHS = (".well-known/agent-card.json", ".well-known/agent.json")

#: A2A's task states. A task in a terminal state never changes again.
TASK_STATES = (
    "submitted", "working", "input-required", "auth-required",
    "completed", "canceled", "failed", "rejected", "unknown",
)
TERMINAL_STATES = frozenset({"completed", "canceled", "failed", "rejected"})

#: The kinds of part a message or an artifact is made of.
PART_KINDS = ("text", "file", "data")


class A2AError(AdapterError):
    """Base class for A2A adapter failures."""


class A2ATransportError(A2AError):
    """The agent could not be reached, timed out, or answered with an HTTP error.

    This is the failure that counts against an agent's breaker: it says nothing
    about the message and everything about whether the agent is there.
    """


class A2AProtocolError(A2AError):
    """The agent answered, with a JSON-RPC error.

    The agent is alive -- it refused this request. Kept apart from transport
    failures so an agent rejecting one message never takes it out of rotation.
    """

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"A2A error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


def _strings(value) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def part_kind(part: dict) -> str:
    """A part's kind. A2A 0.3 calls it `kind`; earlier drafts called it `type`."""
    return str(part.get("kind") or part.get("type") or "")


def parts_text(parts) -> str:
    """The readable content of a list of parts: text, and structured data as JSON.

    Files are named, never read: the gateway records that a file was sent, not
    what was in it.
    """
    pieces = []
    for part in parts if isinstance(parts, list) else []:
        if not isinstance(part, dict):
            continue
        kind = part_kind(part)
        if kind == "text":
            pieces.append(str(part.get("text", "")))
        elif kind == "data":
            pieces.append(json.dumps(part.get("data"), default=str, sort_keys=True))
        elif kind == "file":
            file = part.get("file") if isinstance(part.get("file"), dict) else {}
            pieces.append(f"[file {file.get('name') or file.get('uri') or 'unnamed'}]")
    return "\n".join(piece for piece in pieces if piece)


@dataclass
class A2ASkillDefinition:
    """One skill as an agent advertises it on its card."""

    id: str
    name: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    input_modes: list[str] = field(default_factory=list)
    output_modes: list[str] = field(default_factory=list)

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> "A2ASkillDefinition":
        return cls(
            id=str(payload["id"]),
            name=str(payload.get("name", "") or ""),
            description=str(payload.get("description", "") or ""),
            tags=_strings(payload.get("tags")),
            examples=_strings(payload.get("examples")),
            input_modes=_strings(payload.get("inputModes")),
            output_modes=_strings(payload.get("outputModes")),
        )

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "id": self.id,
            "name": self.name or self.id,
            "description": self.description,
            "tags": self.tags,
        }
        if self.examples:
            wire["examples"] = self.examples
        if self.input_modes:
            wire["inputModes"] = self.input_modes
        if self.output_modes:
            wire["outputModes"] = self.output_modes
        return wire

    def definition_hash(self) -> str:
        """Fingerprint of everything a reviewer approved.

        The id is left out -- it is the key. The description is in: it is what a
        calling agent's model reads when it decides what to hand over, so a
        changed description is exactly how a skill is turned into a way to
        extract data from the agents that trust it.
        """
        canonical = json.dumps(
            {
                "name": self.name,
                "description": self.description,
                "tags": self.tags,
                "examples": self.examples,
                "input_modes": self.input_modes,
                "output_modes": self.output_modes,
            },
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class A2ACardInfo:
    """What an agent said about itself on its Agent Card."""

    name: str = ""
    description: str = ""
    version: str = ""
    protocol_version: str = ""
    url: str = ""
    provider: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)
    default_input_modes: list[str] = field(default_factory=list)
    default_output_modes: list[str] = field(default_factory=list)
    skills: list[A2ASkillDefinition] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_wire(cls, payload: Any) -> "A2ACardInfo":
        if not isinstance(payload, dict):
            raise A2AProtocolError(-32006, "The Agent Card is not a JSON object.")
        skills = [
            A2ASkillDefinition.from_wire(item)
            for item in payload.get("skills") or []
            if isinstance(item, dict) and item.get("id")
        ]
        return cls(
            name=str(payload.get("name", "") or ""),
            description=str(payload.get("description", "") or ""),
            version=str(payload.get("version", "") or ""),
            protocol_version=str(payload.get("protocolVersion", "") or ""),
            url=str(payload.get("url", "") or ""),
            provider=payload.get("provider") if isinstance(payload.get("provider"), dict) else {},
            capabilities=payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else {},
            default_input_modes=_strings(payload.get("defaultInputModes")),
            default_output_modes=_strings(payload.get("defaultOutputModes")),
            skills=skills,
            raw=payload,
        )

    def card_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.raw, sort_keys=True, default=str).encode("utf-8")).hexdigest()


@dataclass
class A2ATaskResult:
    """What an agent answered: a task, or -- for a quick answer -- a message.

    A2A lets an agent answer without starting a task. A reply that is a message
    is a finished exchange, and is recorded as a completed task whose status
    message is the reply.
    """

    task_id: str = ""
    context_id: str = ""
    state: str = "unknown"
    status_message: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_wire(cls, payload: Any) -> "A2ATaskResult":
        if not isinstance(payload, dict):
            raise A2AProtocolError(-32006, "The agent's answer is neither a task nor a message.")
        if payload.get("kind") == "message" or ("parts" in payload and "status" not in payload):
            return cls(
                task_id=str(payload.get("taskId") or ""),
                context_id=str(payload.get("contextId") or ""),
                state="completed",
                status_message=payload,
                raw=payload,
            )
        status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
        state = str(status.get("state") or "unknown")
        return cls(
            task_id=str(payload.get("id") or ""),
            context_id=str(payload.get("contextId") or ""),
            state=state if state in TASK_STATES else "unknown",
            status_message=status.get("message") if isinstance(status.get("message"), dict) else None,
            artifacts=[item for item in payload.get("artifacts") or [] if isinstance(item, dict)],
            raw=payload,
        )

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


class BaseA2AAdapter(abc.ABC):
    """Uniform interface to an A2A agent, whatever its transport."""

    adapter_key: str = ""
    display_name: str = ""
    capabilities: frozenset[str] = frozenset()

    def __init__(self, config: AdapterConfig | None = None):
        self.config = config or AdapterConfig(key=self.adapter_key)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} key={self.adapter_key!r}>"

    @abc.abstractmethod
    def get_card(self) -> A2ACardInfo:
        """Read the agent's card: who it is and the skills it offers."""

    @abc.abstractmethod
    def send_message(
        self, message: dict[str, Any], *, metadata: dict[str, Any] | None = None,
        configuration: dict[str, Any] | None = None,
    ) -> A2ATaskResult:
        """`message/send`. Raises A2ATransportError or A2AProtocolError."""

    @abc.abstractmethod
    def get_task(self, task_id: str) -> A2ATaskResult:
        """`tasks/get`: where the task stands now."""

    @abc.abstractmethod
    def cancel_task(self, task_id: str) -> A2ATaskResult:
        """`tasks/cancel`."""

    def health_check(self) -> HealthCheckResult:
        """Reading the card is the cheapest request every agent must answer."""
        started = time.monotonic()
        try:
            card = self.get_card()
        except A2AError as exc:
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
            message=f"{card.name or 'Agent'} {card.version}".strip(),
            checked_at=datetime.now(timezone.utc),
        )
