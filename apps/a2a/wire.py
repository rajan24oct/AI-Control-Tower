"""A2A's wire shapes for what the gateway sends back: tasks, and its own messages.

Everything the gateway returns is a task in A2A's own shape, so an A2A client
reads a governed exchange exactly as it reads one with the agent directly. The
gateway's own view -- delivered or refused, and why -- rides in the task's
metadata under one key, where a client that does not know the gateway simply
ignores it.
"""

from __future__ import annotations

from apps.a2a.models import A2ATask, DeliveryStatus
from integrations.base.a2a import PART_KINDS, part_kind

#: The gateway's view of a task, in the metadata of everything it returns.
GATEWAY_KEY = "io.aicontroltower/gateway"
#: Where a message may carry what the gateway needs to know, inside `metadata`.
SKILL_KEYS = ("io.aicontroltower/skill", "skill")
CLASSIFICATION_KEYS = ("io.aicontroltower/dataClassification", "dataClassification")
PARENT_KEYS = ("io.aicontroltower/parentTaskId", "parentTaskId")
EXECUTION_KEYS = ("io.aicontroltower/executionId", "executionId")

_PREFIXES = {
    DeliveryStatus.BLOCKED: "Refused by the A2A Gateway",
    DeliveryStatus.UNAVAILABLE: "The agent is unavailable",
    DeliveryStatus.FAILED: "Delivery failed",
}


class MessageInvalid(ValueError):
    """A message A2A would not accept: the caller's mistake, not a refusal."""


def explanation(status: str, reason: str) -> str:
    prefix = _PREFIXES.get(status)
    return f"{prefix}: {reason}" if prefix else reason


def meta(message: dict, keys, *extra) -> str:
    """The first of `keys` found in the message's metadata, then in the request's."""
    for source in (message.get("metadata") if isinstance(message, dict) else None, *extra):
        if isinstance(source, dict):
            for key in keys:
                if source.get(key):
                    return str(source[key])
    return ""


def first_reference(message: dict) -> str:
    references = message.get("referenceTaskIds") if isinstance(message, dict) else None
    return str(references[0]) if isinstance(references, list) and references else ""


def validate_message(message) -> None:
    """Hold a message to A2A's shape before anything else looks at it."""
    if not isinstance(message, dict):
        raise MessageInvalid("A message is an object.")
    if message.get("role", "user") != "user":
        raise MessageInvalid("A message sent to an agent has the role 'user'.")
    if not isinstance(message.get("messageId"), str) or not message["messageId"].strip():
        raise MessageInvalid("A message needs a messageId.")
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts:
        raise MessageInvalid("A message needs at least one part.")
    for part in parts:
        kind = part_kind(part) if isinstance(part, dict) else ""
        if kind not in PART_KINDS:
            raise MessageInvalid("Each part is text, file or data.")
        if kind == "text" and not isinstance(part.get("text"), str):
            raise MessageInvalid("A text part carries its text as a string.")
        if kind == "data" and not isinstance(part.get("data"), dict):
            raise MessageInvalid("A data part carries an object.")
        if kind == "file":
            file = part.get("file")
            if not isinstance(file, dict) or not (file.get("bytes") or file.get("uri")):
                raise MessageInvalid("A file part carries its bytes or a URI.")
    for key in ("contextId", "taskId"):
        if key in message and not isinstance(message[key], str):
            raise MessageInvalid(f"{key} is a string.")
    if "metadata" in message and not isinstance(message["metadata"], dict):
        raise MessageInvalid("metadata is an object.")


def is_artifact(value) -> bool:
    return isinstance(value, dict) and isinstance(value.get("parts"), list) and bool(value["parts"])


def gateway_message(task_id, context_id: str, text: str) -> dict:
    """A status message in the gateway's own voice: why it refused, or what went wrong."""
    return {
        "kind": "message",
        "role": "agent",
        "messageId": f"gateway-{task_id}",
        "taskId": str(task_id),
        "contextId": context_id,
        "parts": [{"kind": "text", "text": text}],
        "metadata": {GATEWAY_KEY: {"from": "gateway"}},
    }


def gateway_meta(task: A2ATask) -> dict:
    meta_ = {
        "status": task.status,
        "recipient": task.recipient_name,
        "skill": task.skill_name or None,
        "hops": task.hops,
        "dataClassification": task.data_classification or None,
    }
    if task.reason:
        meta_["reason"] = task.reason
    if task.policies:
        meta_["policies"] = task.policies
    return meta_


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def task_wire(task: A2ATask, *, history: bool = True) -> dict:
    """An A2A Task, as the sender sees it."""
    status = {"state": task.state, "timestamp": _iso(task.completed_at or task.updated_at or task.created_at)}
    if not task.reached_recipient:
        status["message"] = gateway_message(task.pk, task.context_id, explanation(task.status, task.reason))
    elif task.status_message:
        status["message"] = task.status_message
    wire = {
        "kind": "task",
        "id": str(task.pk),
        "contextId": task.context_id,
        "status": status,
        "artifacts": task.artifacts or [],
        "metadata": {GATEWAY_KEY: gateway_meta(task)},
    }
    if history and task.message and not task.message.get("_truncated"):
        wire["history"] = [task.message]
    return wire


def rest_payload(task: A2ATask) -> dict:
    """The REST surface's view: the task, and the gateway's facts about it at the top level."""
    return {
        "task": task_wire(task),
        "task_id": str(task.pk),
        "status": task.status,
        "state": task.state,
        "sender": task.sender.agent_id if task.sender_id else None,
        "recipient": task.recipient_name,
        "skill": task.skill_name or None,
        "reason": task.reason,
        "policies": task.policies,
        "data_classification": task.data_classification or None,
        "personal_data": task.personal_data,
        "hops": task.hops,
        "latency_ms": task.latency_ms,
        "answer": task.answer_text,
    }


def inbox_entry(task: A2ATask) -> dict:
    """A task as a mailbox agent collects it: the message, and what it needs to answer."""
    return {
        "task_id": str(task.pk),
        "context_id": task.context_id,
        "sender": task.sender.agent_id if task.sender_id else None,
        "skill": task.skill_name or None,
        "message": task.message,
        "subject": task.subject,
        "data_classification": task.data_classification or None,
        "hops": task.hops,
        "trail": task.trail,
        "turns": task.turns,
        "created_at": _iso(task.created_at),
    }


def legacy_message(task: A2ATask) -> dict:
    """A task in the shape the old agent-messaging endpoints answered with."""
    if not task.reached_recipient:
        status = "blocked"
    elif task.collected_at:
        status = "read"
    else:
        status = "queued"
    message = task.message or {}
    return {
        "id": str(task.pk),
        "sender": task.sender.agent_id if task.sender_id else None,
        "recipient": task.recipient_name,
        "subject": task.subject,
        "body": task.text_preview,
        "correlation_id": task.context_id,
        "metadata": message.get("metadata") or {},
        "status": status,
        "blocked_reason": task.reason if not task.reached_recipient else "",
        "created_at": _iso(task.created_at),
        "read_at": _iso(task.collected_at),
    }
