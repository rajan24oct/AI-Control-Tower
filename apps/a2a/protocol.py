"""The A2A Gateway's own A2A endpoint: JSON-RPC 2.0 over HTTP.

Each published agent has an address at the gateway, `/api/v1/a2a/<address>/`,
and the gateway speaks A2A there as if it were the agent: `message/send` hands
it work, `tasks/get` and `tasks/cancel` follow the work. Behind the address
the registry, the contract and the content rules apply, and then the task is
either queued in the agent's mailbox or forwarded to the agent's own endpoint.

A refused message is answered as a task in the `rejected` state, with the
gateway's reason as its status message -- not as a JSON-RPC error. A2A keeps
protocol errors for malformed requests, and an agent that reads a rejected
task can adapt (ask someone else, or tell its user why it cannot help) where a
protocol error just breaks its turn. The MCP Gateway answers refused tool calls
as error results for the same reason.

Stateless, like the MCP endpoint: every request carries its own credential and
sending agent. Kept free of HTTP -- the view turns a request into a `Caller`
and a payload, and everything JSON-RPC happens here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from apps.a2a.models import A2AChannel
from apps.a2a.registry import published_card
from apps.a2a.services import (
    A2AGateway,
    A2AGatewayError,
    SendRequest,
    TaskNotCancelable,
    TaskNotFound,
)
from apps.a2a.wire import MessageInvalid, task_wire, validate_message

logger = logging.getLogger(__name__)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# A2A's own codes.
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
PUSH_NOT_SUPPORTED = -32003
UNSUPPORTED_OPERATION = -32004
#: Implementation-defined (-32000 to -32099, clear of A2A's): no such agent here.
UNKNOWN_AGENT = -32050


class ProtocolError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def error_response(message_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


@dataclass
class Caller:
    organization: Any
    #: The published agent this address belongs to: the recipient.
    card: Any
    #: The agent sending: named in X-Agent-ID, or the one agent the key is bound to.
    agent: Any = None
    service_account: Any = None
    environment: str = ""
    execution_id: str = ""
    #: The address's absolute URL, which the published card names.
    url: str = ""


class A2AEndpoint:
    def __init__(self, caller: Caller):
        self.caller = caller

    def handle(self, payload) -> dict | list | None:
        """Answer one message or a batch. None means nothing to send back (202)."""
        if isinstance(payload, list):
            if not payload:
                return error_response(None, INVALID_REQUEST, "Empty batch.")
            replies = [reply for reply in map(self._one, payload) if reply is not None]
            return replies or None
        return self._one(payload)

    def _one(self, message) -> dict | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            message_id = message.get("id") if isinstance(message, dict) else None
            return error_response(message_id, INVALID_REQUEST, "Not a JSON-RPC 2.0 message.")
        if "method" not in message or "id" not in message:
            # A response or a notification: nothing the gateway answers.
            return None

        message_id = message["id"]
        method = message["method"]
        handler = self._handlers().get(method) if isinstance(method, str) else None
        if handler is None:
            return error_response(message_id, METHOD_NOT_FOUND, f"Method not found: {method}")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return error_response(message_id, INVALID_PARAMS, "params must be an object.")

        try:
            return {"jsonrpc": "2.0", "id": message_id, "result": handler(params)}
        except ProtocolError as exc:
            return error_response(message_id, exc.code, exc.message)
        except Exception:
            logger.exception("A2A endpoint failed while handling %s", method)
            return error_response(message_id, INTERNAL_ERROR, "Internal error.")

    def _handlers(self) -> dict:
        return {
            "message/send": self.send,
            "tasks/get": self.get,
            "tasks/cancel": self.cancel,
            "agent/getAuthenticatedExtendedCard": self.card,
            "message/stream": self._no_streaming,
            "tasks/resubscribe": self._no_streaming,
            "tasks/pushNotificationConfig/set": self._no_push,
            "tasks/pushNotificationConfig/get": self._no_push,
            "tasks/pushNotificationConfig/list": self._no_push,
            "tasks/pushNotificationConfig/delete": self._no_push,
        }

    # -- Methods ------------------------------------------------------------

    def send(self, params: dict) -> dict:
        message = params.get("message")
        try:
            validate_message(message)
        except MessageInvalid as exc:
            raise ProtocolError(INVALID_PARAMS, str(exc)) from None
        metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
        configuration = params.get("configuration") if isinstance(params.get("configuration"), dict) else {}
        request = SendRequest.from_message(
            message, params_metadata=metadata, sender=self.caller.agent,
            environment=self.caller.environment, execution_id=self.caller.execution_id,
            channel=A2AChannel.JSONRPC, configuration=configuration,
        )
        try:
            outcome = A2AGateway.send(
                self.caller.organization, request, recipient=self.caller.card,
                service_account=self.caller.service_account,
            )
        except TaskNotFound as exc:
            raise ProtocolError(TASK_NOT_FOUND, f"Task not found: {exc}") from None
        except A2AGatewayError as exc:
            raise ProtocolError(INVALID_PARAMS, str(exc)) from None
        if outcome.task is None:
            raise ProtocolError(INTERNAL_ERROR, outcome.reason or "The gateway could not record the task.")
        return task_wire(outcome.task)

    def _task_id(self, params: dict) -> str:
        task_id = params.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise ProtocolError(INVALID_PARAMS, "Name the task: params.id.")
        return task_id

    def get(self, params: dict) -> dict:
        try:
            task = A2AGateway.get(self.caller.organization, self._task_id(params), agent=self.caller.agent)
        except TaskNotFound as exc:
            raise ProtocolError(TASK_NOT_FOUND, f"Task not found: {exc}") from None
        return task_wire(task, history=params.get("historyLength") != 0)

    def cancel(self, params: dict) -> dict:
        try:
            task = A2AGateway.cancel(self.caller.organization, self._task_id(params), agent=self.caller.agent)
        except TaskNotFound as exc:
            raise ProtocolError(TASK_NOT_FOUND, f"Task not found: {exc}") from None
        except TaskNotCancelable as exc:
            raise ProtocolError(TASK_NOT_CANCELABLE, str(exc)) from None
        except A2AGatewayError as exc:
            raise ProtocolError(INTERNAL_ERROR, str(exc)) from None
        return task_wire(task)

    def card(self, params: dict) -> dict:
        return published_card(self.caller.card, url=self.caller.url)

    def _no_streaming(self, params: dict) -> dict:
        raise ProtocolError(
            UNSUPPORTED_OPERATION,
            "The gateway does not stream: send with message/send and follow the task with tasks/get.",
        )

    def _no_push(self, params: dict) -> dict:
        raise ProtocolError(
            PUSH_NOT_SUPPORTED,
            "The gateway never calls out to a sender: follow the task with tasks/get.",
        )
